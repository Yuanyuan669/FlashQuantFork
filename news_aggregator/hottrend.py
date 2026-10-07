"""社媒热搜聚合：6 平台热搜适配器（移植自 JCP D:\\jcp 的 Go 实现）。

平台：微博 / 百度 / 知乎 / 抖音 / 头条 / B站（均为免 key 公开 JSON 端点）。

设计要点：
- 话题稳定 id：id = md5("社媒热搜|" + 标题)，**不含时间戳** —— 同一话题跨轮次/跨平台
  只产生一次告警（seen.json 持久化去重），避免热搜榜上常驻话题反复推送；
- 每平台独立容错，单个平台失败/改版不影响其他平台；
- 热搜数据归档后供 TM 侧 P1 摄取消费（社媒热度维度），金融相关的热搜
  自然通过情绪/主题过滤触发告警（如"某公司暴雷上热搜"）。
"""

import hashlib
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Asia/Shanghai")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOURCE = "社媒热搜"


def _get(url: str, headers: dict | None = None, timeout: int = 10):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    h.update(headers or {})
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _item(platform_cn: str, title: str, hot: str = "", url: str = "") -> dict | None:
    title = (title or "").strip()
    if not title:
        return None
    now = datetime.now(TZ)
    # 话题稳定 id：不含时间戳，同一话题跨轮次/跨平台只入库一次
    key = f"{SOURCE}|{title}"
    item_id = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    content = f"平台：{platform_cn}｜热度：{hot}" if hot else f"平台：{platform_cn}"
    return {
        "id": item_id,
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "source": SOURCE,
        "title": title,
        "content": content,
        "url": url or "",
        "symbols": [],
        "lang": "zh",
        "kind": "news",
        "ticker": "",
        "politician": "",
        "rank": None,
    }


def fetch_weibo() -> list:
    """微博热搜：data.realtime[] -> word/note/num。需 Referer。"""
    data = _get("https://weibo.com/ajax/side/hotSearch",
                headers={"Referer": "https://weibo.com/"})
    if data.get("ok") != 1:
        return []
    out = []
    for row in (data.get("data") or {}).get("realtime") or []:
        word = str(row.get("word") or "").strip()
        if not word:
            continue
        note = str(row.get("note") or "").strip()
        hot = note or str(row.get("num") or "")
        it = _item("微博", word, hot=hot,
                   url=f"https://s.weibo.com/weibo?q=%23{word}%23")
        if it:
            out.append(it)
    return out


def fetch_baidu() -> list:
    """百度热搜：data.cards[].content[].content[] -> word/url（双层嵌套）。"""
    data = _get("https://top.baidu.com/api/board?platform=wise&tab=realtime",
                headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X)"})
    out = []
    for card in (data.get("data") or {}).get("cards") or []:
        for group in card.get("content") or []:
            for row in group.get("content") or []:
                word = str(row.get("word") or "").strip()
                if not word:
                    continue
                it = _item("百度", word, url=str(row.get("url") or ""))
                if it:
                    out.append(it)
    return out


def fetch_zhihu() -> list:
    """知乎热榜：data[].target -> title_area.text / metrics_area.text / link.url。"""
    data = _get("https://www.zhihu.com/api/v3/feed/topstory/hot-list-web?limit=50&desktop=true")
    out = []
    for row in data.get("data") or []:
        target = row.get("target") or {}
        title = ((target.get("title_area") or {}).get("text") or "").strip()
        title = title.removeprefix("热搜｜").strip()
        if not title:
            continue
        metrics = ((target.get("metrics_area") or {}).get("text") or "").strip()
        url = ((target.get("link") or {}).get("url") or "")
        it = _item("知乎", title, hot=metrics, url=url)
        if it:
            out.append(it)
    return out


def fetch_douyin() -> list:
    """抖音热点：data.word_list[] -> word/hot_value。"""
    data = _get("https://www.douyin.com/aweme/v1/web/hot/search/list/")
    out = []
    for row in (data.get("data") or {}).get("word_list") or []:
        word = str(row.get("word") or "").strip()
        if not word:
            continue
        it = _item("抖音", word, hot=str(row.get("hot_value") or ""))
        if it:
            out.append(it)
    return out


def fetch_toutiao() -> list:
    """头条热榜：data[] -> Title/HotValue/ClusterIdStr。"""
    data = _get("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc")
    out = []
    for row in data.get("data") or []:
        title = str(row.get("Title") or "").strip()
        if not title:
            continue
        cid = str(row.get("ClusterIdStr") or "")
        url = f"https://www.toutiao.com/trending/{cid}/" if cid else ""
        it = _item("头条", title, hot=str(row.get("HotValue") or ""), url=url)
        if it:
            out.append(it)
    return out


def fetch_bilibili() -> list:
    """B站热搜：list[] -> keyword/show_name。"""
    data = _get("https://s.search.bilibili.com/main/hotword?limit=50")
    out = []
    for row in data.get("list") or []:
        word = str(row.get("show_name") or row.get("keyword") or "").strip()
        if not word:
            continue
        it = _item("B站", word)
        if it:
            out.append(it)
    return out


ADAPTERS = [
    ("微博热搜", fetch_weibo),
    ("百度热搜", fetch_baidu),
    ("知乎热榜", fetch_zhihu),
    ("抖音热点", fetch_douyin),
    ("头条热榜", fetch_toutiao),
    ("B站热搜", fetch_bilibili),
]


def fetch_all() -> list:
    """聚合 6 平台热搜；每平台独立容错，平台间 0.3s 限速。"""
    items, stats = [], {}
    for name, fn in ADAPTERS:
        try:
            got = fn() or []
            stats[name] = len(got)
            items.extend(got)
        except Exception as e:  # noqa: BLE001
            stats[name] = f"失败({type(e).__name__})"
        time.sleep(0.3)
    print("[hottrend] " + " | ".join(f"{k}:{v}" for k, v in stats.items()))
    return items
