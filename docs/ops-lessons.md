# 运行事故与教训记录（Ops Lessons）

> 2026-10-03 ~ 10-11 部署与重打分战役的完整复盘。
> 每条 = 事故 → 根因 → 规矩。**规矩比事故重要**——同类错误不允许第二次。
> 关联：`docs/usage.md`（部署）、`docs/architecture.md`（架构）、`scripts/audit_sources.py`（数据源健康审计）

---

## 一、验证纪律（出现频率最高的病）

### 1.1 未验证就宣布完成（本役重复 4 次）

| 事故 | 后果 |
|---|---|
| news.html 生成后从未打开验证 | `symbol_trends` 缺失 → 页面下半区全部不渲染 |
| rescore 自动上传的 datetime 变量遮蔽 | 跑完全量在最后一步崩溃 |
| "重写 0 条 = 算完了"的错误解读 | 误判进度，误导用户 |
| dashboard/index.html 冲突未验证就断言"Pi 没丢数据" | 实为验证脚本缺陷造成的误报 |

**规矩：**
1. 任何"完成"宣告前必须跑完**验证清单**，清单在动手前写好；
2. 涉及页面/文件的改动，必须**实际渲染/解析**验证，不能只看"脚本没报错"；
3. 声明数据正常必须给出**量化证据**（零分占比、|sent| 分布、坏行数），不接受"看起来对"。

### 1.2 估算不实测

- 事故：rescore 全量预估"10-20 分钟"，实际 2-4 小时（Pi 速率高估 2-4 倍 + 英文条量低估）。
- **规矩：批量任务先跑 100 条实测速率，用实测值推全量 ETA。** `finbert` 打分进度行（每 200 条输出速率/ETA）就是这条的产物。

---

## 二、跨机器执行（最大的坑）

### 2.1 环境差异 = 数据污染

- 事故：Windows 无 transformers → FinBERT 静默降级词典 → 用词典分覆盖了 Pi 的 FinBERT 结果并 push（10-11 词典污染）。
- **规矩：带副作用的批处理任务，只在与生产环境一致的机器上跑。** `rescore_raw.py` 已加硬守卫：backend=finbert 但 transformers 不可用时**报错中止，拒绝降级**（降级 = 数据污染，不是容错）。

### 2.2 进程叠加

- 事故：四次重跑命令都没让先杀旧实例 → 4 个 rescore 进程并跑（374% CPU 是四个进程之和，曾被误读为单进程健康）→ 互踩文件 + OOM。
- **规矩：任何"重跑"命令必带 `pkill -f <脚本名>` 前置步骤；发出裸启动命令 = 事故。**
- 附注：uv venv 的解释器叫 `python` 不叫 `python3`——`ps -C python3` 匹配不到，查进程用 `pgrep -af <脚本名>`。

### 2.3 状态文件跨机污染

- 事故：Windows 的 1 条状态记录被 push，Pi 的 295 条进度被 `git pull` 覆盖 → 断点失效全量重来。
- **规矩：**
  1. 机器本地状态文件必须 gitignore（`news/rescore_state*.json` 已加）；
  2. 状态必须记录**后端名**（换后端自动作废，已实现）；
  3. **绝不清除有效的断点状态**——"重写 0 条"证明过计算有效时，清状态 = 纯浪费（2026-10-11 实证：误判清状态白烧 30 分钟）。

### 2.4 跨机并行必须分片

- 并行写同一批文件 = 并发截断坏行（20261003 的 16 条坏行）。
- **规矩：多机并行用 `--shard I:N` 分片（已实现），文件集不相交 + 状态文件按分片隔离。**

---

## 三、CI / GitHub 的特有坑

