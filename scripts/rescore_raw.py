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

from datetime import datetime, timedelta, timezone  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")

from news_aggregator.impact import compute_impact  # noqa: E402
from news_aggregator.sentiment import configure_backend  # noqa: E402

def state_path(shard: str) -> Path:
    """分片各自的断点状态文件（多机并行互不覆盖）。"""
    return ROOT / "news" / (f"rescore_state{shard}.json" if shard else "rescore_state.json")


def load_state(backend: str, path: Path) -> dict:
    """断点续跑：记录后端名 + 已完成的日文件。

    state 里记录 backend：换后端（如词典->FinBERT）后旧状态作废全量重算，
    避免不同量纲的打分结果互相覆盖。
    """
    p = path
    if p.exists():
        try:
            state = json.loads(p.read_text(encoding="utf-8"))
            if state.get("backend") == backend:
                return state
            print(f"[rescore] 状态文件后端为 {state.get('backend')!r}，"
                  f"与当前 {backend!r} 不符 -> 作废重算")
        except json.JSONDecodeError:
            pass
    return {"backend": backend, "done": {}}


def save_state(state: dict, path: Path) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="raw 全量重打分")
    ap.add_argument("--days", type=int, default=0, help="只处理近 N 天（0=全量）")
    ap.add_argument("--shard", default="", metavar="I:N",
                    help="多机并行分片：本机只处理待办列表中 index%%n==i 的文件，状态文件按分片隔离")
    ap.add_argument("--reweight", action="store_true",
                    help="仅重加权（复用已存情绪分，不跑模型；改权重/relevance 公式后用）")
    args = ap.parse_args()

    shard_i, shard_n = 0, 1
    if args.shard:
        i, _, n = args.shard.partition(":")
        shard_i, shard_n = int(i), int(n or 1)
    shard_tag = f"_shard{shard_i}of{shard_n}" if shard_n > 1 else ""
    state_p = state_path(shard_tag)

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    backend_name = str((cfg.get("sentiment") or {}).get("backend") or "lexicon")

    # 重加权模式：改权重/relevance 公式后用——复用已存情绪分，不加载模型、秒-分钟级
    reweight = getattr(args, "reweight", False)
    if reweight:
        backend_name = "reweight(uses stored scores)"
    configure_backend(cfg if not reweight else {})

    # 重打分场景禁止静默降级：配置了 FinBERT 但环境不可用 -> 硬报错。
    # （降级会用词典覆盖 FinBERT 结果，属于数据污染——2026-10-11 实证）
    if backend_name == "finbert":
        try:
            import transformers  # noqa: F401
        except ImportError as e:
            raise SystemExit(f"[rescore] FinBERT 不可用（{e}），"
                             "拒绝用词典重打分。请先安装 transformers/torch 或改 backend=lexicon。")

    state = load_state(backend_name, state_p)
    imp_cfg = cfg.get("impact") or {}
    weights = imp_cfg.get("weights") or None
    window = int(imp_cfg.get("window_minutes", 60))
    burst_cap = int(imp_cfg.get("burst_cap", 4))
    themes_path = ROOT / (imp_cfg.get("themes_path") or "news_aggregator/themes.yaml")
    themes = (yaml.safe_load(themes_path.read_text(encoding="utf-8")) or {}).get("themes") or []

    raw_dir = ROOT / "news" / "raw"
    files = sorted(raw_dir.glob("*.jsonl"))
    if args.days > 0:
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        keep = {(today - timedelta(days=o)).strftime("%Y%m%d") for o in range(args.days)}
        files = [p for p in files if p.stem in keep]
    print(f"[rescore] 待处理 {len(files)} 个日文件 | 后端: "
          f"{(cfg.get('sentiment') or {}).get('backend')}")

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
            save_state(state, state_p)
            continue
        total += len(items)
        out = compute_impact(items, themes, weights, window, burst_cap,
                             use_stored_scores=reweight)
        new_text = "\n".join(json.dumps(it, ensure_ascii=False) for it in out) + "\n"
        if p.read_text(encoding="utf-8", errors="replace") != new_text:
            p.write_text(new_text, encoding="utf-8")
            rescored += len(out)
        done[p.name] = len(out)
        save_state(state, state_p)   # 每个文件完成即落盘，中断不丢进度
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
