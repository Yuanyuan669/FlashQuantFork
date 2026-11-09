#!/usr/bin/env python
"""中文情绪：词典 vs 微调模型的同口径评测（只读，不改线上后端）。

三个标尺：
1) 自有 450 条新闻人工标注（scripts/zh_news_eval_450.jsonl，0利空/1中性/2利好，
   -1 = 存疑，默认剔除）——这是决策标尺。
2) 同一批条目在线上流里的**已存分**（news/raw 的 sentiment 字段）——注意它是
   `title + " " + content` 打出来的，且可能是旧版词典，与人只看标题的口径不一致。
3) FinFE test（1,000 条社媒三分类）——域外参考，用来看模型有没有训崩。

口径说明（重要）：
    人工标注只基于**标题**。因此「词典 vs 人」的公平比较必须让词典也只读标题
    （`score_text_zh(title)`）。本脚本同时输出 title / title+content / 已存分三列，
    差异就是正文信息带来的偏移量，不要混用。

指标（与 450 条标注报告一致）：
    acc / macro-F1 / 逐类 P-R-F1 / 中性泄漏 P(判成有方向 | 人工中性) /
    方向召回 / 方向准确率（分母只算人工有方向的条数，判中性算错）/ 分层 acc。

用法（香橙派，项目 venv）：
    ~/.venvs/flashquant/bin/python scripts/cn_sentiment_eval.py
    ~/.venvs/flashquant/bin/python scripts/cn_sentiment_eval.py \
        --model /home/orangepi/models/cn-news-sent-v1/best --neutral-cv 5

基线参考（2026-11-09 用线上已存分算的旧数，口径为 title+content、词典版本未核）：
    acc .620 / macroF1 .509；本脚本重算后以 title-only 列为准。
"""
import argparse
import glob
import json
import math
import os
import sys
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from news_aggregator.sentiment import LEXICON_VERSION, score_text_zh  # noqa: E402

LAB_CN = {0: "利空", 1: "中性", 2: "利好"}


