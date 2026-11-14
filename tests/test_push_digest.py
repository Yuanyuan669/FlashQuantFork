"""日报/推送简报的当日口径与跨源同题归并单元测试。

2026-11-14 修复两处：
1. 综述/日报 Top15/推送简报此前拿 filter_recent(30天) 池按 impact 排序，
   旧稿（个股新闻里 11-05 的龙虎榜稿）会混进「今日高影响」——增加 pick_day_items；
2. 推送简报的「今日高影响」此前只做 id 级去重，跨源同稿（东财全球资讯 +
   华尔街见闻）会双条入榜——增加 merge_same_event（复用告警层指纹，×N源）。
"""

from news_aggregator.run import (
    build_push_digest,
    day_key,
    merge_same_event,
    pick_day_items,
)


def _item(id, title, source, impact, sentiment=0.6, ts="2026-11-14 09:26"):
    return {
        "id": id,
        "ts": ts,
        "source": source,
        "title": title,
        "content": "",
        "impact": impact,
        "sentiment": sentiment,
    }


SAME_TITLE = ("中信证券：看好中国光通信的长期发展趋势 "
              "建议重点关注深度受益于全球AI基建的核心龙头标的")


# ── day_key / pick_day_items ─────────────────────────────────────────


def test_day_key_normalizes_iso_ts():
    assert day_key({"ts": "2026-11-14 09:26"}) == "20261114"
    assert day_key({"ts": ""}) == ""
    assert day_key({}) == ""


def test_pick_day_items_exact_day():
    items = [
        _item("a", "A", "s", 0.5, ts="2026-11-13 10:00"),
        _item("b", "B", "s", 0.9),
        _item("c", "C", "s", 0.8),
    ]
    day_items, day_used = pick_day_items(items, "20261114")
    assert [it["id"] for it in day_items] == ["b", "c"]
    assert day_used == "20261114"


def test_pick_day_items_falls_back_to_latest_day():
    items = [
        _item("a", "A", "s", 0.5, ts="2026-11-13 10:00"),
        _item("b", "B", "s", 0.9, ts="2026-11-12 09:00"),
    ]
    # 请求 11-14（无条目，节假日兜底）→ 回退最近有数据的 11-13
    day_items, day_used = pick_day_items(items, "20261114")
    assert [it["id"] for it in day_items] == ["a"]
    assert day_used == "20261113"


def test_pick_day_items_empty_pool_returns_empty():
    assert pick_day_items([], "20261114") == ([], "20261114")


# ── merge_same_event ─────────────────────────────────────────────────


def test_merge_same_event_absorbs_cross_source_dups():
    items = [
        _item("em", SAME_TITLE, "东财全球资讯", 0.73),
        _item("wscn", SAME_TITLE, "华尔街见闻", 0.72),
        _item("bot", "嘉腾机器人与软通天驰智行达成战略合作", "东财全球资讯", 0.72),
    ]
    merged, n_same = merge_same_event(items)
    assert [it["id"] for it in merged] == ["em", "bot"]
    assert n_same["em"] == 2
    assert "wscn" not in n_same


def test_merge_same_event_keeps_distinct_titles():
    items = [
        _item("a", "嘉腾机器人与软通天驰智行达成战略合作", "东财全球资讯", 0.72),
        _item("b", "百利天恒宜泽康成功纳入2026年版国家医保药品目录", "华尔街见闻", 0.68),
    ]
    merged, n_same = merge_same_event(items)
    assert len(merged) == 2
    assert all(n_same[it["id"]] == 1 for it in merged)


def test_merge_same_event_numbers_differ_not_merged():
    # 告警层口径：数字串不同的相近标题（龙虎榜 09-29 vs 09-30）不算同题
    items = [
        _item("d1", "万科A000002龙虎榜数据09-29", "个股新闻", 0.7),
        _item("d2", "万科A000002龙虎榜数据09-30", "个股新闻", 0.7),
    ]
    merged, n_same = merge_same_event(items)
    assert len(merged) == 2
    assert all(n_same[it["id"]] == 1 for it in merged)


# ── build_push_digest ────────────────────────────────────────────────


def test_build_push_digest_marks_n_sources_and_dedups():
    items = [
        _item("em", SAME_TITLE, "东财全球资讯", 0.73),
        _item("wscn", SAME_TITLE, "华尔街见闻", 0.72),
        _item("bot", "嘉腾机器人与软通天驰智行达成战略合作", "东财全球资讯", 0.72),
    ]
    text = build_push_digest("20261114", items, {"s": 3}, {"2026-11-14": 0.03})
    assert "×2源" in text
    assert text.count("看好中国光通信") == 1  # 同题只出现一次
    assert "华尔街见闻" not in text.split("**今日高影响**")[1]  # 被吸收源不上榜
    assert text.count("嘉腾机器人") == 1


def test_build_push_digest_single_source_no_marker():
    items = [_item("a", "嘉腾机器人与软通天驰智行达成战略合作", "东财全球资讯", 0.72)]
    text = build_push_digest("20261114", items, {"s": 1}, {"2026-11-14": 0.03})
    assert "×" not in text.split("**今日高影响**")[1]
    assert "东财全球资讯" in text


def test_build_push_digest_empty_items_no_high_impact_block():
    text = build_push_digest("20261114", [], {"s": 0}, {"2026-11-14": 0.03})
    assert "今日高影响" not in text
