"""raw 全量重打分：英文 FinBERT（含量纲对齐）、重算影响分，回写 raw。

背景：FinBERT 上线前的历史英文条目情绪分是词典值（大量 0），影响分失真，
top 榜被高量纲的英文源霸占。本脚本用【当前后端 + 当前权重】对全部 raw
重新打分并回写，随后自动重建情绪历史。

用法：
    .venv/bin/python scripts/rescore_raw.py            # 全量
    .venv/bin/python scripts/rescore_raw.py --days 14  # 只近 N 天

注意：
- FinBERT CPU ~5-10 条/秒，全量（~4 万条、英文约 1/3）约 10-20 分钟
- 确定性打分，重复执行结果一致；中断后重跑安全
- 完成后自动执行 run.py --rebuild-history 重建情绪历史
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from news_aggregator.impact import compute_impact  # noqa: E402
from news_aggregator.sentiment import configure_backend  # noqa: E402

STATE_PATH = ROOT / "news" / "rescore_state.json"


def load_state() -> dict:
    """断点续跑：记录已完成的日文件（文件名 -> 条数）。中断后重跑自动跳过。"""
    p = STATE_PATH
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"done": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def main() -> int:
    ap = argparse.ArgumentParser(description="raw 全量重打分")
    ap.add_argument("--days", type=int, default=0, help="只处理近 N 天（0=全量）")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    configure_backend(cfg)
    imp_cfg = cfg.get("impact") or {}
    weights = imp_cfg.get("weights") or None
    window = int(imp_cfg.get("window_minutes", 60))
    burst_cap = int(imp_cfg.get("burst_cap", 4))
    themes_path = ROOT / (imp_cfg.get("themes_path") or "news_aggregator/themes.yaml")
    themes = (yaml.safe_load(themes_path.read_text(encoding="utf-8")) or {}).get("themes") or []

    raw_dir = ROOT / "news" / "raw"
    files = sorted(raw_dir.glob("*.jsonl"))
    if args.days > 0:
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        keep = {(today - timedelta(days=o)).strftime("%Y%m%d") for o in range(args.days)}
        files = [p for p in files if p.stem in keep]
    print(f"[rescore] 待处理 {len(files)} 个日文件 | 后端: "
          f"{(cfg.get('sentiment') or {}).get('backend')}")

    state = load_state()
    done = state.setdefault("done", {})
    todo = [p for p in files if p.name not in done]
    skipped = len(files) - len(todo)
    if skipped:
        print(f"[rescore] 断点续跑：跳过已完成 {skipped} 个，待处理 {len(todo)} 个")
    total = rescored = 0
    for k, p in enumerate(todo, 1):
        lines = [l for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
                 if l.strip().startswith("{")]
        items = []
        for l in lines:
            try:
                items.append(json.loads(l))
            except json.JSONDecodeError:
                continue
        if not items:
            done[p.name] = 0
            save_state(state)
            continue
        total += len(items)
        out = compute_impact(items, themes, weights, window, burst_cap)
        new_text = "\n".join(json.dumps(it, ensure_ascii=False) for it in out) + "\n"
        if p.read_text(encoding="utf-8", errors="replace") != new_text:
            p.write_text(new_text, encoding="utf-8")
            rescored += len(out)
        done[p.name] = len(out)
        save_state(state)   # 每个文件完成即落盘，中断不丢进度
        if k % 25 == 0 or k == len(todo):
            print(f"[rescore] 进度 {k}/{len(todo)} 文件 | 累计 {total} 条 | "
                  f"重写 {rescored} 条", flush=True)

    print(f"[rescore] 完成：{total} 条 / 重写 {rescored} 条")

    from news_aggregator.run import rebuild_history_from_raw, save_history  # noqa: E402
    h = rebuild_history_from_raw()
    save_history(h)
    print(f"[rescore] 情绪历史已重建：市场 {len(h.get('market', {}))} 天 / "
          f"个股 {len(h.get('symbols', {}))} 只")

    # 结果上传：commit + push（需 Pi 已配置 GitHub SSH key；失败不影响本地结果，重跑只差上传）
    import subprocess
    if not (ROOT / ".git").exists():
        print("[rescore] 非 git 仓库，跳过上传")
        return 0
    for args in (("config", "user.name", "flashquant-rescore"),
                 ("config", "user.email", "flashquant@rescore.local")):
        subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)
    subprocess.run(["git", "-C", str(ROOT), "add", "news/"], capture_output=True)
    stamp = datetime.now(TZ).strftime("%F %H:%M")
    c = subprocess.run(["git", "-C", str(ROOT), "commit", "-m",
                        f"rescore: raw rescored with current backend ({stamp})"],
                       capture_output=True, text=True)
    if c.returncode != 0:
        print("[rescore] 无变更需要提交")
        return 0
    subprocess.run(["git", "-C", str(ROOT), "pull", "--rebase", "--autostash",
                    "origin", "master"], capture_output=True)
    push = subprocess.run(["git", "-C", str(ROOT), "push", "origin", "master"],
                          capture_output=True, text=True)
    print("[rescore] git push:", "OK" if push.returncode == 0
          else f"失败（本地结果已保留）：{(push.stderr or '').strip()[:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
