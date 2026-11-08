"""把历史 raw 里的市场级快讯归属到个股（实体链接回填）。

只改 symbols / symbols_body / symbols_linked 三个字段，不动 sentiment——
情绪与归属是两件独立的事，混在一起会让 diff 无法审阅。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/link_entities.py            # 只统计
    .venv/Scripts/python.exe scripts/link_entities.py --write    # 写盘 + 重建历史
"""
import argparse
import glob
import json
import pathlib
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news_aggregator.entity_link import link_items, stats_line  # noqa: E402

RAW_DIR = ROOT / "news" / "raw"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写盘并重建情绪历史")
    args = ap.parse_args()

    files = sorted(glob.glob(str(RAW_DIR / "*.jsonl")))
    tot = changed = 0
    new_codes = Counter()      # 新增的 symbol 归属次数
    pool_gain = Counter()      # 池内代码的（symbol,date）单元格增益
    codes_before = Counter()
    n_title = n_body = 0
    last_st = {}
    uni_path = ROOT / "data" / "universe_tiers.yaml"
    import re
    uni = set(re.findall(r"symbol:\s*'?(\d{6})'?",
                         uni_path.read_text(encoding="utf-8")))

    for p in files:
        day = pathlib.Path(p).stem
        raw = pathlib.Path(p).read_text(encoding="utf-8", errors="replace")
        lines = [l for l in raw.split("\n") if l.strip().startswith("{")]
        items, bad = [], 0
        for l in lines:
            try:
                items.append(json.loads(l))
            except json.JSONDecodeError:
                bad += 1
        if not items:
            continue
        if bad:
            print(f"  [warn] {p} 有 {bad} 行无法解析，跳过")
            continue

        for it in items:
            for s in (it.get("symbols") or []):
                codes_before[s] += 1
        st = link_items(items)
        last_st = st
        n_title += st["n_title"]
        n_body += st["n_body"]

        touched = False
        for it in items:
            extra = it.get("symbols_linked") or []
            if extra:
                changed += 1
                touched = True
                for s in extra:
                    new_codes[s] += 1
                    if s in uni:
                        pool_gain[(s, day)] += 1
        tot += len(items)

        if touched and args.write:
            pathlib.Path(p).write_text(
                "\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
                encoding="utf-8")

    print(stats_line({"n_items": tot, "n_title": n_title, "n_body": n_body,
                      "n_ambiguous": last_st.get("n_ambiguous", 0)}))
    print(f"[entity-link] 历史回填：扫描 {tot} 条，新增归属 {changed} 条")
    print(f"[entity-link] 新增代码 {len(new_codes)} 个，其中池内 "
          f"{len([c for c in new_codes if c in uni])} 个")
    print(f"[entity-link] 池内新增 (symbol,date) 单元格 {len(pool_gain)} 个，"
          f"涉及 {len({s for s, _ in pool_gain})} 只票")
    if pool_gain:
        top = Counter()
        for (s, _), v in pool_gain.items():
            top[s] += 1
        print(f"[entity-link] 收益最大: {top.most_common(10)}")
    print(f"[entity-link] 提及最多的池外代码（值得加入 universe 的信号）: "
          f"{[(c, n) for c, n in new_codes.most_common(15) if c not in uni][:10]}")

    if not args.write:
        print("[entity-link] 未写盘（加 --write 生效）")
        return 0

    from news_aggregator.run import rebuild_history_from_raw, save_history
    h = rebuild_history_from_raw()
    save_history(h)
    print(f"[entity-link] 情绪历史已重建：市场 {len(h.get('market', {}))} 天 / "
          f"个股 {len(h.get('symbols', {}))} 只")
    return 0


if __name__ == "__main__":
    sys.exit(main())
