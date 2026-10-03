"""URL 归一化与兜底校验测试（回归：华尔街见闻 uri 拼接坏链）。"""

from news_aggregator.fetchers import _mk, _normalize_url, _parse_dt


def test_normalize_full_url_untouched():
    assert _normalize_url("https://wallstreetcn.com/livenews/1", "https://wallstreetcn.com") \
        == "https://wallstreetcn.com/livenews/1"
    assert _normalize_url("http://example.com/a?b=1", "https://wallstreetcn.com") \
        == "http://example.com/a?b=1"


def test_normalize_relative_path():
    assert _normalize_url("/livenews/3173865", "https://wallstreetcn.com") \
        == "https://wallstreetcn.com/livenews/3173865"
    assert _normalize_url("livenews/1", "https://wallstreetcn.com") \
        == "https://wallstreetcn.com/livenews/1"


def test_normalize_broken_schemes():
    # 回归样例：源数据返回缺冒号的完整 URL
    assert _normalize_url("https//wallstreetcn.com/livenews/3173865", "https://wallstreetcn.com") \
        == "https://wallstreetcn.com/livenews/3173865"
    assert _normalize_url("http:/example.com/x", "https://wallstreetcn.com") \
        == "http://example.com/x"


def test_normalize_empty():
    assert _normalize_url("", "https://wallstreetcn.com") == ""
    assert _normalize_url(None, "https://wallstreetcn.com") == ""


def test_mk_drops_non_http_url():
    dt = _parse_dt("2026-10-03T10:00:00+08:00")
    ok = _mk(dt, "s", "t", "c", "https://example.com/x")
    bad = _mk(dt, "s", "t2", "c", "javascript:alert(1)")
    empty = _mk(dt, "s", "t3", "c", "")
    assert ok["url"] == "https://example.com/x"
    assert bad["url"] == ""
    assert empty["url"] == ""


def test_mk_drops_double_scheme_concat():
    """回归：wallstcn uri 拼接出第二个 scheme 的坏链要被置空。"""
    dt = _parse_dt("2026-10-03T10:00:00+08:00")
    garbage = _mk(dt, "s", "t", "c",
                  "https://wallstreetcn.comhttps//wallstreetcn.com/livenews/3173865")
    garbage2 = _mk(dt, "s", "t2", "c",
                   "https://wallstreetcn.comhttps://wallstreetcn.com/livenews/1")
    legit_query = _mk(dt, "s", "t3", "c",
                      "https://example.com/redirect?u=https%3A%2F%2Ftarget.com")
    assert garbage["url"] == ""
    assert garbage2["url"] == ""
    assert legit_query["url"].startswith("https://example.com/")
