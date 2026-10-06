"""聚合推送（build_digest）单元测试：压缩格式与分块上限。"""

from news_aggregator.monitor import build_digest


def _entry(i):
    return (
        f"[事件告警] 主题{i}",
        "**情绪分**：+0.5　**影响分**：0.6\n**原文**：某新闻内容测试\n**来源**：财联社",
    )


def test_digest_compact_format():
    chunks = build_digest([_entry(1)])
    assert len(chunks) == 1
    assert "[事件告警] 主题1" in chunks[0]
    assert "情绪分" in chunks[0] and "影响分" in chunks[0]
    assert "原文" in chunks[0]
    # 冗余行（来源等）不进入摘要
    assert "来源" not in chunks[0]


def test_digest_chunks_respect_byte_limit():
    # 每条 ~200 字节，100 条 ≈ 20KB，必须切成多块且每块都低于上限
    entries = [_entry(i) for i in range(100)]
    chunks = build_digest(entries, max_bytes=3800)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.encode("utf-8")) <= 3800
    # 不丢条目
    joined = "\n".join(chunks)
    assert all(f"主题{i}" in joined for i in range(100))


def test_digest_empty():
    assert build_digest([]) == []
