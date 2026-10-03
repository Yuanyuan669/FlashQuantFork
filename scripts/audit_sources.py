"""数据源健康度审计：跑每个 fetcher，逐条校验输出字段质量（URL/时间/文本/重复）。

用法（项目根目录）：
    python scripts/audit_sources.py            # 审计全部源 + 个股新闻
    python scripts/audit_sources.py --source 财联社    # 只审计指定源

用途：源格式变更（改版/改字段）会在这里现形，而不是靠用户点开坏链接发现。
建议每周跑一次，或纳入 CI；某源连续异常即为数据源健康度告警依据。
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from news_aggregator.fetchers import SOURCES, fetch_symbol_news, load_env_file  # noqa: E402
import yaml  # noqa: E402

load_env_file(ROOT / ".env")
BAD_URL_MARKERS = ("https:", "http:")  # 出现第二次即为拼接坏链


def audit_item(it):
    issues = []
    u = it.get("url") or ""
    if u:
        if not u.startswith(("http://", "https://")):
            issues.append("url非http")
        else:
            tail = u[9:]
            if any(m in tail for m in BAD_URL_MARKERS):
                issues.append("url双scheme")
    try:
        from datetime import datetime
        datetime.fromisoformat(it["ts"])
        if not ("2020" <= it["ts"][:4] <= "2027"):
            issues.append("ts年份异常")
    except Exception:
        issues.append("ts不可解析")
    if not (it.get("title") or it.get("content") or "").strip():
        issues.append("标题内容全空")
    return issues


def main():
    ap = argparse.ArgumentParser(description="数据源健康度审计")
    ap.add_argument("--source", action="append", default=[],
                    help="只审计指定源（可多次传入）")
    args = ap.parse_args()

    report = []
    for name, fn in SOURCES:
        if args.source and name not in args.source:
            continue
        try:
            items = fn() or []
        except Exception as e:
            report.append((name, -1, Counter({f"异常:{type(e).__name__}": 1})))
            continue
        issues = Counter()
        ids = set()
        dups = 0
        for it in items:
            for iss in audit_item(it):
                issues[iss] += 1
            if it["id"] in ids:
                dups += 1
            ids.add(it["id"])
        if dups:
            issues["批内重复id"] = dups
        report.append((name, len(items), issues))

    # 个股新闻
    cfg = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
    try:
        sym_items = fetch_symbol_news(cfg["symbols"])
        issues = Counter()
        for it in sym_items:
            for iss in audit_item(it):
                issues[iss] += 1
        report.append(("个股新闻", len(sym_items), issues))
    except Exception as e:
        report.append(("个股新闻", -1, Counter({f"异常:{type(e).__name__}": 1})))

    print(f"{'源':<14}{'条数':>6}  问题")
    print("-" * 70)
    for name, n, issues in report:
        flag = " <-- " if issues else ""
        print(f"{name:<14}{n:>6}  {dict(issues) if issues else 'OK'}{flag}")

    bad = [name for name, n, iss in report if n < 0 or any(k not in ("批内重复id",) for k in iss)]
    if bad:
        print(f"\n[!] {len(bad)} 个源存在异常: {', '.join(bad)}")
        return 1
    print("\n[OK] 全部源健康")
    return 0


if __name__ == "__main__":
    sys.exit(main())
