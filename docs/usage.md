# FlashQuant 使用与部署指南

> 原项目：[pipi-520/FlashQuant](https://github.com/pipi-520/FlashQuant)。全部内容仅作学习研究，不构成投资建议。

## 一、安装（Python 3.12）

```bash
python -m venv .venv
# Windows
.venv\Scripts\python -m pip install -r requirements.txt
# Linux / macOS
.venv/bin/python -m pip install -r requirements.txt
```

> 本机只有旧版 Python 时可用 `uv venv .venv -p 3.12` 自动下载。

## 二、新闻聚合 + 日报 + Dashboard

```bash
python news_aggregator/run.py          # 抓取 + 情绪分 + 影响分 + 日报
python news_aggregator/run.py --push   # 并推送到企业微信
python news_aggregator/run.py --rebuild-history   # 从 raw 全量重算历史情绪
```

产物：

- `news/raw/{date}.jsonl` —— 原始新闻归档（按新闻日期去重写入）
- `news/daily_sentiment.json` —— 市场 + 个股日度情绪
- `news/report/{date}.md` —— 当日舆情日报（Markdown）
- `dashboard/index.html` —— 静态可视化页面（每次聚合后自动重建）

## 三、实时事件监测（24/7 常驻）

```bash
python news_aggregator/monitor.py               # 常驻轮询（默认 15s）
python news_aggregator/monitor.py --once        # 只跑一轮
python news_aggregator/monitor.py --dry-run     # 只检测不推送，且不落盘（raw/history/seen/alerts/state 全跳过），打印摘要预览与字节数
python news_aggregator/monitor.py --no-boards   # 跳过板块缓存（更快/离线）
```

工作流：轮询快讯源 → id 去重（`news/seen.json`）→ 情绪打分 → 事件词匹配（`themes.yaml`）→ 影响分/情绪地板过滤 → **注意力预算**（同题去重 + 每主题配额 + 每轮 top-N，见 `news_aggregator/alert_filter.py`）→ 板块/个股映射（只对入选条目调 LLM）→ 多通道告警。首次运行只建立基线不告警历史旧闻。

调节推送量的旋钮都在 `config.yaml` 的 `monitor:` 段：`impact_min` / `score_min`（入口阈值）、`max_alerts_per_round`（每轮条数上限，2026-11-09 15:06 反馈后 8→10）、`max_per_theme`（单主题配额，3）、`macro_reserve`（每轮给宏观主题预留的名额，0=关闭）、`dedupe.hours`（跨轮冷却窗）、`dedupe.min_prefix_chars`（事件主干公共前缀同题阈值，0=关闭）、`digest_top_detail`（摘要里带板块明细行的条数，现为 0=不带）、`digest_top_keywords`（抬头热词榜取前几个，现为 14；热词统计口径是**全部过阈值候选**，与列表的 top-N 预算无关——列表只留 10 条，词云仍能覆盖整轮热点）、`fund_flow` / `fund_flow_timeout`（A 股条目附「主力净流入」标签，见下）、`rich_card.enabled`/`rich_card.also_send_text`（图片卡片与「图片+文本」双发开关，见下）。被筛掉的候选仍写进 `news/alerts.jsonl`（`pushed=false` + `reason`），用 `python scripts/alert_review.py` 校验有没有把 alpha 挤掉。

一轮只发一条企微 markdown 消息，结构是「抬头统计 → 按主题分组的条目」：

```
**[事件告警汇总] 11-09 15:06 · 推送 10 条（过阈值 120）**
热词：AI17 · 人工智能12 · 英伟达11 · 业绩10 · 临床7 · 半导体6 · 成交额6 · 新药6 · 碳酸锂6 · 银行6 · Trump5 · 公告5 · 创新药5 · 特朗普5
主线：机器人 3 · AI/算力 2 · 财报/公告 2 · 创新药/疫苗 1 · 半导体/芯片 1 · 白宫/贸易政策 1
多空：利好6/利空4
抑制：同题 17 · 主题配额 1 · 预算 92（落选明细见 dashboard 告警历史）

**财报/公告（2）**
· [耐普矿机：秘鲁工厂预计2027年下半年投产](https://finance.eastmoney.com/a/…)｜0.60 · +0.71 · 主力+484万(涨+0.6%) · 中标订单(利好) · 东财全球资讯
· [中国长城：董沛武辞去独立董事等职位](https://finance.eastmoney.com/a/…)｜0.59 · -0.67 · 主力-2314万(涨+0.3%) · 东财全球资讯

**机器人（3）**
· [机器人零部件产品是否有实质订单落地？恒而达回应](https://finance.eastmoney.com/a/…)｜0.59 · +0.66 · 主力-103万(涨+1.2%) · 中标订单(利好) · 东财全球资讯
```

上面是 15:06 那轮按新版规则回放的真实输出（共 10 条 / 6 主题，节省略）。三处口径要分清：热词按**全部 120 条过阈值候选**计数，与列表的 top-N 预算互不影响；单行里的资金面标签只给「标题里恰好点名一只 A 股」的条目，取的是**当前**东财主力净流入榜（回放时点值，不是 15:06 盘中值）；没带 `×N源` 的行是轮内唯一源。

每条新闻的标题是 markdown 超链接，指回原文地址（企微群机器人支持 `[标题](url)`，点开即可细读；括号/空格做百分号转义，链接不会把 markdown 撑破）。`×N源` 表示轮内有 N 家源在报同一事件（多源互证是重要性的客观信号）。主题分组整块参与切块，即使单组超预算也不会把组标题和条目拆到两条消息里。标题按语义断句截断（优先句末标点，其次逗号/分号等子句边界，再次闭合括号，最后才硬切加 `…`；英文标题再退一档回到词边界，不会把 "To 52-Week Lows" 切成 "To 52"），不再出现「涨超3%！机器人ET…」这类把数字切半的行。分组主题按「标题命中词数」优先（标题没提到才算正文），治的是「军工电子板块走高，铖昌科技涨停」被正文里的「成交额」带偏分到券商/金融。企微群机器人的 markdown 不能内嵌图片，所以文本摘要里的「词云」是热词权重榜（频次降序）；真图片见下。

**图片卡片（`news_aggregator/card.py`）**：同一批入选告警再渲染成一张 PNG，用独立的 `msgtype=image` 消息发出（base64+md5，官方限 2MB；超限或字体缺失即跳过图片、只发文本，绝不硬发）。版面自上而下是抬头（轮次 + 推送/过阈值/新增）→ 主线 → 多空 → 词云 → 按主题分组的条目 → 抑制构成页脚。条目里标题不截断、按像素折行（最多两行），指标行是「影响 · 情绪 · 资金面（A 股条目才有）· 事件类型 · 来源 · ×N源」，利好红/利空绿与 A 股习惯一致。依赖只有 Pillow + 一套中文字体（香橙派用系统 Noto Sans CJK SC，字体探测失败直接报错，不画方框），不需要 wordcloud/matplotlib。旋钮在 `config.yaml` 的 `monitor.rich_card:`——`enabled`（默认开）、`also_send_text`（默认 true：图片不能复制也不能检索，文本摘要继续发；确认图片够用后置 false 就一轮只一条图片消息）、`width`、`max_bytes`、`font_bold`/`font_regular`。本地看图不推送：`python news_aggregator/monitor.py --once --dry-run --save-card /tmp/card.png`。实测 15:06 那轮按新版规则回放 10 条 / 6 主题 / 14 个热词 → 1080×1960、402KB；11:36 那轮 8 条 / 6 主题 → 1080×1912、396KB；稀疏轮 2 条 → 1080×874、136KB。

**生产部署（当前实际形态，香橙派 192.168.31.226）**：cron 单轮驱动，不是常驻进程。
`scripts/cron_monitor.sh`（事件告警，每交易日 08:45 / 11:35 / 15:05 / 21:30 四档）与
`scripts/cron_daily.sh`（收盘日报 + dashboard 刷新，18:30 一档），周末不跑；
时刻表与代理/venv 约定写在两个脚本头部注释里。每轮开头 `git pull --ff-only`，
跑完把 `news/`（与 `dashboard/index.html`）commit 回仓库——**所以提交即部署**。
注意该链路没有文件锁：上一轮没跑完时下一轮会并发启动（2026-11-08 曾因此卡住 4 天），
加 `flock -n` 是待办。

**Linux 常驻（systemd/docker，备用方案）**：

```bash
sudo bash scripts/install_service.sh /opt/chaogu   # systemd 自启 + 崩溃重启
# 或
docker compose up -d --build
```

**Windows 常驻（开发机预览用）**：

```powershell
# 管理员 PowerShell：注册开机自启任务计划（崩溃 5s 自动重启）
powershell -ExecutionPolicy Bypass -File scripts\install_win_service.ps1
# 或临时手动跑守护循环
powershell -ExecutionPolicy Bypass -File scripts\monitor_win_loop.ps1
# 卸载：Unregister-ScheduledTask -TaskName 'chaogu-monitor' -Confirm:$false
```

## 四、Dashboard 静态页面

```bash
python scripts/generate_dashboard.py               # 生成 dashboard/index.html
python scripts/generate_dashboard.py --open        # 生成并用浏览器打开
python scripts/generate_dashboard.py --days 14 --top 100
python scripts/generate_dashboard.py --watch 60    # 每 60s 重建（配合 monitor 常驻）
```

页面包含：KPI 卡片、市场情绪折线、来源/情绪分布、可过滤高影响新闻表、告警历史（只展示实际推送的告警，同窗口被同题去重/配额/预算筛掉的候选数在标题栏给出）。零外部依赖，浏览器直接打开。

托管到 GitHub Pages：在仓库 Settings → Pages 选择 `master` 分支 `/dashboard` 目录即可（产物已随仓库提交）。

## 五、量化回测 + 模拟盘

```bash
python scripts/download_data.py       # 下载行情（腾讯/新浪财经，前复权）→ data/*.csv + vnpy SQLite
python scripts/sentiment_score.py     # 生成情绪分 → data/sentiment_*.csv
python scripts/backtest.py            # vnpy 回测 → results/backtest_report.md
python scripts/paper_trade.py         # 本地模拟盘（T-1 信号 T 日开盘成交）
python scripts/paper_trade.py --reversal   # 反转因子模拟盘（42 只池）
python scripts/predictive_power.py    # 事件预测力回看
python scripts/audit_sources.py       # 数据源健康度审计（URL/时间/文本质量，可 --source 指定）
python scripts/backfill_news.py       # 回填历史新闻（A股东财分页 + 美股 SEC 8-K/Finnhub）
```

模拟盘持久化 `paper/paper_state.json` + `paper/trades.csv`（反转模式独立状态文件），重复运行只处理新增交易日。

策略参数（阈值/止损/仓位等）见 `config.yaml` 的 `sentiment` / `reversal` / `backtest` 段，详细逻辑见 [量化策略说明](../量化策略说明.md)。

## 六、一键日常流水线

```powershell
scripts\daily_run.cmd        # Windows：聚合 → 情绪 → 模拟盘 → 回测
```

## 七、推送通道配置

密钥写入 `.env`（参考 `.env.example`）或 `config.yaml` 对应留空字段：

| 通道 | 环境变量 |
|---|---|
| 企业微信（日报 + 告警机器人分开） | `WECOM_WEBHOOK` |
| Server酱 | `SERVERCHAN_SENDKEY` |
| Telegram | `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` |
| ntfy | `NTFY_TOPIC` |

一手源可选密钥：`AP_API_KEY`、`CONGRESS_API_KEY`、`QUIVER_TOKEN`、`BARGO_BASE_URL`/`BARGO_API_KEY`、`FRED_API_KEY`（无 key 自动走免 key CSV 端点）、`FINNHUB_API_KEY`（历史回填用）。

## 八、GitHub Actions 日报

仓库自带工作流：每个工作日 18:30（北京时间）自动聚合 → 推送企业微信 → commit `news/` 回仓库。需在仓库 Secrets 配置 `WECOM_WEBHOOK`。

## 九、已知限制

- 免费快讯源只返回最近数日新闻；历史需 `backfill_news.py` 回填，A 股分页深度需实测
- 手写单点源（非 akshare）无契约，改版即失效（政策公告源已修复过一次）；建议对连续失败源做健康度告警
- 词典情绪 v1 较粗，可切 FinBERT/LLM 后端提升精度
- A 股无完全免费稳定的实时逐笔行情，vnpy PaperAccount 撮合需 IB/TuShare 等实时源

## 十、运维备忘（本 fork 实测记录）

- 主分支 `master`；`origin` 为本 fork，`upstream` 为原项目（`git fetch upstream` 同步）
- 板块接口在代理/受限网络下 ProxyError 自动降级，告警不受影响
- 日报/`news/` 数据由 CI 逐日 commit 累积，历史新闻不可回填时以本地 `news/raw/` 累积为准
