"""定向重打 2026-10-14 被固化为 0.0 的东财新闻/公告情绪分。

背景（git 已证实）
------------------
提交 ac867c5（flashquant-rescore, 2026-10-14 12:21:14 +0800）用 `--reweight`
跑 rescore_raw.py。该路径走 `float(it.get("sentiment") or 0.0)`，于是
「原本没有 sentiment 字段」和「情绪真的为 0」被混为一谈，3,569 条缺字段的
条目被写成 0.0 并随脚本自动 push 固化。其中东方财富个股新闻 1,810 +
东方财富公告 498。

判据：这两个来源在当前语料里 100% 为 0.0（1,812/1,812、498/498）。
一次真实重打分不可能产生 100% 零率（v1 词典在该来源实测零率 46.9%）。

本脚本做什么
------------
1. 只重算 source ∈ {东方财富个股新闻, 东方财富公告} 的 sentiment，
   用当前后端（zh-v2.1）从 title+content 打分，并把 title 单独传入以启用
   标题优先降权（公告的 content 恒为空，实际等于只按标题打分）。
2. 对受影响的整天文件跑 compute_impact(use_stored_scores=True)，
   刷新 intensity / impact（它们也被那次提交一并写成了 0.0）。
3. 重建 news/sentiment_history.json。

刻意不做什么
------------
不动其他来源的分。全语料统一到 v2.1 是另一个决定（会重写 11.8 万条、
并抹掉过往决策的可复现性），不在本次授权范围内。

不做 git 操作——提交与推送由调用方显式控制。
"""
import argparse
import json
import pathlib
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RAW_DIR = ROOT / "news" / "raw"
TARGET_SOURCES = {"东方财富个股新闻", "东方财富公告"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    ap.add_argument("--no-impact", action="store_true", help="跳过 impact 重算")
    args = ap.parse_args()

    import yaml

    from news_aggregator.sentiment import (score_text, LEXICON_VERSION,
                                           configure_backend)
    from news_aggregator.impact import compute_impact

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    configure_backend(cfg)
    backend = str((cfg.get("sentiment") or {}).get("backend") or "lexicon")
    imp_cfg = cfg.get("impact") or {}
    weights = imp_cfg.get("weights") or None
    window = int(imp_cfg.get("window_minutes", 60))
    burst_cap = int(imp_cfg.get("burst_cap", 4))
    themes_path = ROOT / (imp_cfg.get("themes_path") or "news_aggregator/themes.yaml")
    themes = (yaml.safe_load(themes_path.read_text(encoding="utf-8")) or {}).get("themes") or []
    print(f"[rescore-em] 词典 {LEXICON_VERSION} | backend={backend} | "
          f"themes={len(themes)} | dry_run={args.dry_run}")

    files = sorted(RAW_DIR.glob("*.jsonl"))
    n_seen = n_rescored = n_still_zero = 0
    n_impact_written = 0
    files_touched = 0
    dist = Counter()

    for p in files:
        raw = p.read_text(encoding="utf-8", errors="replace")
        # 用 split("\n") 而非 splitlines()：后者还会在 \x0b\x0c\x1c\x1d\x1e\x85
        # \u2028\u2029 上断行，会把含这些字符的单条 JSON 切成碎片、误判为坏行
        # （实测 20261102/20261103 各被切出 1~2 个假坏行，逐行读则 0 坏行）。
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
            print(f"  [warn] {p.name} 有 {bad} 行无法解析，整个文件跳过（不写盘）")
            continue

        touched = False
        for it in items:
            if (it.get("source") or "") not in TARGET_SOURCES:
                continue
            n_seen += 1
            title = str(it.get("title", "") or "")
            new = round(float(score_text(f"{title} {it.get('content', '') or ''}",
                                        title)), 4)
            old = it.get("sentiment")
            if old is None or abs(float(old) - new) > 1e-9:
                it["sentiment"] = new
                n_rescored += 1
                touched = True
            if abs(new) < 1e-9:
                n_still_zero += 1
            dist[round(new, 1)] += 1

        if not touched:
            continue
        files_touched += 1

        if not args.no_impact:
            # 只回写目标条目，且不重排序：
            # compute_impact 会返回按 impact 降序的**全部**条目，直接写回会
            # (a) 顺带改掉非目标条目的 impact——实测抽样 12 文件 4327 条里有
            #     172 条（4.0%）因配置/公式漂移而变值，超出本次授权范围；
            # (b) 打乱整个日文件的行序，让 diff 无法审阅。
            # 所以这里借 _i 做索引映射，只把目标条目的三个派生字段拷回来。
            try:
                tagged = []
                for i, it in enumerate(items):
                    c = dict(it)
                    c["_i"] = i
                    tagged.append(c)
                out = compute_impact(tagged, themes, weights, window, burst_cap,
                                     use_stored_scores=True)
                back = {c["_i"]: c for c in out}
                n_imp = 0
                for i, it in enumerate(items):
                    if (it.get("source") or "") not in TARGET_SOURCES:
                        continue
                    r = back.get(i)
                    if r is None:
                        continue
                    it["sentiment"] = r["sentiment"]
                    it["impact"] = r["impact"]
                    it["impact_parts"] = r["impact_parts"]
                    n_imp += 1
                n_impact_written += n_imp
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] {p.name} impact 重算失败 {type(e).__name__}: {e}")

        if not args.dry_run:
            p.write_text("\n".join(json.dumps(it, ensure_ascii=False)
                                   for it in items) + "\n", encoding="utf-8")

    print(f"\n[rescore-em] 目标来源条目 {n_seen} 条，分布在 {files_touched} 个日文件")
    print(f"[rescore-em] 分数发生变化 {n_rescored} 条 | 重算后仍为 0.0 的 "
          f"{n_still_zero} 条（{100*n_still_zero/max(n_seen,1):.1f}%）")
    print(f"[rescore-em] 同步回写 impact/impact_parts 的目标条目 {n_impact_written} 条；"
          f"非目标条目与原文件行序均未改动")
    if args.dry_run:
        print("[rescore-em] dry-run：未写盘")

    print("\n[分布] 新分直方图（0.1 一档）")
    for k in sorted(dist):
        print(f"  {k:+.1f}  {'#' * min(dist[k], 60)} {dist[k]}")

    if args.dry_run:
        return 0

    from news_aggregator.run import rebuild_history_from_raw, save_history
    h = rebuild_history_from_raw()
    save_history(h)
    print(f"\n[rescore-em] 情绪历史已重建：市场 {len(h.get('market', {}))} 天 / "
          f"个股 {len(h.get('symbols', {}))} 只")
    return 0


if __name__ == "__main__":
    sys.exit(main())