| 坑 | 事故 | 规矩 |
|---|---|---|
| env 键**大小写不敏感**查重 | `NO_PROXY` + `no_proxy` 同块 → workflow 解析失败（startup_failure，push 即报） | 改 workflow 后先本地 YAML 校验 + 大小写查重再推 |
| PyYAML 把 `on:` 解析为布尔 True | 校验脚本 `doc.get("on")` 拿不到触发器（误判"inputs 丢失"） | 校验脚本用 `doc.get(True)` |
| contents API 对 **>1MB 文件返回空 content** | 验证脚本误报"10-06/07/08 Finnhub 条目丢失"（实际完好） | **数据验证一律走 `git show`，不走 contents API** |
| workflow 文件改名后 Actions 服务记录卡死 | flashquant.yml 新路径长期 name=path（未解析态），dispatch 422 | 换路径 = 换登记；改名后若 dispatch 持续 422，检查 workflow list 的 name 是否已更新 |
| `|| echo` 兜不住 step 级超时 | 超时是 runner 强杀，shell 内兜底逻辑不执行 → 连坐跳过后续步骤 | 用 `continue-on-error: true` + `if ! cmd; then echo warning; fi` |
| 长任务超时预算 | rescore 两连取消（40 分钟 job timeout） | 长任务 job timeout 按**实测速率 × 3** 设置 |
| fork 仓库 Actions 限制 | schedule 投递延迟 55 分钟~5.9 小时；workflow 解析态长期不刷新 | fork 仓库不要依赖 GitHub schedule；外部触发（cron-job.org）+ 本地 cron 均已验证 |

---

## 四、数据源 / 第三方 API 的契约坑

| 坑 | 事故 | 规矩 |
|---|---|---|
| 手写单点源无契约 | gov.cn RSS 下线（源死）；华尔街见闻 uri 拼接假设错误（1673 条坏链）；SEC findtext 缺 namespaces（源上线以来 0 条）；Finnhub 日期格式 YYYY-MM-DD（紧凑格式返回空） | **新源上线必配录制回放测试**（`tests/test_fetchers_regression.py` 模式）+ `audit_sources.py` 纳入定期审计 |
| 静默吞错 | fetchers 的 `except Exception` 让死源潜伏数月 | 数据源健康度告警：每源 N 小时滑动窗口产出数，归零即报 |
| 第三方 LLM 口述不可信 | 外部 AI 的"监管风险核查"存在公司名-代码错配幻觉 | 此类输出只当线索，**验证必须走确定性数据**（TM stock_name_history / 新闻归档全文检索） |
| 大文件 contents API 返回空 content | 误报数据丢失 | 同上：验证走 git |

---

## 五、部署架构结论（已定，不再反复）

1. **Pi（自托管 runner）只跑日常增量**：每天 4 轮 monitor（各几百条，含少量英文 FinBERT），单轮几分钟——Pi 扛得住；
2. **全量/重打分类任务 → Windows（或更强机器）**：装好 transformers + torch（CPU 版），跑完 git push；
   - 实测基准（2026-10-11，benchmark_finbert.py，200 条样本）：Windows AMD64 **15.1 条/秒** vs Pi RK3588 **2.84 条/秒**（5.3 倍差）
   - **分派阈值：单批 > 1000 条英文 → 必须 Windows**（Pi 2.2-2.8 条/秒，OOM + 超时风险都在长任务上）
3. **单一写入方**：news/ 只由统一工作流（flashquant.yml，mode 参数区分 monitor/daily）提交，news-aggregator.yml 已退役；
4. **cron-job.org 外部触发**：GitHub schedule 对 fork 仓库不可靠（实测延迟 55 分钟~5.9 小时）；workflow 改名后 cron-job 的 dispatch URL 要同步更新；
5. **新闻数据再分发**：公开仓库含媒体快讯正文属灰色地带——个人研究规模可接受（上游先例），仓库若转向展示/商业化，先把 news/ 数据抽到私有仓库。

---

## 六、当前未完成事项（2026-10-11 状态）

- [ ] Windows 分片（shard 0:2）+ Pi 分片（shard 1:2）并行重打分进行中，等两边 `git push: OK`
- [ ] 完成后验证：坏行全 0 / Finnhub 零分占比趋近 0 / 英文 |sent| 收敛 0.2~0.25
- [ ] TM 侧全量摄取：`python3 scripts/ingest_flashquant.py --days 1000`（Pi 的 TM source 目录）
- [ ] news.html 刷新验证（坏行修复 + FinBERT 分数 + 环形图）
- [ ] backlog：情绪缓存（id → [后端版本, 分数]，让权重/主题调整变成秒级重加权）
- [ ] backlog：cron-job.org 4 个任务的 dispatch URL 已改 flashquant.yml；新增 daily 槽 payload 带 `"mode":"daily"`
