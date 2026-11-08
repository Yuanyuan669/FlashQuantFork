"""历史新闻回填 + 重建情绪历史。

为新闻池（config.symbols + news.per_symbol_universe，通常 100+ 只）回填可回填的历史：
- A股：东方财富个股新闻分页（免 key，实测接口只给最近 ~1000 条，时间跨度约 2 个月）
       + 东方财富公告分页（免 key，可翻十数年，是 A股历史深度的真正来源）
- 美股：SEC EDGAR 8-K（免 key）+ Finnhub company-news（需 FINNHUB_API_KEY，可选）

回填结果归档到 news/raw/{date}.jsonl，随后从归档重建 news/sentiment_history.json，
供 scripts/sentiment_score.py 消费，让回测用上真实历史情绪。

用法（项目根目录）：
    python scripts/backfill_news.py                      # 回填新闻池全部标的并重建历史
    python scripts/backfill_news.py --market cn          # 只回填 A股
    python scripts/backfill_news.py --source ann         # 只回填公告（深度优先）
    python scripts/backfill_news.py --code 600519,000858 # 只回填指定代码
    python scripts/backfill_news.py --no-rebuild         # 只回填归档，不重建历史

每 --batch 只归档一次（默认 10），中途断掉也能保留已抓到的数据；重复跑幂等。

公告正文不在这里拉（列表接口 content 恒为空）：回填后跑
`scripts/enrich_ann_bodies.py --write` 按 column_name 选择性补正文并重打分。
"""

import argparse
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from news_aggregator.backfill import (  # noqa: E402
    archive,
    compact_ymd,
    fetch_em_announcements,
    fetch_em_stock_news,
    fetch_finnhub_news,
    fetch_sec_8k,
)
from news_aggregator.fetchers import load_news_universe  # noqa: E402


def load_config() -> dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_env_file(env_path) -> None:
    """把 KEY=VALUE 行载入 os.environ（已有环境变量优先，不覆盖）。"""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k and not os.environ.get(k):
                    os.environ[k] = v
    except FileNotFoundError:
        pass


def finnhub_token(cfg: dict) -> str:
    """读取 Finnhub key：config.primary.finnhub_api_key -> 环境变量 FINNHUB_API_KEY。"""
    return (str((cfg.get("primary") or {}).get("finnhub_api_key") or "").strip()
            or os.environ.get("FINNHUB_API_KEY", "").strip())


def dedupe(items: list) -> list:
    """按 id 去重；同一条被多只股票命中时合并 symbols，而不是丢弃。"""
    uniq = {}
    for it in items:
        if it["id"] in uniq:
            merged = set(uniq[it["id"]].get("symbols") or [])
            merged.update(it.get("symbols") or [])
            uniq[it["id"]]["symbols"] = sorted(merged)
        else:
            uniq[it["id"]] = it
    return list(uniq.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["cn", "us"], default=None, help="只回填某个市场")
    ap.add_argument("--code", default="", help="只回填这些代码（逗号分隔）")
    ap.add_argument("--source", choices=["all", "news", "ann"], default="all",
                    help="all=新闻+公告（美股为 8-K+Finnhub）；ann=只回填 A股公告")
    ap.add_argument("--batch", type=int, default=10, help="每 N 只归档一次（断点续跑用）")
    ap.add_argument("--news-pages", type=int, default=30)
    ap.add_argument("--ann-pages", type=int, default=60)
    ap.add_argument("--no-rebuild", action="store_true", help="只归档，不重建情绪历史")
    args = ap.parse_args()

    load_env_file(ROOT / ".env")
    cfg = load_config()
    start_ymd = compact_ymd(cfg["data"]["start_date"])
    end_ymd = compact_ymd(cfg["data"]["end_date"])
    token = finnhub_token(cfg)

    universe = load_news_universe(cfg)
    if args.market:
        universe = [s for s in universe if (s.get("market") or "cn") == args.market]
    if args.code:
        want = {c.strip() for c in args.code.split(",") if c.strip()}
        universe = [s for s in universe
                    if str(s.get("symbol") or "") in want or str(s.get("code") or "") in want]
    if not universe:
        print("[backfill] 新闻池为空（检查 config.symbols / news.per_symbol_universe）")
        return 1

    print(f"[backfill] 新闻池 {len(universe)} 只，起始日 {start_ymd}，来源 {args.source}")
    t0 = time.time()
    buf = []
    n_codes = n_items = n_written = 0
    for i, item in enumerate(universe, 1):
        market = item.get("market") or "cn"
        sym = str(item.get("symbol") or item.get("code"))
        code = str(item.get("code") or sym)
        name = str(item.get("name") or code)
        t = time.time()
        items = []
        try:
            if market == "cn":
                if args.source in ("all", "news"):
                    items += fetch_em_stock_news(name, sym, start_ymd, max_pages=args.news_pages)
                if args.source in ("all", "ann"):
                    items += fetch_em_announcements(code, sym, start_ymd, max_pages=args.ann_pages)
            else:
                items += fetch_sec_8k(sym, start_ymd)
                if args.source in ("all", "news"):
                    items += fetch_finnhub_news(sym, start_ymd, end_ymd, token)
        except Exception as e:  # noqa: BLE001  单只失败不中断整批
            print(f"[{i}/{len(universe)}] {name}({sym}) 失败 {type(e).__name__}: {e}")
            continue
        buf.extend(items)
        n_codes += 1
        n_items += len(items)
        eta = (time.time() - t0) / i * (len(universe) - i)
        print(f"[{i}/{len(universe)}] {market} {name}({sym}) {len(items)} 条 "
              f"{time.time() - t:.0f}s | 累计 {n_items} 条，ETA {eta / 60:.0f} 分钟")
        if i % max(args.batch, 1) == 0:
            n_written += archive(dedupe(buf))
            print(f"  -> 已归档（本批去重后 {len(dedupe(buf))} 条）")
            buf = []

    if buf:
        n_written += archive(dedupe(buf))
        buf = []

    print(f"[backfill] 完成：{n_codes}/{len(universe)} 只，抓到 {n_items} 条，"
          f"归档新增 {n_written} 条，用时 {(time.time() - t0) / 60:.1f} 分钟")

    if not args.no_rebuild:
        from news_aggregator.run import rebuild_history_from_raw, save_history  # noqa: E402
        h = rebuild_history_from_raw()
        save_history(h)
        print(f"[backfill] 已重建历史情绪：市场 {len(h.get('market', {}))} 天 / "
              f"个股 {len(h.get('symbols', {}))} 只")
    return 0


if __name__ == "__main__":
    sys.exit(main())
