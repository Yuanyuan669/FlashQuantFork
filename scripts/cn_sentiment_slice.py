#!/usr/bin/env python
"""把 --dump-rows 产出的逐条预测按来源子集切开重新出表。

为什么需要它：一旦把不同抽样方式的标注集合并（比如自然分布的 410 条 + 利空富集的 180 条），
整体 acc/macro-F1 就是两种抽样比例的加权平均，没有解释力。只有按子集切开、各自出表，
才看得清「新数据帮没帮上忙」。

分组键：
    默认按 `--by sample`（行里的 sample 字段，缺省记为 450），
    `--by neg_hint` 可切成候选/对照两份。

用法：
    python scripts/cn_sentiment_slice.py --rows D:/fq-data/tmp/domain_cv_581_date_rows.jsonl
    python scripts/cn_sentiment_slice.py --rows ... --by neg_hint
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from cn_sentiment_eval import mcnemar, report  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True, help="逐条预测 jsonl（dump-rows 产物）")
    ap.add_argument("--by", choices=("sample", "neg_hint"), default="sample")
    ap.add_argument("--keys", default="", help="逗号分隔的子集名，只报这些；空=全部")
    args = ap.parse_args()

    rows = []
    with open(args.rows, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    groups = {}
    for r in rows:
        if args.by == "neg_hint":
            key = "候选子集" if r.get("neg_hint") else "对照子集"
        else:
            key = r.get("sample") or "450自然分布"
        groups.setdefault(key, []).append(r)

    want = [k.strip() for k in args.keys.split(",") if k.strip()]
    log = lambda s: print(s, flush=True)
    for key, grp in groups.items():
        if want and key not in want:
            continue
        gold = [int(r["gold"]) for r in grp]
        mod = [int(r["model"]) for r in grp]
        lex = [int(r["lex"]) for r in grp]
        log("")
        log(f"==== {key}（n={len(grp)}，可用 {sum(1 for g in gold if g >= 0)}）====")
        r_m = report(f"模型/{key}", gold, mod)
        r_l = report(f"词典/{key}", gold, lex)
        p, b, c = mcnemar([int(x == y) for x, y in zip(mod, gold)],
                          [int(x == y) for x, y in zip(lex, gold)])
        tb = sum(1 for g in gold if g == 0)
        log("{:<14} {:>7} {:>8} {:>9} {:>8} {:>11} {:>6} {:>10}".format(
            "方法", "acc", "macroF1", "中性泄漏", "方向acc", "方向acc条件", "中性率", "利空召回"))
        for r, pred in ((r_m, mod), (r_l, lex)):
            rb = sum(1 for g, q in zip(gold, pred) if g == 0 and q == 0)
            log("{:<14} {:>7.3f} {:>8.3f} {:>9.3f} {:>8.3f} {:>11.3f} {:>6.3f} {:>10}".format(
                r["name"], r["acc"], r["macro_f1"], r["neutral_leak"], r["dir_acc_strict"],
                r["dir_acc_cond"], r["pred_neutral_rate"], f"{rb}/{tb}"))
        log(f"McNemar vs 词典：b_model_only={b} c_lex_only={c} p={p:.4f}")


if __name__ == "__main__":
    main()
