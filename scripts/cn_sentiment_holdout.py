#!/usr/bin/env python
"""外部测试集评测：在自有标注全量上二阶段微调，在一份未参与训练的标注集上对比模型与词典。

协议与 `cn_sentiment_domain_cv.py` 完全一致（FinFE 初始化，lr 1e-5、2 epoch、bs 16、max_len 64、
grad clip 1.0），差别只有一点：不留验证折，全部标注拿去训练，评测落在另一个 jsonl 上。
`--test-jsonl` 里 label < 0 的存疑条目不参与评测。

用途：2026-11 新补的 180 条利空富集集不参与训练，只在它上面比较「FinFE+410 条二阶段微调」
与词典的利空召回。这套样本是按「命中利空关键词或词典分 ≤ -0.15」富集出来的，因此：

- 利空的绝对召回率在两边都偏高，不能和自然分布的 410 条数字直接比；
- 富集把「词典没看出来」的利空条目前置筛掉了一部分，所以对词典反而有利，
  模型若仍赢，结论偏保守；
- 两个方法在同一批样本上的差异（McNemar）是可用的判决依据。

用法（GPU）：
    python scripts/cn_sentiment_holdout.py \
        --finfe-model D:/fq-data/models/cn-news-sent-v1/best \
        --train-jsonl scripts/zh_news_eval_450.jsonl \
        --test-jsonl scripts/zh_news_eval_bear180.jsonl \
        --device cuda --json-out D:/fq-data/tmp/holdout_bear180.json
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


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def bear_recall(rows, gold, pred):
    out = {}
    for r, g, p in zip(rows, gold, pred):
        if g < 0:
            continue
        key = "候选子集" if r.get("neg_hint") else "对照子集"
        d = out.setdefault(key, [0, 0])
        if g == 0:
            d[1] += 1
            d[0] += int(p == 0)
    return {k: f"{v[0]}/{v[1]}" for k, v in out.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--finfe-model", required=True, help="FinFE 阶段产物目录（…/best）")
    ap.add_argument("--train-jsonl", default=os.path.join(ROOT, "scripts", "zh_news_eval_450.jsonl"))
    ap.add_argument("--test-jsonl", required=True)
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--max-len", type=int, default=64)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--dump-rows", default="", help="逐条预测落盘（jsonl），便于人工比对错例")
    args = ap.parse_args()

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)
    log(f"设备 {device}")

    tr_all = load_jsonl(args.train_jsonl)
    tr = [r for r in tr_all if int(r["label"]) >= 0]
    te_all = load_jsonl(args.test_jsonl)
    te = [r for r in te_all if int(r["label"]) >= 0]
    log(f"训练 {len(tr_all)} 条，可用 {len(tr)} 条；外部测试 {len(te_all)} 条，可用 {len(te)} 条")

    tok = AutoTokenizer.from_pretrained(args.finfe_model, local_files_only=True)
    pad_id = tok.pad_token_id if tok.pad_token_id is not None else 0

    tr_titles = [r["title"] for r in tr]
    te_titles = [r["title"] for r in te]
    tr_ids = [list(x) for x in tok(tr_titles, add_special_tokens=True, truncation=True,
                                   max_length=args.max_len)["input_ids"]]
    te_ids = [list(x) for x in tok(te_titles, add_special_tokens=True, truncation=True,
                                   max_length=args.max_len)["input_ids"]]
    tr_gold = [int(r["label"]) for r in tr]
    te_gold = [int(r["label"]) for r in te]

    def to_tensors(batch, ids):
        L = max(len(ids[i]) for i in batch)
        x = torch.full((len(batch), L), pad_id, dtype=torch.long, device=device)
        m = torch.zeros((len(batch), L), dtype=torch.long, device=device)
        for r, i in enumerate(batch):
            s = ids[i]
            x[r, : len(s)] = torch.tensor(s, dtype=torch.long, device=device)
            m[r, : len(s)] = 1
        return x, m

    model = AutoModelForSequenceClassification.from_pretrained(
        args.finfe_model, num_labels=3, local_files_only=True)
    model.to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    rnd = random.Random(args.seed)

    model.train()
    t0 = time.time()
    for ep in range(args.epochs):
        order = list(range(len(tr)))
        rnd.shuffle(order)
        order.sort(key=lambda j: len(tr_ids[j]))
        for k in range(0, len(order), args.batch_size):
            batch = order[k: k + args.batch_size]
            x, m = to_tensors(batch, tr_ids)
            y = torch.tensor([tr_gold[i] for i in batch], dtype=torch.long, device=device)
            loss = torch.nn.functional.cross_entropy(model(input_ids=x, attention_mask=m).logits, y)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            opt.zero_grad(set_to_none=True)
        log(f"  epoch {ep + 1}/{args.epochs} 完成，累计 {time.time() - t0:.1f}s")

    def predict(ids, n):
        out = [1] * n
        probs = [[0.0, 1.0, 0.0] for _ in range(n)]
        model.eval()
        with torch.no_grad():
            for k in range(0, n, 64):
                batch = list(range(k, min(k + 64, n)))
                x, m = to_tensors(batch, ids)
                lg = model(input_ids=x, attention_mask=m).logits
                pr = torch.softmax(lg, dim=-1).tolist()
                for r, i in enumerate(batch):
                    out[i] = int(lg[r].argmax().item())
                    probs[i] = [round(x, 4) for x in pr[r]]
        return out, probs

    te_pred, te_probs = predict(te_ids, len(te))
    tr_pred, _ = predict(tr_ids, len(tr))          # 样本内，只用来确认训练有没有学到
    te_lex_score = [score_text_zh(t) for t in te_titles]
    te_lex = [lex_label(s, 0.0) for s in te_lex_score]

    results = []
    r_model = report("微调模型/外部集", te_gold, te_pred)
    r_lex = report("词典/外部集", te_gold, te_lex)
    r_neu = report("全判中性/外部集", te_gold, [1] * len(te_gold))
    p, b, c = mcnemar([int(x == y) for x, y in zip(te_pred, te_gold)],
                      [int(x == y) for x, y in zip(te_lex, te_gold)])
    r_model["mcnemar_vs_lexicon"] = {"b_model_only": b, "c_lex_only": c, "p": round(p, 4)}
    r_model["bear_recall_split"] = bear_recall(te, te_gold, te_pred)
    r_lex["bear_recall_split"] = bear_recall(te, te_gold, te_lex)
    for r, pred in ((r_model, te_pred), (r_lex, te_lex), (r_neu, [1] * len(te_gold))):
        r["stratum"] = by_stratum(te, te_gold, pred)
        results.append(r)
    results[0]["lexicon_version"] = LEXICON_VERSION
    results[0]["insample"] = report("微调模型/训练集(样本内)", tr_gold, tr_pred)

    log("")
    log("{:<22} {:>4} {:>7} {:>8} {:>9} {:>8} {:>11} {:>6}".format(
        "方法", "n", "acc", "macroF1", "中性泄漏", "方向acc", "方向acc条件", "中性率"))
    for r in results:
        log("{:<22} {:>4} {:>7.3f} {:>8.3f} {:>9.3f} {:>8.3f} {:>11.3f} {:>6.3f}".format(
            r["name"], r["n"], r["acc"], r["macro_f1"], r["neutral_leak"],
            r["dir_acc_strict"], r["dir_acc_cond"], r["pred_neutral_rate"]))
    log(f"McNemar vs 词典/外部集：b_model_only={b} c_lex_only={c} p={p:.4f}")
    ins = results[0]["insample"]
    log(f"样本内 acc={ins['acc']:.3f} macroF1={ins['macro_f1']:.3f}（只做自检，不作结论）")
    for r in results[:2]:
        log(f"[{r['name']}] 利空召回：{r['bear_recall_split']}")
        log(f"[{r['name']}] 逐类 P/R/F1")
        for k, v in r["per_class"].items():
            log(f"    {k}: P {v['p']:.3f}  R {v['r']:.3f}  F1 {v['f1']:.3f}")
        log(f"分层（{r['name']}）：")
        for k, v in r["stratum"].items():
            log(f"    {k}: n={v['n']} acc={v['acc']:.3f} 方向acc={v['dir_acc']:.3f}")

    if args.dump_rows:
        with open(args.dump_rows, "w", encoding="utf-8") as f:
            for r, g, p_, pb, lx, s in zip(te, te_gold, te_pred, te_probs, te_lex, te_lex_score):
                f.write(json.dumps({"id": r.get("id"), "stratum": r.get("stratum"),
                                    "neg_hint": r.get("neg_hint"), "title": r.get("title"),
                                    "gold": g, "model": p_, "probs": pb, "lex": lx,
                                    "lex_score": round(s, 4)}, ensure_ascii=False) + "\n")
        log(f"已写 {args.dump_rows}")
    if args.json_out:
        json.dump({"lexicon_version": LEXICON_VERSION, "results": results},
                  open(args.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        log(f"已写 {args.json_out}")


if __name__ == "__main__":
    main()
