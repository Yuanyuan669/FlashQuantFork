#!/usr/bin/env python
"""周级漂移监控：只看已存字段，纯统计，不训练、不打分。

为什么需要它：静态评测已经切不出更干净的判决（见 finetune-cn-sentiment.md 第三、四轮），
模型能不能用取决于「域有没有变」。逐条置信度做不到这件事（实测只抬 acc 4–6 点、召回没救），
能看的是整周的结构性指标。本脚本按 ISO 周汇总：

    条数 / 来源集中度 / 新来源 / 词典中性率 / 词典判有方向比例 / |分| 均值 / 标题长度中位数

给 `--scored`（cn_sentiment_score.py 的产物，逐条带 sentiment_v2_label 与概率）时再补：
    模型中性率 / 低置信占比（pmax < --gate）/ 模型-词典分歧率

最新一周与前几周合并基线比较，超过阈值就打印告警行。阈值是拍的经验值，第一次跑完按实际
分布调，不要当成统计检验。

用法：
    python scripts/cn_sentiment_drift.py --raw-dir news/raw --weeks 6
    python scripts/cn_sentiment_drift.py --raw-dir news/raw --weeks 6 \
        --scored /tmp/scored_20261103.jsonl --out /tmp/drift.md
"""
import argparse
import glob
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta

DATE_RE = re.compile(r"(\d{8})")

if hasattr(sys.stdout, "reconfigure"):        # Windows 管道下默认 cp936，中文会乱码
    sys.stdout.reconfigure(encoding="utf-8")


def iso_week(d):
    y, w, _ = d.isocalendar()
    return "{}-W{:02d}".format(y, w)


def load_window(raw_dir, since):
    """按文件名日期粗筛，再按行内 date 精确过滤。返回 [(week, row)]。"""
    out = []
    for path in sorted(glob.glob(os.path.join(raw_dir, "*.jsonl"))):
        m = DATE_RE.search(os.path.basename(path))
        if m:
            try:
                fd = date(int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:]))
            except ValueError:
                continue
            if fd < since:
                continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ds = r.get("date") or str(r.get("ts") or "")[:10]
                try:
                    d = date.fromisoformat(str(ds)[:10])
                except ValueError:
                    continue
                if d < since:
                    continue
                out.append((iso_week(d), r))
    return out


def lex_label(score, band):
    if score is None:
        return None
    if score > band:
        return 2
    if score < -band:
        return 0
    return 1


def week_stats(rows):
    n = len(rows)
    src = Counter((r.get("source") or "未知") for r in rows)
    scores = []
    labels = []
    for r in rows:
        try:
            s = float(r.get("sentiment"))
        except (TypeError, ValueError):
            s = None
        if s is not None:
            scores.append(s)
            labels.append(lex_label(s, 0.0))
    tl = [len(str(r.get("title") or "")) for r in rows]
    return {
        "n": n,
        "sources": len(src),
        "top_share": (src.most_common(1)[0][1] / n) if n else 0.0,
        "src": src,
        "neutral": (sum(1 for x in labels if x == 1) / len(labels)) if labels else None,
        "dir": (sum(1 for x in labels if x != 1) / len(labels)) if labels else None,
        "abs_mean": (sum(abs(s) for s in scores) / len(scores)) if scores else None,
        "title_len": statistics.median(tl) if tl else 0,
    }


def model_stats(rows, scored):
    """rows 为本周原始行；scored: id -> (label, pmax)。"""
    labels, pmaxs, disag = [], [], []
    for r in rows:
        hit = scored.get(r.get("id"))
        if not hit:
            continue
        mlab, pm = hit
        labels.append(mlab)
        pmaxs.append(pm)
        try:
            sl = lex_label(float(r.get("sentiment")), 0.0)
        except (TypeError, ValueError):
            sl = None
        if sl is not None:
            disag.append(mlab != sl)
    if not labels:
        return None
    return {
        "n": len(labels),
        "neutral": sum(1 for x in labels if x == 1) / len(labels),
        "lowconf": None,          # 由调用方按 gate 计算
        "pmax": pmaxs,
        "disagree": sum(disag) / len(disag) if disag else None,
    }


