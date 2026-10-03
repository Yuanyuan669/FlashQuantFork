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
python news_aggregator/monitor.py --dry-run     # 只检测不推送
python news_aggregator/monitor.py --no-boards   # 跳过板块缓存（更快/离线）
```

工作流：轮询快讯源 → id 去重（`news/seen.json`）→ 情绪打分 → 事件词匹配（`themes.yaml`）→ 板块/个股映射 → 多通道告警。首次运行只建立基线不告警历史旧闻。

**Linux 常驻（推荐，ubuntu 22.04 arm64 实测可用）**：

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

页面包含：KPI 卡片、市场情绪折线、来源/情绪分布、可过滤高影响新闻表、告警历史。零外部依赖，浏览器直接打开。

托管到 GitHub Pages：在仓库 Settings → Pages 选择 `master` 分支 `/dashboard` 目录即可（产物已随仓库提交）。

## 五、量化回测 + 模拟盘

```bash
python scripts/download_data.py       # 下载行情（腾讯/新浪财经，前复权）→ data/*.csv + vnpy SQLite
python scripts/sentiment_score.py     # 生成情绪分 → data/sentiment_*.csv
python scripts/backtest.py            # vnpy 回测 → results/backtest_report.md
python scripts/paper_trade.py         # 本地模拟盘（T-1 信号 T 日开盘成交）
python scripts/paper_trade.py --reversal   # 反转因子模拟盘（42 只池）
python scripts/predictive_power.py    # 事件预测力回看
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
