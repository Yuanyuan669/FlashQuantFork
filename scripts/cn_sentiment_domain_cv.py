#!/usr/bin/env python
"""域适配的 K 折评估：在 FinFE 预热模型上，用自有标注做二阶段微调，报 OOF 数字。

背景：`cn_sentiment_finetune.py` 的模型在 FinFE 上 .79，迁到 410 条快讯标题只剩 .63，
主要败在不肯弃权（中性泄漏 .360）。这个脚本回答的是「如果允许模型见我们的标注，能到哪」。

协议（每折）：
    1. 从 FinFE 阶段产物（--finfe-model）复制一份模型；
    2. 用 4/5 自有标注二阶段微调（lr 1e-5，默认 2 epoch，bs 16）；
    3. 在剩下 1/5 上预测，汇成 out-of-fold 预测。

FinFE 阶段没见过任何自有标签，二阶段每折也只在 4/5 上训练，所以 OOF 是不吃训练集的。
代价：每折的初始权重相同（同一个 FinFE 模型），折间差异只来自二阶段，方差会偏小。

注意：二阶段训练用的是 450 条里的训练折，`--our-jsonl` 里 -1 存疑条目不参与训练也不参与
评测。若将来把这些标注真正并入训练集，要重新划一份独立测试集，不能再用这 410 条。

用法（GPU）：
    python scripts/cn_sentiment_domain_cv.py --finfe-model D:/fq-data/models/cn-news-sent-v1/best \
        --our-jsonl scripts/zh_news_eval_450.jsonl --device cuda --json-out /tmp/domain_cv.json
"""
import argparse
import json
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from news_aggregator.sentiment import LEXICON_VERSION, score_text_zh  # noqa: E402
from cn_sentiment_eval import by_stratum, lex_label, mcnemar, report  # noqa: E402


def log(msg):
    print(msg, flush=True)


