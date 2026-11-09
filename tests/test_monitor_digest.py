"""聚合推送摘要（digest_entry + build_digest）单元测试：瘦身格式与分块上限。

2026-11-08 起摘要条目从「标题 + 情绪分行 + 原文行（≤160 字）」改为
「标题行 + 单行元数据」，配合每轮 top-N 预算，一轮通常只需 1 条企微消息。
"""

from news_aggregator.monitor import build_digest, digest_entry

HEADER = ("**[事件告警汇总] 11-06 15:07 · 新增 901 / 过阈值 169 / 推送 15**\n"
          "抑制：同题 42 · 主题配额 88 · 预算 24（落选明细见 dashboard 告警历史）")


def _entry(i):
    return digest_entry(f"新闻标题{i}", "AI/算力", 0.62, 0.8,
                        "财报预增(利好)", "东财全球资讯", "2026-11-06 15:07")


def test_digest_entry_compact():
    e = _entry(1)
    assert "新闻标题1" in e
    assert "影响0.62" in e and "情绪+0.80" in e
    assert "AI/算力" in e and "东财全球资讯" in e and "财报预增(利好)" in e
    # 瘦身目标：单条 < 260 字节（旧格式约 330 字节）
    assert len(e.encode("utf-8")) < 260


def test_digest_entry_truncates_long_title():
    e = digest_entry("长" * 200, "主题", 0.5, -0.3)
    assert "…" in e
    assert len(e.encode("utf-8")) < 300


def test_digest_entry_optional_detail_line():
    e = digest_entry("标题", "主题", 0.5, 0.1, detail="- 算力概念(概念)：润建股份 +10.0%")
    assert e.endswith("- 算力概念(概念)：润建股份 +10.0%")


def test_digest_chunks_respect_byte_limit():
    entries = [_entry(i) for i in range(100)]
    chunks = build_digest(entries, max_bytes=3800)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.encode("utf-8")) <= 3800
    joined = "\n".join(chunks)
    assert all(f"新闻标题{i}" in joined for i in range(100))


def test_digest_header_in_every_chunk_without_overflow():
    entries = [_entry(i) for i in range(100)]
    chunks = build_digest(entries, HEADER, max_bytes=3800)
    assert len(chunks) > 1
    for c in chunks:
        assert c.startswith(HEADER)
        assert len(c.encode("utf-8")) <= 3800


def test_typical_round_is_a_single_message():
    """核心指标：每轮预算 15 条时只产生 1 条企微消息（旧格式同规模约 3 条）。"""
    chunks = build_digest([_entry(i) for i in range(15)], HEADER)
    assert len(chunks) == 1
    assert len(chunks[0].encode("utf-8")) <= 3800


def test_digest_empty():
    assert build_digest([]) == []
    assert build_digest([], HEADER) == []
