"""中文情绪后端离线 A/B：词典 zh-v2.1 vs 中文金融 BERT（hw2942）。

两个标尺（都只读，不写 news/，不改线上后端）：
1) 自有 120 条中文新闻人工标注（title / +1 利好 0 中性 -1 利空）
2) FinFE test（复旦 BBT-Fin/CFLEB 的金融社媒三分类，1000 条，0 负 / 1 中 / 2 正）

准备数据（FinFE 无 LICENSE，不入库；HF 镜像 FinanceMTEB/FinFE）：
    python -c "import pandas as pd,json; df=pd.read_parquet('test.parquet'); \\
      [open('finfe_test.jsonl','a',encoding='utf-8').write(json.dumps( \\
      {'text':r.sentence,'label':int(r.label)},ensure_ascii=False)+'\\n') \\
      for _,r in df.iterrows()]"

用法（香橙派）：
    ~/.venvs/flashquant/bin/python scripts/sentiment_ab_eval.py \\
        --data-dir /tmp --model /home/orangepi/models/hw2942

结论（2026-11-09 实测，Pi CPU）：
    our120 词典 acc .808 / 方向 .987（n=78，词典在本题上调过，偏乐观）
    our120 hw2942 acc .567 / 方向 .939（n=49，55.8% 判中性，丢信号）
    FinFE  词典 acc .420 / macroF1 .394；hw2942 acc .500 / macroF1 .504
    hw2942 ≈ 110ms/条；论文 80.24% 是 BBT-FinT5-large（权重未公开），
    与这里的 bert-base 代理模型不是一回事。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from news_aggregator.sentiment import score_text_zh  # noqa: E402

DEFAULT_MODEL = "hw2942/bert-base-chinese-finetuning-financial-news-sentiment-v2"


def lex_predict(texts):
    out, raw = [], []
    for t in texts:
        s = score_text_zh(t or "")
        raw.append(s)
        out.append(2 if s > 0 else (0 if s < 0 else 1))
    return out, raw


def macro_f1(gold, pred, labels=(0, 1, 2)):
    f1s = []
    for lab in labels:
        tp = sum(1 for g, p in zip(gold, pred) if g == lab and p == lab)
        fp = sum(1 for g, p in zip(gold, pred) if g != lab and p == lab)
        fn = sum(1 for g, p in zip(gold, pred) if g == lab and p != lab)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return sum(f1s) / len(f1s)


def report(name, gold, pred, raw=None):
    n = len(gold)
    dirs = [(g, p) for g, p in zip(gold, pred) if g != 1 and p != 1]
    row = {
        "name": name, "n": n,
        "acc": sum(1 for g, p in zip(gold, pred) if g == p) / n,
        "macro_f1": macro_f1(gold, pred),
        "dir_acc": (sum(1 for g, p in dirs if g == p) / len(dirs)) if dirs else 0.0,
        "dir_n": len(dirs),
        "neutral_rate": sum(1 for p in pred if p == 1) / n,
    }
    if raw is not None:
        row["zero_rate"] = sum(1 for s in raw if s == 0.0) / n
    return row


def load_our_set(data_dir):
    items = json.load(open(os.path.join(data_dir, "zh_eval_set.json"), encoding="utf-8"))
    labels = json.load(open(os.path.join(data_dir, "zh_eval_labels.json"),
                            encoding="utf-8"))["labels"]
    texts = [it.get("title") or "" for it in items]
    gold = [int(labels[str(i + 1)]) + 1 for i in range(len(items))]
    return texts, gold


def load_finfe(data_dir):
    texts, gold = [], []
    with open(os.path.join(data_dir, "finfe_test.jsonl"), encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            texts.append(r["text"])
            gold.append(int(r["label"]))
    return texts, gold


def load_model(path):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    kw = {"local_files_only": True} if os.path.isdir(path) else {}
    tok = AutoTokenizer.from_pretrained(path, **kw)
    model = AutoModelForSequenceClassification.from_pretrained(path, **kw)
    model.eval()

    def predict(texts, bs=32):
        out, t0 = [], time.time()
        with torch.no_grad():
            for i in range(0, len(texts), bs):
                enc = tok(texts[i:i + bs], truncation=True, max_length=256,
                          padding=True, return_tensors="pt")
                out.extend(model(**enc).logits.argmax(-1).tolist())
        return out, time.time() - t0
    return predict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="/tmp")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    sets = [("our120", *load_our_set(args.data_dir)),
            ("finfe", *load_finfe(args.data_dir))]
    rows = []
    for name, texts, gold in sets:
        pred, raw = lex_predict(texts)
        row = report(f"{name}/lexicon", gold, pred)
        row["zero_rate"] = sum(1 for s in raw if s == 0.0) / len(raw)
        rows.append(row)
    try:
        predict = load_model(args.model)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 模型不可用: {type(e).__name__}: {e}")
        predict = None
    if predict:
        for name, texts, gold in sets:
            pred, dt = predict(texts)
            row = report(f"{name}/bert", gold, pred)
            row.update(sec_total=round(dt, 2),
                       ms_per_item=round(dt * 1000 / len(texts), 1))
            rows.append(row)

    print("{:<20} {:>5} {:>7} {:>8} {:>8} {:>5} {:>8}".format(
        "backend", "n", "acc", "macroF1", "dirAcc", "dirN", "中性率"))
    for r in rows:
        print("{:<20} {:>5} {:>7.3f} {:>8.3f} {:>8.3f} {:>5} {:>8.3f}".format(
            r["name"], r["n"], r["acc"], r["macro_f1"], r["dir_acc"],
            r["dir_n"], r["neutral_rate"]))
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
