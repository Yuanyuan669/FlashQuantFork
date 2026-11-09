#!/usr/bin/env python
"""FinFE 三分类中文情绪微调（CPU 版，香橙派可用）。

把公开金融社媒三分类语料（FinFE train，15,157 条，标签 0负/1中/2正）
微调到 bert-base 上，产物给 scripts/cn_sentiment_eval.py 做迁移评测。
本脚本只训练，不碰 news/，不改线上后端。

数据准备（FinFE 无 LICENSE，不入库，只放 /tmp）：
    python - <<'EOF'
    import pandas as pd, json
    for split in ("train", "test"):
        df = pd.read_parquet(f"{split}.parquet")
        with open(f"/tmp/finfe_{split}.jsonl", "w", encoding="utf-8") as f:
            for r in df.itertuples():
                f.write(json.dumps({"text": r.sentence, "label": int(r.label)},
                                   ensure_ascii=False) + "\n")
    EOF

用法（香橙派，项目 venv）：
    ~/.venvs/flashquant/bin/python scripts/cn_sentiment_finetune.py --smoke 400
    ~/.venvs/flashquant/bin/python scripts/cn_sentiment_finetune.py \
        --epochs 3 --out /home/orangepi/models/cn-news-sent-v1

耗时基准（2026-11-09 实测，Pi CPU 8 核）：bs32 / max_len 64 / 固定 padding 时
训练吞吐 1.3 样本/秒，即 15,157 条一个 epoch ≈ 3.2 小时。本脚本按长度分桶 +
动态 padding，FinFE 文本 p50 仅 16 字、p95 51 字，实际每 batch 有效长度远低于 64，
吞吐预计提升到 2–3 倍（估计值，未实测；脚本每 50 步打印实测速率与 ETA，以日志为准）。

选点规则：只在 FinFE 验证集（从 train 分层切 8%）上按 macro-F1 选 epoch。
自有 450 条新闻标注是**测试集**，训练过程中不看，避免泄漏。
"""
import argparse
import json
import math
import os
import random
import time


def log(msg):
    print(msg, flush=True)


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            t = str(r.get("text") or "").strip()
            if not t:
                continue
            y = int(r["label"])
            if y not in (0, 1, 2):
                raise ValueError(f"非法标签 {y}: {t[:40]}")
            rows.append((t, y))
    return rows


def split_valid(rows, ratio, seed):
    """分层切验证集（每类按比例取，至少 1 条）。"""
    rnd = random.Random(seed)
    by = {}
    for i, (_, y) in enumerate(rows):
        by.setdefault(y, []).append(i)
    valid = set()
    for _, idx in by.items():
        rnd.shuffle(idx)
        k = max(1, int(round(len(idx) * ratio)))
        valid.update(idx[:k])
    train = [rows[i] for i in range(len(rows)) if i not in valid]
    val = [rows[i] for i in sorted(valid)]
    return train, val


