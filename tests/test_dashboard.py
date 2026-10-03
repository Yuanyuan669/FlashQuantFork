"""Dashboard 构建器测试：数据收集容错、HTML 渲染与 XSS 转义。"""

import json

from news_aggregator.dashboard import build, collect, render


def _make_news(tmp_path, items):
    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    (raw / "20261003.jsonl").write_text(
        "\n".join(json.dumps(it, ensure_ascii=False) for it in items),
        encoding="utf-8")


def test_collect_counts_and_sorting(tmp_path):
    items = [
        {"id": "1", "ts": "2026-10-03T10:00:00+08:00", "date": "2026-10-03",
         "source": "财联社", "title": "利好政策出台", "content": "", "url": "",
         "symbols": ["600519"], "sentiment": 0.8, "impact": 0.9,
         "impact_parts": {"theme": 0.5}},
        {"id": "2", "ts": "2026-10-03T09:00:00+08:00", "date": "2026-10-03",
         "source": "Reuters", "title": "rate hike worry", "content": "", "url": "",
         "symbols": [], "sentiment": -0.5, "impact": 0.4,
         "impact_parts": {"theme": 0.0}},
    ]
    _make_news(tmp_path, items)
    data = collect(tmp_path, days=7, top=10)
    assert data["kpi"]["total"] == 2
    assert data["kpi"]["themed"] == 1
    assert data["kpi"]["pos"] == 1 and data["kpi"]["neg"] == 1
    assert data["top_news"][0]["impact"] == 0.9  # 按影响分降序
    assert data["source_stats"][0]["source"] in ("财联社", "Reuters")


def test_collect_tolerates_bad_lines_and_missing_files(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "20261003.jsonl").write_text(
        '{"id":"1","date":"2026-10-03"}\n{broken json\n\n', encoding="utf-8")
    data = collect(tmp_path, days=7, top=10)
    assert data["kpi"]["total"] == 1
    assert data["alerts"] == []  # alerts.jsonl 不存在不报错
    assert data["market_trend"] == []


def test_render_escapes_payload_and_hides_script_break(tmp_path):
    """标题里的 </script> 与引号不得破坏 JSON payload 或注入 HTML。"""
    items = [{
        "id": "1", "ts": "2026-10-03T10:00:00+08:00", "date": "2026-10-03",
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