def load_our(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_jsonl_texts(path):
    texts, gold = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            t = str(r.get("text") or "").strip()
            if t:
                texts.append(t)
                gold.append(int(r["label"]))
    return texts, gold


def index_raw(raw_dir, titles):
    """一次扫描 news/raw，只留目标标题的正文与已存分。返回 title -> (content, stored)。"""
    want = set(titles)
    found = {}
    files = sorted(glob.glob(os.path.join(raw_dir, "*.jsonl")))
    for fp in files:
        try:
            fh = open(fp, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    it = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                t = (it.get("title") or "").strip()
                if t in want and t not in found:
                    found[t] = (it.get("content") or "", it.get("sentiment"))
    return found


def per_class(gold, pred):
    out = {}
    for lab in (0, 1, 2):
        tp = sum(1 for g, p in zip(gold, pred) if g == lab and p == lab)
        fp = sum(1 for g, p in zip(gold, pred) if g != lab and p == lab)
        fn = sum(1 for g, p in zip(gold, pred) if g == lab and p != lab)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        out[LAB_CN[lab]] = {"p": round(prec, 3), "r": round(rec, 3), "f1": round(f1, 3)}
    return out


def report(name, gold, pred):
    n = len(gold)
    conf = {LAB_CN[g]: dict(Counter(LAB_CN[p] for gg, p in zip(gold, pred) if gg == g))
            for g in (0, 1, 2)}
    for g in conf:
        for k in (LAB_CN[0], LAB_CN[1], LAB_CN[2]):
            conf[g].setdefault(k, 0)
    neu = [p for g, p in zip(gold, pred) if g == 1]
    dirn = [(g, p) for g, p in zip(gold, pred) if g != 1]
    dirn_cond = [(g, p) for g, p in dirn if p != 1]
    row = {
        "name": name,
        "n": n,
        "acc": round(sum(1 for g, p in zip(gold, pred) if g == p) / n, 4),
        "macro_f1": round(sum(v["f1"] for v in per_class(gold, pred).values()) / 3, 4),
        "neutral_leak": round(sum(1 for p in neu if p != 1) / len(neu), 4) if neu else 0.0,
        "dir_acc_strict": round(sum(1 for g, p in dirn if g == p) / len(dirn), 4) if dirn else 0.0,
        "dir_acc_cond": round(sum(1 for g, p in dirn_cond if g == p) / len(dirn_cond), 4)
        if dirn_cond else 0.0,
        "dir_n": len(dirn),
        "pred_neutral_rate": round(sum(1 for p in pred if p == 1) / n, 4),
        "zero_rate": None,
        "per_class": per_class(gold, pred),
        "confusion": conf,
    }
    return row


def by_stratum(rows, labels, preds):
    out = {}
    for r, y, p in zip(rows, labels, preds):
        if y < 0:
            continue
        s = r.get("stratum") or "其他"
        d = out.setdefault(s, {"n": 0, "right": 0, "dir_n": 0, "dir_right": 0})
        d["n"] += 1
        d["right"] += int(y == p)
        if y != 1:
            d["dir_n"] += 1
            d["dir_right"] += int(y == p)
    return {k: {"n": v["n"], "acc": round(v["right"] / v["n"], 3),
                "dir_acc": round(v["dir_right"] / v["dir_n"], 3) if v["dir_n"] else 0.0}
            for k, v in out.items()}


def mcnemar(left, right):
    """精确 McNemar：left/right 为两个方法各自的正确性布尔序列。返回 (p, b, c)。"""
    b = sum(1 for x, y in zip(left, right) if x and not y)
    c = sum(1 for x, y in zip(left, right) if not x and y)
    n = b + c
    if n == 0:
        return 1.0, b, c
    k = min(b, c)
    p = 2 * sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, p), b, c


def lex_label(s, band):
    if s is None:
        return 1
    if s > band:
        return 2
    if s < -band:
        return 0
    return 1


def load_predictor(path, device="auto"):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    if device == "auto":
        dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        dev = torch.device(device)
    kw = {"local_files_only": True} if os.path.isdir(path) else {}
    tok = AutoTokenizer.from_pretrained(path, **kw)
    model = AutoModelForSequenceClassification.from_pretrained(path, num_labels=3, **kw)
    model.to(dev).eval()

    def predict(texts, bs=64):
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        logits = [None] * len(texts)
        t0 = time.time()
        with torch.no_grad():
            for k in range(0, len(order), bs):
                idx = order[k: k + bs]
                enc = tok([texts[i] for i in idx], truncation=True, max_length=64,
                          padding=True, return_tensors="pt")
                enc = {kk: vv.to(dev) for kk, vv in enc.items()}
                lg = model(**enc).logits
                for r, i in enumerate(idx):
                    logits[i] = lg[r].float().cpu().tolist()
        return logits, time.time() - t0
    return predict


def neutral_cv(logits, gold, folds):
    """用 K 折在自有标注上拟合「中性偏置」，返回 out-of-fold 预测。

    只调一个阈值 t：logit_neu - max(logit_pos, logit_neg) > t 判中性。
    OOF 结果无泄漏，但它用上了 410 条标签本身，报告时必须单独标注。
    """
    import random
    idx = list(range(len(gold)))
    random.Random(42).shuffle(idx)
    fold_of = {i: n % folds for n, i in enumerate(idx)}
    marg = [l[1] - max(l[0], l[2]) for l in logits]
    raw = [int(max(range(3), key=lambda c: l[c])) for l in logits]
    preds = list(raw)
    for f in range(folds):
        tr = [i for i in range(len(gold)) if fold_of[i] != f]
        te = [i for i in range(len(gold)) if fold_of[i] == f]
        qs = sorted(marg[i] for i in tr)
        cands = sorted({qs[min(len(qs) - 1, int(len(qs) * p / 20))] for p in range(21)})
        best, best_f1 = None, -1.0
        g = [gold[i] for i in tr]
        for t in cands:
            p = [1 if marg[i] > t else raw[i] for i in tr]
            f1 = sum(per_class(g, p)[LAB_CN[k]]["f1"] for k in (0, 1, 2)) / 3
            if f1 > best_f1:
                best_f1, best = f1, t
        for i in te:
            preds[i] = 1 if marg[i] > (best if best is not None else 0.0) else raw[i]
    return preds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--our-jsonl", default=os.path.join(ROOT, "scripts", "zh_news_eval_450.jsonl"))
    ap.add_argument("--raw-dir", default="/home/orangepi/flashquant/news/raw")
    ap.add_argument("--finfe-test", default="/tmp/finfe_test.jsonl")
    ap.add_argument("--model", default="", help="微调产物目录（best/）；留空则只评词典")
    ap.add_argument("--baseline-model", default="",
                    help="可选的第三方基线（如 /home/orangepi/models/hw2942）")
    ap.add_argument("--lex-band", type=float, default=0.0)
    ap.add_argument("--neutral-cv", type=int, default=0,
                    help=">0 时额外输出「模型 + 中性阈值 K 折校准」的 OOF 行")
    ap.add_argument("--skip-raw", action="store_true", help="不扫 news/raw（跳过全文本与已存分两列）")
    ap.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto",
                    help="auto = 有 CUDA 就用 CUDA")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    rows = load_our(args.our_jsonl)
    titles = [r["title"] for r in rows]
    gold = [int(r["label"]) for r in rows]
    keep = [i for i, y in enumerate(gold) if y >= 0]
    log(f"自有标注 {len(rows)} 条，可评 {len(keep)} 条"
        f"（剔除存疑 {len(rows) - len(keep)}）；词典版本 {LEXICON_VERSION}")

    # ---- 词典：标题口径（与人一致） ----
    lex_title_scores = [score_text_zh(t) for t in titles]
    lex_title = [lex_label(s, args.lex_band) for s in lex_title_scores]

    raw = {}
    if not args.skip_raw and os.path.isdir(args.raw_dir):
        t0 = time.time()
        raw = index_raw(args.raw_dir, titles)
        log(f"news/raw 索引：命中 {len(raw)}/{len(titles)}，耗时 {time.time() - t0:.0f}s")
    else:
        log("跳过 news/raw 扫描：只输出标题口径")

    results = []

    def add(row, mask=None, name_suffix="", extras=None):
        if mask is None:
            mask = keep
        g = [gold[i] for i in mask]
        p = [row[i] for i in mask]
        r = report(name_suffix or "row", g, p)
        if extras:
            r.update(extras)
        r["stratum"] = by_stratum([rows[i] for i in mask], g, p)
        results.append(r)
        return r

    add(lex_title, name_suffix=f"词典/标题 (band={args.lex_band})")

    if raw:
        full_scores = [score_text_zh(f"{t} {raw.get(t, ('', None))[0]}") for t in titles]
        add([lex_label(s, args.lex_band) for s in full_scores],
            name_suffix="词典/标题+正文")
        stored_idx = [i for i in keep if raw.get(titles[i], ("", None))[1] is not None]
        add([lex_label(raw.get(t, ("", None))[1], args.lex_band) for t in titles],
            mask=stored_idx, name_suffix="线上已存分(旧口径)")

    # ---- 全判中性基线 ----
    add([1] * len(titles), name_suffix="全判中性")

    # ---- 模型 ----
    def run_model(path, tag):
        try:
            predict = load_predictor(path, args.device)
        except Exception as e:  # noqa: BLE001
            log(f"[warn] 模型不可用 {path}: {type(e).__name__}: {e}")
            return
        logits, dt = predict(titles)
        pred = [int(max(range(3), key=lambda c: l[c])) for l in logits]
        add(pred, name_suffix=f"{tag}/标题",
            extras={"ms_per_item": round(dt * 1000 / len(titles), 1)})
        if raw:
            full_texts = [f"{t} {raw.get(t, ('', None))[0]}" for t in titles]
            logits2, dt2 = predict(full_texts)
            pred2 = [int(max(range(3), key=lambda c: l[c])) for l in logits2]
            add(pred2, name_suffix=f"{tag}/标题+正文",
                extras={"ms_per_item_full": round(dt2 * 1000 / len(titles), 1)})
        if args.neutral_cv > 1:
            k = min(args.neutral_cv, 10)
            oof = neutral_cv(logits, gold, k)
            add(oof, name_suffix=f"{tag}/中性{k}折校准(OOF)")
        if args.finfe_test and os.path.exists(args.finfe_test):
            texts, fgold = load_jsonl_texts(args.finfe_test)
            flogits, fdt = predict(texts)
            fpred = [int(max(range(3), key=lambda c: l[c])) for l in flogits]
            r = report(f"{tag}/FinFE-test", fgold, fpred)
            r["ms_per_item"] = round(fdt * 1000 / len(texts), 1)
            results.append(r)
        # McNemar vs 词典/标题
        base = results[0]
        g = [gold[i] for i in keep]
        base_ok = [int(base_pred == y) for base_pred, y in
                   zip([lex_title[i] for i in keep], g)]
        this_ok = [int(p == y) for p, y in zip([pred[i] for i in keep], g)]
        pval, b, c = mcnemar(this_ok, base_ok)
        base["mcnemar_vs"] = {"method": tag, "b_model_only": b, "c_lex_only": c,
                              "p": round(pval, 4)}

    if args.model:
        run_model(args.model, "微调模型")
    if args.baseline_model:
        run_model(args.baseline_model, "hw2942")

    # ---- 输出 ----
    log("")
    log("{:<30} {:>4} {:>7} {:>8} {:>8} {:>9} {:>11} {:>6}".format(
        "方法", "n", "acc", "macroF1", "中性泄漏", "方向acc", "方向acc条件", "中性率"))
    for r in results:
        log("{:<30} {:>4} {:>7.3f} {:>8.3f} {:>9.3f} {:>9.3f} {:>11.3f} {:>6.3f}".format(
            r["name"], r["n"], r["acc"], r["macro_f1"], r["neutral_leak"],
            r["dir_acc_strict"], r["dir_acc_cond"], r["pred_neutral_rate"]))
    log("  方向acc：分母=人工有方向条数（判中性算错）；方向acc条件：分母=双方都有方向的条数")
    log("")
    for r in results:
        if r["name"].startswith("全判中性"):
            continue
        log(f"[{r['name']}] 逐类 P/R/F1")
        for k, v in r["per_class"].items():
            log(f"    {k}: P {v['p']:.3f}  R {v['r']:.3f}  F1 {v['f1']:.3f}")
        conf = r["confusion"]
        log("    混淆（行=人工，列=预测）  利空 中性 利好")
        for k, v in conf.items():
            log(f"    {k:<3} {v['利空']:>4} {v['中性']:>4} {v['利好']:>4}")
    log("")
    for r in results:
        if "stratum" in r and not r["name"].endswith("FinFE-test"):
            log(f"分层（{r['name']}）：")
            order = ["公告", "个股", "宏观", "其他"]
            for k in order + [s for s in r["stratum"] if s not in order]:
                if k not in r["stratum"]:
                    continue
                v = r["stratum"][k]
                log(f"    {k}: n={v['n']} acc={v['acc']:.3f} 方向acc={v['dir_acc']:.3f}")

    if args.json_out:
        json.dump({"lexicon_version": LEXICON_VERSION, "results": results},
                  open(args.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        log(f"已写 {args.json_out}")


def log(msg):
    print(msg, flush=True)


if __name__ == "__main__":
    main()
