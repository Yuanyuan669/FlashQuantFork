"""Dashboard 构建器测试：数据收集容错、HTML 渲染与 XSS 转义。"""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from news_aggregator.dashboard import build, collect, render, render_fresh

TZ = ZoneInfo("Asia/Shanghai")


def _today(offset: int = 0) -> str:
    return (datetime.now(TZ) - timedelta(days=offset)).strftime("%Y-%m-%d")


def _make_news(tmp_path, items, offset: int = 0):
    raw = tmp_path / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    fname = (datetime.now(TZ) - timedelta(days=offset)).strftime("%Y%m%d") + ".jsonl"
    (raw / fname).write_text(
        "\n".join(json.dumps(it, ensure_ascii=False) for it in items),
        encoding="utf-8")


def _item(i, offset=0):
    d = _today(offset)
    return {"id": str(i), "ts": f"{d}T10:00:00+08:00", "date": d,
            "source": "财联社", "title": f"标题{i}", "content": "", "url": "",
            "symbols": [], "sentiment": 0.5, "impact": 0.5,
            "impact_parts": {"theme": 0.0}}


def test_collect_counts_and_sorting(tmp_path):
    it1 = _item(1)
    it1.update({"title": "利好政策出台", "sentiment": 0.8, "impact": 0.9,
                "symbols": ["600519"], "impact_parts": {"theme": 0.5}})
    it2 = _item(2)
    it2.update({"source": "Reuters", "title": "rate hike worry", "sentiment": -0.5,
                "impact": 0.4, "impact_parts": {"theme": 0.0}})
    _make_news(tmp_path, [it1, it2])
    data = collect(tmp_path, days=7, top=10)
    assert data["kpi"]["total"] == 2
    assert data["kpi"]["themed"] == 1
    assert data["kpi"]["pos"] == 1 and data["kpi"]["neg"] == 1
    assert data["top_news"][0]["impact"] == 0.9  # 按影响分降序
    assert data["source_stats"][0]["source"] in ("财联社", "Reuters")


def test_collect_tolerates_bad_lines_and_missing_files(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / f"{_today()}.jsonl".replace("-", "")).write_text(
        '{"id":"1","date":"' + _today() + '"}\n{broken json\n\n', encoding="utf-8")
    data = collect(tmp_path, days=7, top=10)
    assert data["kpi"]["total"] == 1
    assert data["alerts"] == []  # alerts.jsonl 不存在不报错
    assert data["market_trend"] == []


def test_render_escapes_payload_and_hides_script_break(tmp_path):
    """标题里的 </script> 与引号不得破坏 JSON payload 或注入 HTML。"""
    items = [{
        "id": "1", "ts": f"{_today()}T10:00:00+08:00", "date": _today(),
        "source": "x", "title": '</script><img src=x onerror=alert(1)>',
        "content": "", "url": "", "symbols": [], "sentiment": 0.0,
        "impact": 0.5, "impact_parts": {"theme": 0.0},
    }]
    _make_news(tmp_path, items)
    data = collect(tmp_path, days=7, top=10)
    html = render(data)
    assert "</script><img" not in html.split("<script id=\"payload\"")[1].split("</script>")[0]
    assert html.startswith("<!DOCTYPE html>")


def test_build_writes_single_file(tmp_path):
    news = tmp_path / "news"
    news.mkdir()
    out = build(news, tmp_path / "dash" / "index.html", days=1, top=10)
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "FlashQuant" in text and "__DATA__" not in text
    assert "__REFRESH__" not in text  # 静态文件不带刷新占位残留


def test_render_auto_refresh_meta(tmp_path):
    _make_news(tmp_path, [])
    html_off = render(collect(tmp_path), auto_refresh_sec=0)
    html_on = render(collect(tmp_path), auto_refresh_sec=60)
    assert 'http-equiv="refresh"' not in html_off
    assert 'http-equiv="refresh" content="60"' in html_on


def test_render_fresh_uses_project_root():
    html = render_fresh(days=1, top=5, auto_refresh_sec=60)
    assert html.startswith("<!DOCTYPE html>")
    assert 'http-equiv="refresh" content="60"' in html
    assert "__DATA__" not in html
