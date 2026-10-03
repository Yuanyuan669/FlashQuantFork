"""fetcher 录制回放回归测试。

每个用例用真实抓取的响应样本做 fixture，锁死字段解析行为：
- SEC EDGAR：findtext 缺 namespaces 导致永远 0 条（2026-10 修复）
- 金十：源偶发空正文行，应跳过
- Google News：query 需带 when:30d 时效过滤
"""

import json

import pytest

from news_aggregator import fetchers as F

SEC_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>EDGAR - Current Events</title>
  <entry>
    <title>8-K - Outdoor Holding Co (0001015383) (Filer)</title>
    <link rel="alternate" type="text/html"
          href="https://www.sec.gov/Archives/edgar/data/1015383/000149315226045615/0001493152-26-045615-index.htm" />
    <summary type="html">
      &lt;b&gt;Filed:&lt;/b&gt; 2026-10-02 &lt;b&gt;AccNo:&lt;/b&gt; 0001493152-26-045615
      &lt;br&gt;Item 5.02: Departure of Directors
    </summary>
    <updated>2026-10-02T17:29:10-04:00</updated>
    <id>urn:tag:sec.gov,2008:accession-number=0001493152-26-045615</id>
  </entry>
</feed>"""

JIN10_JSON = {
    "data": [
        {"data": {"content": "美联储官员放鹰", "time": "2026-10-03 19:36:19"}},
        {"data": {"content": "", "time": "2026-10-03 18:39:17"}},   # 空正文行，应跳过
        {"data": {"content": None, "time": "2026-10-03 18:00:00"}},  # None 正文，应跳过
    ]
}


class _FakeResp:
    def __init__(self, content=b"", text="", status_code=200):
        self.content = content
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return json.loads(self.text)


def test_sec_edgar_parses_entries(monkeypatch):
    """回归：findtext 必须带 namespaces，否则 title/updated 恒空、整源永远 0 条。"""
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(content=SEC_ATOM))
    items = F.fetch_sec_edgar()
    assert len(items) == 1
    it = items[0]
    assert it["source"] == "SEC EDGAR"
    assert "Outdoor Holding" in it["title"]
    assert it["url"].startswith("https://www.sec.gov/Archives/")
    assert it["date"] == "2026-10-03"  # -04:00 转 Asia/Shanghai
    assert "Item 5.02" in it["content"] and "<b>" not in it["content"]


def test_jin10_skips_empty_rows(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(
        text=json.dumps(JIN10_JSON)))
    items = F.fetch_jin10()
    assert len(items) == 1
    assert items[0]["content"] == "美联储官员放鹰"


def test_gnews_query_has_recent_window(monkeypatch):
    """Google News 检索必须带 when:30d，否则混入多年前的旧文。"""
    captured = {}

    def fake_rss(url, source, lang="en", params=None, strip_source=True):
        captured["params"] = params
        return []

    monkeypatch.setattr(F, "_fetch_rss", fake_rss)
    F._gnews_rss("site:apnews.com", "AP")
    assert "when:30d" in captured["params"]["q"]
    assert captured["params"]["q"].startswith("site:apnews.com")
