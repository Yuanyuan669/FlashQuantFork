<div align="center">

# ⚡ FlashQuant · 快讯驱动的量化工具箱

**多源新闻聚合 · 秒级事件监测 · 静态舆情 Dashboard**

</div>

---

> 本仓库为 [pipi-520/FlashQuant](https://github.com/pipi-520/FlashQuant) 的 fork。
> 原项目架构与设计归原作者所有，遵循 MIT License；本 fork 的改动同样以 MIT 开源。

## 这是什么

把「数据采集 → 情绪打分 → 影响分排序 → 实时告警 → 量化策略」整条链路串起来的 A 股 + 美股开源量化研究工具，全部本地/云上可跑、免费可复现。

技术架构、模块设计、数据源清单见 **[docs/architecture.md](docs/architecture.md)**。

## 本 Fork 的改动

- **静态舆情 Dashboard**：`run.py` 每日聚合后自动生成零依赖单文件页面（`dashboard/index.html`），KPI / 情绪趋势 / 高影响新闻过滤 / 告警历史，可托管 GitHub Pages
- **社媒热搜源**：新增 6 平台热搜聚合（微博/百度/知乎/抖音/头条/B站），适配器**移植自 [JCP](https://github.com/run-bigpig/jcp) 项目**（原 Go 实现，`internal/services/hottrend`，archived），Python 重写并采用话题稳定 id 设计； 许可声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- **政策公告源修复**：gov.cn RSS 已下线，改用官网 JSON 数据端点（旧 RSS 保留为回退）
- **采集扩围**：个股新闻扩至 102 只分层标的池（大盘/中盘/小盘/微盘，美股接入 Finnhub）
- **FinBERT 情绪后端**：英文混合路由（ProsusAI/finbert + 词典回退），告警增加情绪地板过滤
- **跨平台部署整理**：Linux（systemd / docker / cron）为主目标，Windows 仅开发预览；新增 dashboard 常驻刷新（`--watch`）
- **测试补充**：dashboard 构建器 / URL 归一化 / fetcher 录制回放 / 单元测试 49+

## 快速开始

```bash
python -m venv .venv && .venv/bin/python -m pip install -r requirements.txt

python news_aggregator/run.py               # 聚合 + 日报 + Dashboard
python news_aggregator/monitor.py           # 15s 轮询实时监测（常驻）
python scripts/backtest.py                  # vnpy 回测
python scripts/paper_trade.py               # 本地模拟盘
```

安装细节、24/7 部署（Linux/Windows/Docker）、推送通道配置、回测与模拟盘操作：**[docs/usage.md](docs/usage.md)**。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 技术架构：模块设计、影响分模型、数据源、策略与研究结论 |
| [docs/usage.md](docs/usage.md) | 使用与部署：安装、常驻监测、Dashboard、回测/模拟盘、推送配置 |
| [量化策略说明.md](量化策略说明.md) | vnpy 策略逻辑、参数与模拟盘接入 |

## ⚠️ 免责声明

本项目仅用于**学习与研究**，不构成任何投资建议。股市有风险，入市需谨慎。

## 📄 License

[MIT](LICENSE) · 原项目 © 2026 [pipi-520](https://github.com/pipi-520/FlashQuant)
