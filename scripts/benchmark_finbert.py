"""FinBERT 打分基准：固定样本，两台机器跑同一份，对比吞吐。

用法：
    python scripts/benchmark_finbert.py            # 默认 200 条
    python scripts/benchmark_finbert.py --n 500

样本：news/raw/20261007.jsonl 的 Finnhub 条目（按 id 排序，两机一致）。
流程：模型预热 10 条（不计入）→ 计时打分 N 条 → 输出条/秒。
把两台机器的输出发回对比即可。
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from news_aggregator.sentiment import configure_backend  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="FinBERT 打分基准")
    ap.add_argument("--n", type=int, default=200)
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    configure_backend(cfg)

    p = ROOT / "news" / "raw" / "20261007.jsonl"
    items = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip().startswith("{"):
            continue
        try:
            it = json.loads(line)
        except json.JSONDecodeError:
            continue
        if it.get("source") == "Finnhub":
            items.append(it)
    items.sort(key=lambda x: x.get("id") or "")
    items = items[:args.n]
    texts = [(it.get("title") or "") + " " + (it.get("content") or "")[:1400]
             for it in items]
    print(f"[bench] 样本 {len(texts)} 条（Finnhub @ 20261007，截断 1500 字符）")

    from news_aggregator.sentiment import score_batch

    score_batch(texts[:10])   # 预热：模型加载不计入
    t0 = time.time()
    scores = score_batch(texts)
    dt = time.time() - t0
    print(f"[bench] 结果: {len(scores)} 条 | 总耗时 {dt:.1f}s | "
          f"吞吐 {len(scores)/dt:.2f} 条/秒")
    print("[bench] 机器信息: ", end="")
    import platform
    print(platform.machine(), "|", platform.processor()[:60])
    return 0


if __name__ == "__main__":
    sys.exit(main())