def fmt(pct):
    return "-" if pct is None else "{:.1%}".format(pct)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default="news/raw")
    ap.add_argument("--weeks", type=int, default=6, help="回看几周（含最新一周）")
    ap.add_argument("--until", default="", help="截止日期 YYYY-MM-DD，默认数据里最新一天")
    ap.add_argument("--scored", nargs="*", default=[],
                    help="cn_sentiment_score.py 产物（jsonl，含 id/sentiment_v2_label/概率）")
    ap.add_argument("--gate", type=float, default=0.70, help="低置信阈值：pmax < gate 记为低置信")
    ap.add_argument("--out", default="", help="另存一份 Markdown")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.raw_dir, "*.jsonl")))
    if not files:
        raise SystemExit("没有找到 {}".format(os.path.join(args.raw_dir, "*.jsonl")))
    fd = []
    for p in files:
        m = DATE_RE.search(os.path.basename(p))
        if not m:
            continue
        try:
            fd.append(date(int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:])))
        except ValueError:
            pass
    until = date.fromisoformat(args.until) if args.until else max(fd)
    since = until - timedelta(days=7 * args.weeks)
    rows = load_window(args.raw_dir, since)

    by_week = defaultdict(list)
    for w, r in rows:
        by_week[w].append(r)
    weeks = sorted(by_week)
    if not weeks:
        raise SystemExit("窗口内没有数据：{}".format(since))

    scored = {}
    for path in args.scored:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                lab = r.get("sentiment_v2_label")
                probs = r.get("probs") or r.get("probabilities")
                if lab is None or not probs:
                    continue
                scored[r.get("id")] = (int(lab), max(probs))

    print("窗口 {} ～ {}，{} 周，共 {} 条（gate {:.2f}）".format(
        since, until, len(weeks), len(rows), args.gate))
    hdr = ("{:<9} {:>6} {:>6} {:>6} {:>7} {:>7} {:>6} {:>6}".format(
        "周", "条数", "来源", "集中度", "中性率", "有方向", "|分|", "标题长"))
    if scored:
        hdr += " {:>7} {:>7} {:>7}".format("模型中性", "低置信", "分歧")
    print(hdr)

    stats = {}
    for w in weeks:
        s = week_stats(by_week[w])
        stats[w] = s
        line = "{:<9} {:>6} {:>6} {:>7} {:>8} {:>7} {:>6} {:>7}".format(
            w, s["n"], s["sources"], fmt(s["top_share"]), fmt(s["neutral"]),
            fmt(s["dir"]), "-" if s["abs_mean"] is None else "{:.2f}".format(s["abs_mean"]),
            "{:.0f}".format(s["title_len"]))
        ms = model_stats(by_week[w], scored) if scored else None
        if ms:
            low = sum(1 for p in ms["pmax"] if p < args.gate) / len(ms["pmax"])
            line += " {:>9} {:>8} {:>7}".format(fmt(ms["neutral"]), fmt(low), fmt(ms["disagree"]))
            s["model_neutral"], s["lowconf"], s["disagree"] = ms["neutral"], low, ms["disagree"]
        print(line)

    if until.isoweekday() != 7:
        print("注意：最新一周不完整（截至 {} 周{}），条数不能与前几周直接比；速率类指标不受影响".format(
            until, "一二三四五六日"[until.isoweekday() - 1]))

    # 新来源：最新一周里、前几周没出现过的
    latest = weeks[-1]
    prior_src = set()
    for w in weeks[:-1]:
        prior_src |= set(stats[w]["src"])
    new_src = [(k, v) for k, v in stats[latest]["src"].items() if k not in prior_src]
    if new_src:
        total_new = sum(v for _, v in new_src)
        print("新来源 {} 个，共 {} 条（{:.1%}）：{}".format(
            len(new_src), total_new, total_new / stats[latest]["n"],
            "、".join("{}({})".format(k, v) for k, v in sorted(new_src, key=lambda x: -x[1])[:5])))

    # 与基线比较：前几周合并
    base_rows = [r for w in weeks[:-1] for r in by_week[w]]
    base = week_stats(base_rows) if base_rows else None
    cur = stats[latest]
    alarms = []
    if base:
        checks = (("中性率", cur["neutral"], base["neutral"], 0.10),
                  ("来源集中度", cur["top_share"], base["top_share"], 0.15),
                  ("|分|均值", cur["abs_mean"], base["abs_mean"], 0.10),
                  ("标题长度中位数", float(cur["title_len"]), float(base["title_len"]), 8.0))
        for name, a, b, tol in checks:
            if a is None or b is None:
                continue
            if abs(a - b) > tol:
                alarms.append("{} {:.3f} → {:.3f}（基线 {:.3f}）".format(name, b, a, b))
    if cur.get("model_neutral") is not None and len(weeks) > 1:
        prior = [stats[w].get("model_neutral") for w in weeks[:-1]]
        prior = [x for x in prior if x is not None]
        if prior:
            b = sum(prior) / len(prior)
            if abs(cur["model_neutral"] - b) > 0.10:
                alarms.append("模型中性率 {:.3f} → {:.3f}".format(b, cur["model_neutral"]))
    if cur.get("disagree") is not None and len(weeks) > 1:
        prior = [stats[w].get("disagree") for w in weeks[:-1]]
        prior = [x for x in prior if x is not None]
        if prior:
            b = sum(prior) / len(prior)
            if abs(cur["disagree"] - b) > 0.10:
                alarms.append("模型-词典分歧率 {:.3f} → {:.3f}".format(b, cur["disagree"]))

    if alarms:
        print("\n漂移告警（最新一周 {}）".format(latest))
        for a in alarms:
            print("  - " + a)
    else:
        print("\n最新一周 {} 未超阈值".format(latest))
    if not scored:
        print("提示：没给 --scored，模型侧指标（中性率/低置信/分歧）缺失；"
              "先用 cn_sentiment_score.py 给当周打分再喂进来。")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("# 情绪漂移周报 {}～{}\n\n".format(since, until))
            f.write("| 周 | 条数 | 来源 | 集中度 | 词典中性率 | 有方向 | \\|分\\| | 标题长 |\n")
            f.write("| --- | --- | --- | --- | --- | --- | --- | --- |\n")
            for w in weeks:
                s = stats[w]
                f.write("| {} | {} | {} | {} | {} | {} | {} | {:.0f} |\n".format(
                    w, s["n"], s["sources"], fmt(s["top_share"]), fmt(s["neutral"]),
                    fmt(s["dir"]), "-" if s["abs_mean"] is None else "{:.2f}".format(s["abs_mean"]),
                    s["title_len"]))
            if new_src:
                f.write("\n新来源 {} 个，共 {} 条。\n".format(len(new_src), sum(v for _, v in new_src)))
            if alarms:
                f.write("\n**漂移告警**\n\n")
                for a in alarms:
                    f.write("- " + a + "\n")
        print("已写 " + args.out)


if __name__ == "__main__":
    main()
