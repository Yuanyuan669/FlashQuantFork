#!/usr/bin/env python
"""用微调好的中文情绪模型给新闻流批量打分（只读输入，写新文件）。

产物字段（不改动原始行里的 sentiment）：
    sentiment_v2        p(利好) - p(利空)，[-1,1]，与词典分同向同区间，便于对比
    sentiment_v2_label  0利空 / 1中性 / 2利好
    sentiment_v2_model  模型目录名，用于区分版本

中性判定：`p(中性) - max(p(利好), p(利空)) > t` 判中性，t 默认 0（等价 argmax）。
把 t 调成负数（如 -0.2）会让更多条目落到中性——FinFE 的中性先验（26%）
和新闻流（71%）不一致时，先用 cn_sentiment_eval.py --neutral-cv 找到 t。

用法（香橙派，项目 venv）：
    ~/.venvs/flashquant/bin/python scripts/cn_sentiment_score.py \
        --model /home/orangepi/models/cn-news-sent-v1/best \
        --in-file news/raw/20261109.jsonl --out /tmp/scored_20261109.jsonl

只测速不落盘加 --dry-run；先跑 200 条试水加 --limit 200。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAB = {0: "利空", 1: "中性", 2: "利好"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--in-file", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--input", choices=("title", "full"), default="full",
                    help="full = 标题+正文（与线上 sentiment 的口径一致）")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--max-len", type=int, default=128)
    ap.add_argument("--threads", type=int, default=8, help="仅 CPU 生效")
    ap.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto",
                    help="auto = 有 CUDA 就用 CUDA")
    ap.add_argument("--neutral-threshold", type=float, default=0.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    if args.device == "auto":
        dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        dev = torch.device(args.device)
    if dev.type == "cuda":
        print(f"设备 {dev}：{torch.cuda.get_device_name(0)}", flush=True)
    else:
        torch.set_num_threads(args.threads)
    tok = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.model, num_labels=3, local_files_only=True)
    model.to(dev).eval()

    items = []
    with open(args.in_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    if args.limit:
        items = items[: args.limit]
    texts = [f"{it.get('title') or ''} {it.get('content') or ''}".strip()
             if args.input == "full" else str(it.get("title") or "")
             for it in items]
    print(f"读入 {len(items)} 条，输入口径={args.input}", flush=True)

    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    out = [None] * len(items)
    t0 = time.time()
    with torch.no_grad():
        for k in range(0, len(order), args.batch_size):
            idx = order[k: k + args.batch_size]
            enc = tok([texts[i] for i in idx], truncation=True, max_length=args.max_len,
                      padding=True, return_tensors="pt")
            enc = {k: v.to(dev) for k, v in enc.items()}
            probs = model(**enc).logits.softmax(-1).float().cpu()
            for r, i in enumerate(idx):
                p = probs[r].tolist()
                lab = 1 if p[1] - max(p[0], p[2]) > args.neutral_threshold \
                    else (2 if p[2] >= p[0] else 0)
                out[i] = (round(p[2] - p[0], 4), lab, round(p[0], 4),
                          round(p[1], 4), round(p[2], 4))
            done = min(k + args.batch_size, len(order))
            if done % 200 < args.batch_size or done == len(order):
                rate = done / max(time.time() - t0, 1e-6)
                eta = (len(order) - done) / max(rate, 1e-6)
                print(f"  打分 {done}/{len(order)} ({rate:.1f} 条/秒，剩余 {eta:.0f}s)",
                      flush=True)
    dt = time.time() - t0
    name = os.path.basename(os.path.dirname(args.model.rstrip("/")) or args.model)
    for it, (s, lab, p0, p1, p2) in zip(items, out):
        it["sentiment_v2"] = s
        it["sentiment_v2_label"] = lab
        it["sentiment_v2_model"] = name
        it["sentiment_v2_probs"] = [p0, p1, p2]

    dist = {LAB[k]: sum(1 for x in out if x[1] == k) for k in (0, 1, 2)}
    print(f"完成 {len(items)} 条，{dt:.1f}s，{dt * 1000 / max(len(items), 1):.0f}ms/条；"
          f"分布 {dist}", flush=True)
    if args.dry_run:
        print("--dry-run：不写文件", flush=True)
        return
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    print(f"已写 {args.out}", flush=True)


if __name__ == "__main__":
    main()
