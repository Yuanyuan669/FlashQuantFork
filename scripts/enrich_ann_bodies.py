"""给已归档的东财公告按需补正文（#12）。

列表接口 content 恒为空，情绪只按标题打，丢失了业绩/回购金额/增减持数量这些
真正能改变判断的信息。本脚本对存量 news/raw/*.jsonl 里的公告逐条判断：
`ann_body.wants_body(column_name, title)` 为真才调正文接口，拉到后
重新打情绪分并同步刷新 impact，最后重建 sentiment_history.json。

特性
----
- 默认 dry-run，只统计候选量与分类，不写盘；`--write` 才执行；
- 逐日文件处理，每完成一个文件立即落盘（原子替换），中断可续跑；
- 已有 content 的条目跳过；拉取失败不标记，下次会重试；
- 只改目标条目的 sentiment/impact/impact_parts，不重排、不动其他来源。

用法（项目根目录）：
    python scripts/enrich_ann_bodies.py                        # 预估
    python scripts/enrich_ann_bodies.py --write --limit 50     # 小批量试跑
    python scripts/enrich_ann_bodies.py --write                # 全量
    python scripts/enrich_ann_bodies.py --write --code 300750  # 限定个股
"""

import argparse
import json
import os
import pathlib
import sys
import time
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RAW_DIR = ROOT / "news" / "raw"
ANN_SOURCE = "东方财富公告"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="实际拉正文并写盘")
    ap.add_argument("--limit", type=int, default=0, help="最多拉多少条正文（0=不限）")
    ap.add_argument("--code", default="", help="只处理这些股票代码（逗号分隔）")
    ap.add_argument("--sleep", type=float, default=0.6, help="每次请求间隔秒数")
    ap.add_argument("--no-rebuild", action="store_true", help="不重建情绪历史")
    args = ap.parse_args()

    import yaml

    from news_aggregator.ann_body import art_code_of, fetch_body, wants_body
    from news_aggregator.impact import compute_impact
    from news_aggregator.sentiment import LEXICON_VERSION, configure_backend, score_text

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    configure_backend(cfg)
    imp_cfg = cfg.get("impact") or {}
    weights = imp_cfg.get("weights") or None
    window = int(imp_cfg.get("window_minutes", 60))
    burst_cap = int(imp_cfg.get("burst_cap", 4))
    themes_path = ROOT / (imp_cfg.get("themes_path") or "news_aggregator/themes.yaml")
    themes = (yaml.safe_load(themes_path.read_text(encoding="utf-8")) or {}).get("themes") or []
    codes = {c.strip() for c in args.code.split(",") if c.strip()}

    print(f"[ann-body] 词典 {LEXICON_VERSION} | write={args.write} | "
          f"limit={args.limit or '∞'} | sleep={args.sleep}s")

    n_seen = n_cand = n_fetched = n_empty = 0
    n_chars = 0
    files_touched = 0
    by_col = Counter()
    skipped_col = Counter()
    t0 = time.time()

    for p in sorted(RAW_DIR.glob("*.jsonl")):
        raw = p.read_text(encoding="utf-8", errors="replace")
        # 用 split("\n")：splitlines() 会在 \x0b\x0c 等字符上断行，切碎 JSON
        lines = [l for l in raw.split("\n") if l.strip().startswith("{")]
        items, bad = [], 0
        for l in lines:
            try:
                items.append(json.loads(l))
            except json.JSONDecodeError:
                bad += 1
        if not items or bad:
            if bad:
                print(f"  [warn] {p.name} 有 {bad} 行无法解析，跳过")
            continue

        changed = False
        for it in items:
            if (it.get("source") or "") != ANN_SOURCE:
                continue
            if codes and not (set(it.get("symbols") or []) & codes):
                continue
            n_seen += 1
            if str(it.get("content") or "").strip():
                continue
            col = str(it.get("column_name") or "")
            title = str(it.get("title") or "")
            if not wants_body(col, title):
                skipped_col[col or "(无分类)"] += 1
                continue
            art = art_code_of(it)
            if not art:
                continue
            n_cand += 1
            if args.limit and n_fetched >= args.limit:
                break
            if not args.write:  # dry-run 只统计
                by_col[col or "(无分类)"] += 1
                continue
            txt = fetch_body(art)
            time.sleep(args.sleep)
            if not txt:
                n_empty += 1
                continue
            it["content"] = txt
            it["content_source"] = "em_cnotice"
            it["sentiment"] = round(float(score_text(f"{title} {txt}", title)), 4)
            n_fetched += 1
            n_chars += len(txt)
            by_col[col or "(无分类)"] += 1
            changed = True
            if n_fetched % 25 == 0:
                rate = n_fetched / max(time.time() - t0, 1e-6)
                print(f"  ... 已拉 {n_fetched} 条（{rate * 60:.0f} 条/分钟）"
                      f" 当前 {p.name} {title[:28]}")

        if not changed or not args.write:
            continue
        # impact 复算：只把目标条目的派生字段拷回，避免顺带改别的来源、打乱行序
        try:
            tagged = []
            for i, it in enumerate(items):
                c = dict(it)
                c["_i"] = i
                tagged.append(c)
            out = compute_impact(tagged, themes, weights, window, burst_cap,
                                 use_stored_scores=True)
            back = {c["_i"]: c for c in out}
            for i, it in enumerate(items):
                if (it.get("source") or "") != ANN_SOURCE or not it.get("content_source"):
                    continue
                r = back.get(i)
                if r is not None:
                    it["impact"] = r["impact"]
                    it["impact_parts"] = r["impact_parts"]
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] {p.name} impact 复算失败 {type(e).__name__}: {e}")

        tmp = p.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n",
                       encoding="utf-8")
        os.replace(tmp, p)
        files_touched += 1

        if args.limit and n_fetched >= args.limit:
            print(f"[ann-body] 达到 --limit {args.limit}，停止")
            break

    print(f"\n[ann-body] 公告 {n_seen} 条 | 候选 {n_cand} | 拉到正文 {n_fetched} 条 "
          f"| 空/失败 {n_empty} | 平均 {n_chars / max(n_fetched, 1):.0f} 字 "
          f"| 用时 {(time.time() - t0) / 60:.1f} 分钟")
    print(f"[ann-body] 地址条目 {len(by_col)} 类，top: {by_col.most_common(8)}")
    print(f"[ann-body] 按分类跳过 {sum(skipped_col.values())} 条，top: "
          f"{skipped_col.most_common(8)}")
    if not args.write:
        print("[ann-body] dry-run：未写盘；加 --write 执行")
        return 0

    print(f"[ann-body] 写盘文件 {files_touched} 个")
    if not args.no_rebuild:
        from news_aggregator.run import rebuild_history_from_raw, save_history
        h = rebuild_history_from_raw()
        save_history(h)
        print(f"[ann-body] 情绪历史已重建：市场 {len(h.get('market', {}))} 天 / "
              f"个股 {len(h.get('symbols', {}))} 只")
    return 0


if __name__ == "__main__":
    sys.exit(main())
