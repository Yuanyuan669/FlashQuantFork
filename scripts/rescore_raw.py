"""raw 全量重打分：英文 FinBERT（含量纲对齐）、重算影响分，回写 raw。

背景：FinBERT 上线前的历史英文条目情绪分是词典值（大量 0），影响分失真，
top 榜被高量纲的英文源霸占。本脚本用【当前后端 + 当前权重】对全部 raw
重新打分并回写，随后自动重建情绪历史。

用法：
    .venv/bin/python scripts/rescore_raw.py            # 全量
    .venv/bin/python scripts/rescore_raw.py --days 14  # 只近 N 天

注意：
- FinBERT CPU ~5-10 条/秒，全量（~4 万条、英文约 1/3）约 10-20 分钟
- 确定性打分，重复执行结果一致；中断后重跑安全
- 完成后自动执行 run.py --rebuild-history 重建情绪历史
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from news_aggregator.impact import compute_impact  # noqa: E402
from news_aggregator.sentiment import configure_backend  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="raw 全量重打分")
    ap.add_argument("--days", type=int, default=0, help="只处理近 N 天（0=全量）")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    configure_backend(cfg)
    imp_cfg = cfg.get("impact") or {}
    weights = imp_cfg.get("weights") or None
    window = int(imp_cfg.get("window_minutes", 60))
    burst_cap = int(imp_cfg.get("burst_cap", 4))
    themes_path = ROOT / (imp_cfg.get("themes_path") or "news_aggregator/themes.yaml")
    themes = (yaml.safe_load(themes_path.read_text(encoding="utf-8")) or {}).get("themes") or []

    raw_dir = ROOT / "news" / "raw"
    files = sorted(raw_dir.glob("*.jsonl"))
    if args.days > 0:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        keep = {(today - timedelta(days=o)).strftime("%Y%m%d") for o in range(args.days)}
        files = [p for p in files if p.stem in keep]
    print(f"[rescore] 待处理 {len(files)} 个日文件 | 后端: "
          f"{(cfg.get('sentiment') or {}).get('backend')}")

    total = rescored = 0
    for k, p in enumerate(files, 1):
        lines = [l for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
                 if l.strip().startswith("{")]
        items = []
        for l in lines:
            try:
                items.append(json.loads(l))
            except json.JSONDecodeError:
                continue
        if not items:
            continue
        total += len(items)
        out = compute_impact(items, themes, weights, window, burst_cap)
        new_text = "\n".join(json.dumps(it, ensure_ascii=False) for it in out) + "\n"
        if p.read_text(encoding="utf-8", errors="replace") != new_text:
            p.write_text(new_text, encoding="utf-8")
            rescored += len(out)
        if k % 25 == 0 or k == len(files):
            print(f"[rescore] 进度 {k}/{len(files)} 文件 | 累计 {total} 条 | "
                  f"重写 {rescored} 条", flush=True)

    print(f"[rescore] 完成：{total} 条 / 重写 {rescored} 条")

    from news_aggregator.run import rebuild_history_from_raw, save_history  # noqa: E402
    h = rebuild_history_from_raw()
    save_history(h)
    print(f"[rescore] 情绪历史已重建：市场 {len(h.get('market', {}))} 天 / "
          f"个股 {len(h.get('symbols', {}))} 只")
    return 0


if __name__ == "__main__":
    sys.exit(main())