def load_our(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def make_folds(rows, k, seed):
    """按 (标签, 分层) 分组后轮转分配，保证每折的类与分层分布接近。"""
    groups = {}
    for i, r in enumerate(rows):
        groups.setdefault((int(r["label"]), r.get("stratum", "")), []).append(i)
    folds = [[] for _ in range(k)]
    rnd = random.Random(seed)
    for key in sorted(groups):
        idx = groups[key][:]
        rnd.shuffle(idx)
        start = rnd.randrange(k)
        for n, i in enumerate(idx):
            folds[(start + n) % k].append(i)
    return folds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--finfe-model", required=True, help="FinFE 阶段产物目录（…/best）")
    ap.add_argument("--our-jsonl", default=os.path.join(ROOT, "scripts", "zh_news_eval_450.jsonl"))
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--max-len", type=int, default=64)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)
    log(f"设备 {device}")

    rows = load_our(args.our_jsonl)
    keep = [r for r in rows if int(r["label"]) >= 0]
    titles = [r["title"] for r in keep]
    gold = [int(r["label"]) for r in keep]
    log(f"自有标注 {len(rows)} 条，可用 {len(keep)} 条（剔除存疑 {len(rows) - len(keep)}）")

    tok = AutoTokenizer.from_pretrained(args.finfe_model, local_files_only=True)
    pad_id = tok.pad_token_id if tok.pad_token_id is not None else 0
    enc = tok(titles, add_special_tokens=True, truncation=True, max_length=args.max_len)
    ids = [list(x) for x in enc["input_ids"]]

    folds = make_folds(keep, args.folds, args.seed)
    oof = [1] * len(keep)
    t_all = time.time()
    for fi, val_idx in enumerate(folds):
        val_set = set(val_idx)
        tr_idx = [i for i in range(len(keep)) if i not in val_set]
        model = AutoModelForSequenceClassification.from_pretrained(
            args.finfe_model, num_labels=3, local_files_only=True)
        model.to(device)
        opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
        rnd = random.Random(args.seed + fi)

        def to_tensors(batch):
            L = max(len(ids[i]) for i in batch)
            x = torch.full((len(batch), L), pad_id, dtype=torch.long, device=device)
            m = torch.zeros((len(batch), L), dtype=torch.long, device=device)
            for r, i in enumerate(batch):
                s = ids[i]
                x[r, : len(s)] = torch.tensor(s, dtype=torch.long, device=device)
                m[r, : len(s)] = 1
            return x, m

        model.train()
        for ep in range(args.epochs):
            order = tr_idx[:]
            rnd.shuffle(order)
            order.sort(key=lambda j: len(ids[j]))      # 长度分桶，减少 padding
            for k in range(0, len(order), args.batch_size):
                batch = order[k: k + args.batch_size]
                x, m = to_tensors(batch)
                y = torch.tensor([gold[i] for i in batch], dtype=torch.long, device=device)
                loss = torch.nn.functional.cross_entropy(model(input_ids=x, attention_mask=m).logits, y)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                opt.zero_grad(set_to_none=True)

        model.eval()
        with torch.no_grad():
            for k in range(0, len(val_idx), 64):
                batch = val_idx[k: k + 64]
                x, m = to_tensors(batch)
                lg = model(input_ids=x, attention_mask=m).logits
                for r, i in enumerate(batch):
                    oof[i] = int(lg[r].argmax().item())
        log(f"  折 {fi + 1}/{len(folds)}：训练 {len(tr_idx)} 条，验证 {len(val_idx)} 条，"
            f"累计 {time.time() - t_all:.1f}s")

    lex_scores = [score_text_zh(t) for t in titles]
    lex_pred = [lex_label(s, 0.0) for s in lex_scores]

    results = []
    r_model = report("模型/域适配 5 折 OOF", gold, oof)
    r_lex = report("词典/标题", gold, lex_pred)
    r_neu = report("全判中性", gold, [1] * len(gold))
    p, b, c = mcnemar([int(p_ == g) for p_, g in zip(oof, gold)],
                      [int(p_ == g) for p_, g in zip(lex_pred, gold)])
    r_model["mcnemar_vs_lexicon"] = {"b_model_only": b, "c_lex_only": c, "p": round(p, 4)}
    for r in (r_model, r_lex, r_neu):
        r["stratum"] = by_stratum(keep, gold, oof if r is r_model else
                                  (lex_pred if r is r_lex else [1] * len(gold)))
        results.append(r)
    results[0]["lexicon_version"] = LEXICON_VERSION

    log("")
    log("{:<22} {:>4} {:>7} {:>8} {:>9} {:>8} {:>11} {:>6}".format(
        "方法", "n", "acc", "macroF1", "中性泄漏", "方向acc", "方向acc条件", "中性率"))
    for r in results:
        log("{:<22} {:>4} {:>7.3f} {:>8.3f} {:>9.3f} {:>8.3f} {:>11.3f} {:>6.3f}".format(
            r["name"], r["n"], r["acc"], r["macro_f1"], r["neutral_leak"],
            r["dir_acc_strict"], r["dir_acc_cond"], r["pred_neutral_rate"]))
    log(f"McNemar vs 词典/标题：b_model_only={b} c_lex_only={c} p={p:.4f}")
    for r in results:
        if r["name"].startswith("全判中性"):
            continue
        log(f"[{r['name']}] 逐类 P/R/F1")
        for k, v in r["per_class"].items():
            log(f"    {k}: P {v['p']:.3f}  R {v['r']:.3f}  F1 {v['f1']:.3f}")
    for r in results:
        log(f"分层（{r['name']}）：")
        for k, v in r["stratum"].items():
            log(f"    {k}: n={v['n']} acc={v['acc']:.3f} 方向acc={v['dir_acc']:.3f}")

    if args.json_out:
        json.dump({"lexicon_version": LEXICON_VERSION, "results": results},
                  open(args.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        log(f"已写 {args.json_out}")


if __name__ == "__main__":
    main()
