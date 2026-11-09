"""图片卡片（news_aggregator/card.py）单元测试。

纯计算部分（字号/折行/词云排布/数据组装）用 measure 回调离线跑，不依赖 Pillow；
只有 test_render_produces_png 需要 Pillow + 中文字体，缺任一即 skip
（香橙派生产环境两者都有，开发机 Windows 有微软雅黑 + 项目 venv 里的 Pillow）。
"""

import math
import os

import pytest

from news_aggregator import card

CJK_PX = 30          # 存根度量：一个中日韩字符 30px
LATIN_PX = 15        # 一个英文字符 15px


def measure(text, size):
    w = sum(CJK_PX if ord(c) > 0x2E80 else LATIN_PX for c in text)
    return w, size


def measure_w(text, size):
    return measure(text, size)[0]


# ---------------- 字号与折行 ----------------

def test_cloud_font_size_monotonic_and_bounded():
    lo, hi = card.cloud_font_size(1, 10), card.cloud_font_size(10, 10)
    assert lo < hi
    assert card.cloud_font_size(99, 10) == hi                 # 超频封顶，不撑爆区域
    assert card.cloud_font_size(0, 10) == 26
    assert card.cloud_font_size(3, 0) == 26                   # max_count=0 不除零


def test_cloud_font_size_compresses_with_power_law():
    """幂律 0.55：频次差 4 倍时字号只差约 2 倍，否则最高频词会独占整行。"""
    s1 = card.cloud_font_size(2, 8, 20, 60)
    s2 = card.cloud_font_size(8, 8, 20, 60)
    assert s2 - s1 < (s2 // 2)


def test_units_keeps_latin_words_and_spaces():
    """英文/数字串整块、中文逐字，空白折成一个空格单位——断行不能把 "AVGO" 拆成 "A|VGO"。"""
    assert card.units("AMD, AVGO 涨超3%") == ["AMD,", " ", "AVGO", " ", "涨", "超", "3%"]
    assert card.units("AMD  AVGO") == ["AMD", " ", "AVGO"]      # 连续空白折成一个
    assert card.units(" 算力 ") == ["算", "力"]                  # 首尾空白丢弃
    assert card.units("") == []


def test_wrap_keeps_explicit_space_between_cjk():
    """主线行是拼出来的（"A ×1 · B ×1"），空格被丢掉会粘成读不懂的一串。"""
    line = "主线：财报/公告 ×1 · 创新药/疫苗 ×1"
    out = card.wrap(line, 600, measure_w, 30, max_lines=2)
    assert "".join(out).count(" ") == line.count(" ")
    assert "公告×1" not in "".join(out)


def test_wrap_folds_by_pixel_width():
    lines = card.wrap("中文标题很长很长需要折行处理一下", 150, measure_w, 30, max_lines=2)
    assert len(lines) == 2
    assert all(measure_w(l, 30) <= 150 or l.endswith("…") for l in lines)
    # 首行吃满宽度：再加一个字就超 150，说明不是提前断的
    assert measure_w(lines[0], 30) + CJK_PX > 150


def test_wrap_adds_ellipsis_when_content_overflows():
    out = card.wrap("一二三四五六七八九十壹贰叁肆伍陆柒捌玖拾", 150, measure_w, 30, max_lines=2)
    assert len(out) == 2 and out[-1].endswith("…")


def test_wrap_short_text_is_single_line():
    assert card.wrap("短标题", 600, measure_w, 30) == ["短标题"]
    assert card.wrap("", 600, measure_w, 30) == []


# ---------------- 词云排布 ----------------

def test_place_cloud_no_overlap_within_area_and_bigger_for_hotter():
    words = [("算力", 9), ("回购", 6), ("三季报", 5), ("中标", 3), ("减持", 2), ("机器人", 8)]
    placed, skipped = card.place_cloud(words, 900, 300, measure)
    assert skipped == []
    assert len(placed) == len(words)
    for p in placed:
        assert 0 <= p["x"] and p["x"] + p["w"] <= 900
        assert 0 <= p["y"] and p["y"] + p["h"] <= 300
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            assert (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
                    or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"]), (a, b)
    by_text = {p["text"]: p for p in placed}
    assert by_text["算力"]["size"] > by_text["减持"]["size"]
    # 高频词更靠近中心
    d = lambda p: math.hypot(p["x"] + p["w"] / 2 - 450, p["y"] + p["h"] / 2 - 150)
    assert d(by_text["算力"]) < d(by_text["减持"])


def test_place_cloud_skips_words_that_cannot_fit():
    """区域放不下的高频长词进 skipped，而不是硬画到边界外。"""
    placed, skipped = card.place_cloud([("很长的英文单词不应出现在区域外", 9), ("算力", 5)],
                                       200, 160, measure)
    assert skipped == ["很长的英文单词不应出现在区域外"]
    assert [p["text"] for p in placed] == ["算力"]
    assert placed[0]["x"] + placed[0]["w"] <= 200 and placed[0]["y"] + placed[0]["h"] <= 160


def test_place_cloud_empty_and_zero_count():
    assert card.place_cloud([], 300, 200, measure) == ([], [])
    placed, skipped = card.place_cloud([("", 3), ("  ", 2), ("算力", 0)], 300, 200, measure)
    assert [p["text"] for p in placed] == ["算力"] and len(skipped) == 0


# ---------------- 数据组装 ----------------

def test_build_payload_sorts_and_caps_hot_words():
    p = card.build_payload("2026-11-09T15:05", {"pushed": 3, "over": 120, "new": 800},
                           [{"theme": "AI/算力", "tone": {"利好": 2},
                             "items": [{"title": "a"}, {"title": "b"}]},
                            {"theme": "机器人", "tone": {}, "items": [{"title": "c"}]}],
                           {"算力": 3, "回购": 1, "中标": 2, "业绩": 1},
                           {"利好": 2, "利空": 1},
                           {"同题": 4, "注意力预算": 30}, top_n=3)
    assert p["hot"][:2] == [("算力", 3), ("中标", 2)]          # 频次降序、同频按词序
    assert len(p["hot"]) == 3                                  # top_n 截断
    assert p["mainline"] == [("AI/算力", 2), ("机器人", 1)]
    assert p["stats"]["pushed"] == 3 and p["suppressed"]["同题"] == 4
    assert p["groups"][0]["items"][0]["title"] == "a"


def test_build_payload_empty_inputs():
    p = card.build_payload("", None, None, None, None, None)
    assert p["hot"] == [] and p["mainline"] == [] and p["groups"] == []


def test_short_round_matches_text_digest_header():
    assert card._short_round("2026-11-09T12:42") == "11-09 12:42"
    assert card._short_round("11-09 12:42") == "11-09 12:42"   # 已精简的原样保留
    assert card._short_round(None) == ""


# ---------------- 渲染（需要 Pillow + 中文字体） ----------------

def _pil_available():
    try:
        import PIL                      # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _pil_available() or not card.find_font()[0],
                    reason="需要 Pillow 与中文字体（Noto Sans CJK / 微软雅黑）")
def test_render_produces_png_within_wecom_limit(tmp_path):
    payload = card.build_payload(
        "2026-11-09 15:05", {"pushed": 8, "over": 199, "new": 952},
        [{"theme": "AI/算力", "tone": {"利好": 2, "利空": 1},
          "items": [{"title": "360与中国移动达成战略合作 携手推动AI应用与安全发展",
                     "impact": 0.71, "score": 0.63, "ev": "合作(利好)",
                     "source": "东财全球资讯", "n_same": 2},
                    {"title": "Why Did DKNG, SOUN, ORCL Stocks Plunge To 52-Week Lows?",
                     "impact": 0.66, "score": -0.56, "ev": "",
                     "source": "Finnhub", "n_same": 1}]},
         {"theme": "白宫/贸易政策", "tone": {"利空": 1},
          "items": [{"title": "国务院关税税则委员会：11月10日起2个月内继续暂停实施24%的对美加征关税税率",
                     "impact": 0.64, "score": -0.81, "ev": "", "source": "东财全球资讯",
                     "n_same": 3}]}],
        {"AI": 5, "算力": 4, "回购": 3, "机器人": 2, "关税": 2},
        {"利好": 5, "利空": 3}, {"同题": 12, "主题配额": 0, "注意力预算": 49})
    out = tmp_path / "card.png"
    res = card.render(payload, str(out))
    assert out.exists()
    head = out.read_bytes()[:8]
    assert head[:4] == b"\x89PNG"
    assert res["width"] == card.DEFAULT_WIDTH
    assert res["height"] > 400
    assert res["bytes"] < card.MAX_IMAGE_BYTES
    assert os.path.getsize(str(out)) == res["bytes"]


@pytest.mark.skipif(not _pil_available() or not card.find_font()[0],
                    reason="需要 Pillow 与中文字体")
def test_render_shrinks_cloud_band_when_few_words(tmp_path):
    """热词只有几个时词云条带跟着收缩，否则图上是一大片空白。"""
    def payload(kw):
        return card.build_payload("2026-11-09 15:05", {"pushed": 2},
                                  [{"theme": "AI/算力", "tone": {"利好": 2},
                                    "items": [{"title": "标题甲", "impact": 0.7, "score": 0.5,
                                               "source": "东财"},
                                              {"title": "标题乙", "impact": 0.6, "score": -0.4,
                                               "source": "同花顺"}]}],
                                  kw, {"利好": 2}, {"同题": 1})
    many = card.render(payload({f"词{i}": 9 - i for i in range(8)}), str(tmp_path / "a.png"))
    few = card.render(payload({"算力": 9, "回购": 6}), str(tmp_path / "b.png"))
    assert many["height"] - few["height"] == card.CLOUD_H - max(150, 96 + 33 * 2)
    assert few["height"] < many["height"]


@pytest.mark.skipif(not _pil_available() or not card.find_font()[0],
                    reason="需要 Pillow 与中文字体")
def test_render_missing_font_raises_instead_of_boxes(tmp_path, monkeypatch):
    """字体探测失败要明确报错，不能退化成中文方框图（图片是给人看的，方框等于没做）。"""
    monkeypatch.setattr(card, "find_font", lambda path="", bold=True: ("", 0))
    with pytest.raises(RuntimeError):
        card.render({"round_id": "x", "groups": []}, str(tmp_path / "c.png"))