def macro_f1(gold, pred):
    f1s = []
    for lab in (0, 1, 2):
        tp = sum(1 for g, p in zip(gold, pred) if g == lab and p == lab)
        fp = sum(1 for g, p in zip(gold, pred) if g != lab and p == lab)
        fn = sum(1 for g, p in zip(gold, pred) if g == lab and p != lab)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return sum(f1s) / 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-file", default="/tmp/finfe_train.jsonl")
    ap.add_argument("--init", default="/home/orangepi/models/hw2942",
                    help="初始化权重目录；其分类头也是 3 类金融情绪，标签口径一致（0负/1中/2正）")
    ap.add_argument("--out", default="/home/orangepi/models/cn-news-sent-v1")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--eval-batch-size", type=int, default=64)
    ap.add_argument("--max-len", type=int, default=64)
    ap.add_argument("--lr", type=float, default=3e-5)
    ap.add_argument("--weight-decay", type=float, default=0.01)
    ap.add_argument("--warmup-ratio", type=float, default=0.1)
    ap.add_argument("--valid-ratio", type=float, default=0.08)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--threads", type=int, default=8, help="仅 CPU 生效")
    ap.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto",
                    help="auto = 有 CUDA 就用 CUDA（7CH 的 5070 Ti 走这条）")
    ap.add_argument("--bucket", type=int, default=64,
                    help="长度分桶：每个桶含 bucket*batch_size 条样本，桶内按长度排序后切 batch")
    ap.add_argument("--grad-clip", type=float, default=1.0)
    ap.add_argument("--class-weights", default="",
                    help="三个逗号分隔的数（负/中/正），如 '0.202,2.382,0.417'；留空=不加权。"
                         "用来纠正 FinFE（中性 26%）与新闻流（中性 71%）的先验错配；"
                         "权重只用了 450 条的类别分布，不用逐条标签")
    ap.add_argument("--smoke", type=int, default=0,
                    help="冒烟：只取 N 条训练、200 条验证、跑 1 个 epoch（验证代码通路，产物丢弃）")
    ap.add_argument("--resume", action="store_true",
                    help="从 <out>/last 继续（仅恢复权重与最佳分，优化器状态不恢复，见 runbook）")
    args = ap.parse_args()

    import torch
    from torch.nn.utils import clip_grad_norm_
    from torch.optim import AdamW
    from torch.optim.lr_scheduler import LambdaLR
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    if args.device == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(args.device)
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if device.type == "cuda":
        torch.cuda.manual_seed_all(args.seed)
        # BERT 微调对 TF32 不敏感，但速度差一截；显式打开并打印，避免"悄悄变了数值"
        torch.set_float32_matmul_precision("high")
        log(f"设备 {device}：{torch.cuda.get_device_name(0)}（TF32 开启）")
    else:
        torch.set_num_threads(args.threads)
        log(f"设备 cpu，线程 {args.threads}")

    rows = load_jsonl(args.train_file)
    train_rows, val_rows = split_valid(rows, args.valid_ratio, args.seed)
    if args.smoke:
        # FinFE 的 jsonl 是按标签排好序的（0 全在前），直接切前 N 条会只训到一类
        pick = random.Random(args.seed).sample(
            train_rows, min(args.smoke, len(train_rows)))
        train_rows = pick
        val_rows = val_rows[:200]
        args.epochs = 1
        args.out = args.out.rstrip("/") + "-smoke"
        log(f"[smoke] train={len(train_rows)} valid={len(val_rows)} out={args.out}")

    log(f"训练集 {len(train_rows)} 条，验证集 {len(val_rows)} 条，"
        f"init={args.init}，epochs={args.epochs}，bs={args.batch_size}，max_len={args.max_len}")

    tok = AutoTokenizer.from_pretrained(args.init, local_files_only=os.path.isdir(args.init))
    model = AutoModelForSequenceClassification.from_pretrained(
        args.init, num_labels=3, local_files_only=os.path.isdir(args.init))
    model.config.id2label = {0: "Negative", 1: "Neutral", 2: "Positive"}
    model.config.label2id = {"Negative": 0, "Neutral": 1, "Positive": 2}

    start_epoch, best_f1 = 0, -1.0
    if args.resume and os.path.isdir(os.path.join(args.out, "last")):
        model = AutoModelForSequenceClassification.from_pretrained(
            os.path.join(args.out, "last"), num_labels=3, local_files_only=True)
        st = os.path.join(args.out, "state.json")
        if os.path.exists(st):
            s = json.load(open(st, encoding="utf-8"))
            start_epoch = int(s.get("epochs_done", 0))
            best_f1 = float(s.get("best_macro_f1", -1.0))
        log(f"[resume] 从 epoch {start_epoch} 继续，最佳 macro-F1={best_f1:.4f}")

    model.to(device)

    cw = None
    if args.class_weights.strip():
        parts = [float(x) for x in args.class_weights.split(",")]
        if len(parts) != 3:
            raise SystemExit("--class-weights 需要三个逗号分隔的数（负/中/正）")
        # 只改损失权重，不改验证集：验证分仍在 FinFE 分布上，加点权后 valid 分不能与
        # 未加点权的 run 直接比，只能拿 410 条域内评测比。
        cw = torch.tensor(parts, dtype=torch.float, device=device)
        log(f"类别权重 负/中/正 = {parts}")

    def encode(texts):
        enc = tok(texts, add_special_tokens=True, truncation=True,
                  max_length=args.max_len)
        return [list(x) for x in enc["input_ids"]]

    log("编码训练/验证集…")
    t_enc = time.time()
    tr_ids = encode([t for t, _ in train_rows])
    tr_y = [y for _, y in train_rows]
    va_ids = encode([t for t, _ in val_rows])
    va_y = [y for _, y in val_rows]
    log(f"编码完成 {time.time() - t_enc:.1f}s")

    pad_id = tok.pad_token_id if tok.pad_token_id is not None else 0

    def to_tensors(ids_list, idx):
        """按本 batch 最长序列做动态 padding（不补到 max_len），省 CPU。"""
        L = max(len(ids_list[i]) for i in idx)
        x = torch.full((len(idx), L), pad_id, dtype=torch.long, device=device)
        m = torch.zeros((len(idx), L), dtype=torch.long, device=device)
        for r, i in enumerate(idx):
            s = ids_list[i]
            x[r, : len(s)] = torch.tensor(s, dtype=torch.long, device=device)
            m[r, : len(s)] = 1
        return x, m

    def predict(ids_list, batch_size):
        order = sorted(range(len(ids_list)), key=lambda i: len(ids_list[i]))
        preds = [0] * len(ids_list)
        # 验证必须在 eval 模式下跑：训练态带着 dropout，验证分会被系统性压低。
        model.eval()
        with torch.no_grad():
            for k in range(0, len(order), batch_size):
                idx = order[k: k + batch_size]
                x, m = to_tensors(ids_list, idx)
                lg = model(input_ids=x, attention_mask=m).logits
                for r, i in enumerate(idx):
                    preds[i] = int(lg[r].argmax().item())
        model.train()
        return preds

    opt = AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    total_steps = math.ceil(len(train_rows) / args.batch_size) * args.epochs
    warmup = max(1, int(total_steps * args.warmup_ratio))

    def lr_lambda(step):
        if step < warmup:
            return step / warmup
        return max(0.0, (total_steps - step) / max(1, total_steps - warmup))

    sched = LambdaLR(opt, lr_lambda)
    if args.resume:
        for _ in range(min(int(total_steps * start_epoch / args.epochs), total_steps)):
            sched.step()

    os.makedirs(args.out, exist_ok=True)
    log_path = os.path.join(args.out, "train_log.jsonl")
    history = []
    if os.path.exists(log_path):
        history = [json.loads(x) for x in open(log_path, encoding="utf-8") if x.strip()]

    rnd = random.Random(args.seed)
    for epoch in range(start_epoch, args.epochs):
        model.train()
        order = list(range(len(train_rows)))
        rnd.shuffle(order)
        span = args.batch_size * args.bucket
        batches = []
        for i in range(0, len(order), span):
            chunk = sorted(order[i: i + span], key=lambda j: len(tr_ids[j]))
            for k in range(0, len(chunk), args.batch_size):
                batches.append(chunk[k: k + args.batch_size])
        rnd.shuffle(batches)

        t0 = time.time()
        loss_sum, n_seen, n_right = 0.0, 0, 0
        for step, idx in enumerate(batches):
            x, m = to_tensors(tr_ids, idx)
            y = torch.tensor([tr_y[i] for i in idx], dtype=torch.long, device=device)
            out = model(input_ids=x, attention_mask=m)
            loss = torch.nn.functional.cross_entropy(out.logits, y, weight=cw)

            loss_sum += float(loss.detach()) * len(idx)
            n_seen += len(idx)
            n_right += int((out.logits.detach().argmax(-1) == y).sum().item())

            loss.backward()
            clip_grad_norm_(model.parameters(), args.grad_clip)
            opt.step()
            sched.step()
            opt.zero_grad(set_to_none=True)
            if step % 50 == 0:
                dt = max(time.time() - t0, 1e-6)
                rate = n_seen / dt
                eta = (len(train_rows) - n_seen) / max(rate, 1e-6)
                log(f"  epoch {epoch + 1}/{args.epochs} step {step}/{len(batches)} "
                    f"loss {loss_sum / n_seen:.4f} train_acc {n_right / n_seen:.3f} "
                    f"{rate:.2f} 样本/秒 ETA {eta / 60:.0f} 分钟")

        val_pred = predict(va_ids, args.eval_batch_size)
        vac = sum(1 for g, p in zip(va_y, val_pred) if g == p) / len(va_y)
        vf1 = macro_f1(va_y, val_pred)
        rec = {
            "epoch": epoch + 1,
            "train_loss": round(loss_sum / n_seen, 5),
            "train_acc": round(n_right / n_seen, 4),
            "valid_acc": round(vac, 4),
            "valid_macro_f1": round(vf1, 4),
            "sec": round(time.time() - t0, 1),
            "lr": round(sched.get_last_lr()[0], 8),
        }
        history.append(rec)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log(f"[epoch {epoch + 1}] " + json.dumps(rec, ensure_ascii=False))

        model.save_pretrained(os.path.join(args.out, "last"))
        tok.save_pretrained(os.path.join(args.out, "last"))
        if vf1 > best_f1:
            best_f1 = vf1
            model.save_pretrained(os.path.join(args.out, "best"))
            tok.save_pretrained(os.path.join(args.out, "best"))
            log(f"  新最佳 macro-F1={best_f1:.4f}，已存 best/")
        json.dump({"epochs_done": epoch + 1, "best_macro_f1": best_f1,
                   "history": history},
                  open(os.path.join(args.out, "state.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    log(f"完成。best={os.path.join(args.out, 'best')} macro-F1={best_f1:.4f}")
    log("下一步：scripts/cn_sentiment_eval.py --model <best 目录>")


if __name__ == "__main__":
    main()
