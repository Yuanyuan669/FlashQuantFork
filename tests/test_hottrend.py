"""社媒热搜适配器（hottrend.py）离线回归测试。

2026-11-09 生产日志：知乎/头条 失败(NameError)、抖音 失败(JSONDecodeError)。
前两者是移植时丢掉 enumerate、循环体仍引用 rank_i（上游响应格式并没有变），
抖音则是端点要求 webapp 公共参数 + Referer，缺任一个返回 HTTP 200 + 空正文。
本文件用真实响应结构做 fixture 锁死这三条，不联网。
"""

import pytest

from news_aggregator import hottrend as H


def _patch(monkeypatch, payload, calls=None):
    """把 _get 换成返回固定 payload，并记录 (url, headers) 供断言。"""
    def fake_get(url, headers=None, timeout=10):
        if calls is not None:
            calls.append((url, headers or {}))
        return payload
    monkeypatch.setattr(H, "_get", fake_get)


def _assert_ranks_1_to_n(items, n):
    assert len(items) == n
    assert [it["rank"] for it in items] == list(range(1, n + 1))
    assert all(it["source"] == H.SOURCE and it["title"] for it in items)


def test_zhihu_ranks_are_filled(monkeypatch):
    """知乎：data[] 结构未变，循环必须自己编号（曾因缺 enumerate 抛 NameError）。"""
    payload = {"data": [
        {"target": {"title_area": {"text": f"热搜｜知乎话题{i}"},
                    "metrics_area": {"text": f"{100 + i} 万热度"},
                    "link": {"url": f"https://www.zhihu.com/question/{i}"}}}
        for i in range(1, 4)
    ]}
    _patch(monkeypatch, payload)
    items = H.fetch_zhihu()
    _assert_ranks_1_to_n(items, 3)
    assert items[0]["title"] == "知乎话题1"          # 「热搜｜」前缀被剥掉
    assert "101 万热度" in items[0]["content"]


def test_toutiao_ranks_are_filled(monkeypatch):
    """头条：data[] 结构未变，同样必须自己编号。"""
    payload = {"data": [
        {"Title": f"头条话题{i}", "HotValue": str(1000 + i), "ClusterIdStr": f"c{i}"}
        for i in range(1, 4)
    ]}
    _patch(monkeypatch, payload)
    items = H.fetch_toutiao()
    _assert_ranks_1_to_n(items, 3)
    assert items[2]["url"] == "https://www.toutiao.com/trending/c3/"


def test_douyin_needs_params_and_referer(monkeypatch):
    """抖音：URL 必须带 webapp 公共参数，且必须带 Referer，否则上游返回空正文。"""
    calls = []
    payload = {"data": {"word_list": [
        {"word": f"抖音热点{i}", "hot_value": 90000 + i} for i in range(1, 4)
    ]}}
    _patch(monkeypatch, payload, calls)
    items = H.fetch_douyin()
    _assert_ranks_1_to_n(items, 3)
    url, headers = calls[0]
    assert "device_platform=webapp" in url and "aid=6383" in url
    assert headers.get("Referer") == "https://www.douyin.com/hot"


def test_bare_douyin_empty_body_is_caught_by_fetch_all(monkeypatch):
    """上游返回 200 + 空正文（缺 Referer 的真实表现）时，fetch_all 记账不炸整轮。"""
    def fake_get(url, headers=None, timeout=10):
        if "douyin" in url:
            raise H.requests.JSONDecodeError("Expecting value", "", 0)
        return {"list": [{"show_name": "B站热词", "keyword": "B站热词"}]}
    monkeypatch.setattr(H, "_get", fake_get)
    monkeypatch.setattr(H, "ADAPTERS", [("抖音热点", H.fetch_douyin), ("B站热搜", H.fetch_bilibili)])
    monkeypatch.setattr(H.time, "sleep", lambda *_: None)
    items = H.fetch_all()                       # 不抛异常
    assert [it["title"] for it in items] == ["B站热词"]


@pytest.mark.parametrize("fn", [H.fetch_zhihu, H.fetch_toutiao, H.fetch_douyin,
                                H.fetch_weibo, H.fetch_bilibili])
def test_missing_fields_degrade_to_empty(monkeypatch, fn):
    """空 dict / 缺字段响应一律返回 []，不能抛 AttributeError。"""
    _patch(monkeypatch, {})
    assert fn() == []
