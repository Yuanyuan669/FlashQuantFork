#!/usr/bin/env python3
"""生成静态舆情 Dashboard（单文件 HTML，零外部依赖）。

用法（项目根目录，Linux / Windows 通用）：
    python scripts/generate_dashboard.py                    # 默认近 7 天 Top 50
    python scripts/generate_dashboard.py --days 14 --top 100
    python scripts/generate_dashboard.py --open             # 生成后用浏览器打开
    python scripts/generate_dashboard.py --watch 60         # 常驻每 60s 重建（配合 monitor）

输出：dashboard/index.html
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news_aggregator.dashboard import build_from_root, open_in_browser  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="生成静态舆情 Dashboard")
    ap.add_argument("--days", type=int, default=7, help="纳入近 N 天的原始新闻（默认 7）")
    ap.add_argument("--top", type=int, default=50, help="高影响新闻展示条数（默认 50）")
    ap.add_argument("--out", default="", help="输出路径（默认 dashboard/index.html）")
    ap.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    ap.add_argument("--watch", type=int, default=0, metavar="SEC",
                    help="常驻模式：每 N 秒重建一次（0=只跑一次）")
    args = ap.parse_args()

    out = Path(args.out) if args.out else None

    def once() -> Path:
        path = build_from_root(ROOT, days=args.days, top=args.top) if out is None \
            else build(ROOT / "news", out, days=args.days, top=args.top)
        print(f"[dashboard] 已生成 {path}（{path.stat().st_size // 1024} KB）")
        return path

    if args.watch <= 0:
        path = once()
        if args.open:
            open_in_browser(path)
        return 0

    print(f"[dashboard] watch 模式：每 {args.watch}s 重建，Ctrl+C 退出")
    while True:
        try:
            once()
        except Exception as e:  # noqa: BLE001
            print(f"[dashboard] 重建失败: {type(e).__name__}: {e}")
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print("[dashboard] 已退出")
            return 0


if __name__ == "__main__":
    sys.exit(main())
