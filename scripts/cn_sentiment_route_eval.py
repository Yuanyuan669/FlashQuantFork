#!/usr/bin/env python
"""离线复演「发报策略」：同一批逐条预测上，比全词典 / 全模型 / 置信度门控 /
一致才发报+分歧送人工 / 按分层路由。

不训练、不改线上后端。输入是 cn_sentiment_domain_cv.py 或 cn_sentiment_holdout.py 的
--dump-rows 产物。新 dump 每条带 probs=[P(利空), P(中性), P(利好)]；老 dump 没有 probs，
只跑不依赖置信度的策略并提示。

口径提醒：
    - 冷启动 dump（只用 410 条训练）是「没见过的新域」，决策看它；
    - 合并 581 的 OOF dump 里，bear 子集在别的 bear 日期上训练过，偏乐观，只能当
      「持续标注以后」的参考，不能当线上预期。

用法：
    python scripts/cn_sentiment_route_eval.py --rows D:/fq-data/holdout_bear180_rows.jsonl
    python scripts/cn_sentiment_route_eval.py --rows D:/fq-data/domain_cv_581_rows.jsonl \
        --where sample=base450
"""
import argparse
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if hasattr(sys.stdout, "reconfigure"):        # Windows 管道下默认 cp936，中文会乱码
    sys.stdout.reconfigure(encoding="utf-8")

LAB = {0: "利空", 1: "中性", 2: "利好"}


def load_rows(paths, where):
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if int(r.get("gold", -1)) < 0:
                    continue
                if where and any(str(r.get(k)) != v for k, v in where.items()):
                    continue
                rows.append(r)
    return rows


def has_probs(rows):
    return all(isinstance(r.get("probs"), list) and len(r["probs"]) == 3 for r in rows)


def pmax(r):
    return max(r["probs"])


def eval_policy(name, rows, preds):
    """preds 里的 None 表示送人工、不自动发报。指标只在自动发报的子集上算。"""
    gold = [r["gold"] for r in rows]
    decided = [(g, p) for g, p in zip(gold, preds) if p is not None]
    n, nd = len(rows), len(decided)
    acc = sum(1 for g, p in decided if g == p) / nd if nd else 0.0
    bear = [(g, p) for g, p in decided if g == 0]
    bear_r = sum(1 for g, p in bear if p == 0) / len(bear) if bear else 0.0
    pred_bear = [(g, p) for g, p in decided if p == 0]
    bear_p = sum(1 for g, p in pred_bear if g == 0) / len(pred_bear) if pred_bear else 0.0
    neu = [(g, p) for g, p in decided if g == 1]
    leak = sum(1 for g, p in neu if p != 1) / len(neu) if neu else 0.0
    queued = [r for r, p in zip(rows, preds) if p is None]
    q_dir = sum(1 for r in queued if r["gold"] != 1) / len(queued) if queued else 0.0
    return {"name": name, "n": n, "cov": nd / n if n else 0.0, "acc": acc,
            "bear_r": bear_r, "bear_p": bear_p, "leak": leak,
            "queue": len(queued), "queue_dir": q_dir}


def line(r):
    return ("{:<28} n={:>4} 覆盖{:>6.1%} acc{:.3f} 利空R{:.3f} P{:.3f} "
            "泄漏{:.3f} 人工{:>3}({}%)").format(
        r["name"], r["n"], r["cov"], r["acc"], r["bear_r"], r["bear_p"],
        r["leak"], r["queue"], "--" if not r["n"] else "{:.0f}".format(r["queue_dir"] * 100))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", nargs="+", required=True, help="dump-rows 产物，可给多个")
    ap.add_argument("--where", nargs="*", default=[],
                    help="过滤条件，如 sample=base450 / stratum=公告")
    ap.add_argument("--gates", nargs="*", type=float,
                    default=[0.50, 0.60, 0.70, 0.80, 0.90, 0.95],
                    help="置信度门控阈值（模型 max-prob 低于它时改走词典）")
    args = ap.parse_args()

    where = {}
    for kv in args.where:
        k, _, v = kv.partition("=")
        where[k.strip()] = v.strip()

    rows = load_rows(args.rows, where)
    if not rows:
        raise SystemExit("没有可用行（gold<0 或被 --where 过滤掉了）")
    probs_ok = has_probs(rows)
    n = len(rows)
    print("样本 {} 条{}".format(n, "" if not where else "（过滤 {}）".format(where)))
    print("标签分布 " + "  ".join(
        "{}={}".format(LAB[g], c) for g, c in sorted(Counter(r["gold"] for r in rows).items())))
    print("模型概率 " + ("有" if probs_ok else "无（老 dump，跳过置信度门控）"))

    model = [int(r["model"]) for r in rows]
    lex = [int(r["lex"]) for r in rows]

    print("\n== 1. 基线 ==")
    print(line(eval_policy("全词典", rows, lex)))
    print(line(eval_policy("全模型", rows, model)))

    print("\n== 2. 按分层（acc / 利空召回）==")
    strata = sorted(set(r.get("stratum") or "其他" for r in rows))
    for s in strata:
        sub = [r for r in rows if (r.get("stratum") or "其他") == s]
        gm = [r["gold"] for r in sub]
        both = [eval_policy("m", sub, [int(r["model"]) for r in sub]),
                eval_policy("l", sub, [int(r["lex"]) for r in sub])]
        print("{:<6} n={:>4}  模型 acc{:.3f} 利空R{:.3f} | 词典 acc{:.3f} 利空R{:.3f}".format(
            s, len(sub), both[0]["acc"], both[0]["bear_r"], both[1]["acc"], both[1]["bear_r"]))

    print("\n== 3. 一致 / 分歧 ==")
    agree = [(r, int(r["model"])) for r in rows if int(r["model"]) == int(r["lex"])]
    dis = [r for r in rows if int(r["model"]) != int(r["lex"])]
    if agree:
        print("一致 {} 条（{:.1%}）：一致时正确 {:.3f}".format(
            len(agree), len(agree) / n,
            sum(1 for r, p in agree if r["gold"] == p) / len(agree)))
    if dis:
        m_right = sum(1 for r in dis if int(r["model"]) == r["gold"]) / len(dis)
        l_right = sum(1 for r in dis if int(r["lex"]) == r["gold"]) / len(dis)
        both_wrong = sum(1 for r in dis if r["gold"] != int(r["model"])
                         and r["gold"] != int(r["lex"]))
        print("分歧 {} 条（{:.1%}）：模型对 {:.3f}，词典对 {:.3f}，都错 {}".format(
            len(dis), len(dis) / n, m_right, l_right, both_wrong))
        if probs_ok:
            hi = [r for r in dis if pmax(r) >= 0.9]
            if hi:
                print("  分歧且模型 pmax>=0.9 的 {} 条：模型对 {:.3f}".format(
                    len(hi), sum(1 for r in hi if int(r["model"]) == r["gold"]) / len(hi)))

    if probs_ok:
        print("\n== 4. 置信度门控：模型 pmax>=T → 模型，否则词典 ==")
        for t in args.gates:
            preds = [int(r["model"]) if pmax(r) >= t else int(r["lex"]) for r in rows]
            print(line(eval_policy("gate T={:.2f}".format(t), rows, preds)))
        print("\n== 5. 置信度门控（低置信送人工，不自动发报）==")
        for t in args.gates:
            preds = [int(r["model"]) if pmax(r) >= t else None for r in rows]
            print(line(eval_policy("gate+queue T={:.2f}".format(t), rows, preds)))

    print("\n== 6. 一致才发报，分歧送人工 ==")
    preds = [int(r["model"]) if int(r["model"]) == int(r["lex"]) else None for r in rows]
    print(line(eval_policy("agree-only", rows, preds)))

    print("\n== 7. 按分层路由 ==")
    for lbl, rule in (("公告退词典、其余模型", lambda r: int(r["lex"]) if (r.get("stratum") or "") == "公告" else int(r["model"])),
                      ("公告判中性、其余模型", lambda r: 1 if (r.get("stratum") or "") == "公告" else int(r["model"])),
                      ("公告退词典、个股模型、其余词典", lambda r: int(r["model"]) if (r.get("stratum") or "") == "个股" else int(r["lex"]))):
        print(line(eval_policy(lbl, rows, [rule(r) for r in rows])))

    print("\n== 8. 上限与下界 ==")
    oracle = sum(1 for r in rows if r["gold"] == int(r["model"]) or r["gold"] == int(r["lex"]))
    both_wrong = sum(1 for r in rows if r["gold"] != int(r["model"]) and r["gold"] != int(r["lex"]))
    print("任一方对（不可达上限）{}/{} = {:.3f}；两方都错 {} 条".format(
        oracle, n, oracle / n, both_wrong))


if __name__ == "__main__":
    main()
