"""情绪打分单元测试：覆盖中英文词典、否定窗口、边界与范围。"""

from news_aggregator.sentiment import (score_text, score_text_en, score_text_zh,
                                       strip_market_wrap)


def test_positive_zh():
    assert score_text_zh("公司业绩大幅增长") > 0


def test_negative_zh():
    assert score_text_zh("公司亏损严重") < 0


def test_neg_growth_is_negative():
    """回归：负增长不应被当成正面（此前子串计数会把“增长”算正）。"""
    assert score_text_zh("公司业绩负增长") < 0


def test_not_growth_is_negative():
    assert score_text_zh("公司业绩没有增长") < 0


def test_avoid_risk_is_positive():
    """否定词反转：避免风险 = 正面。"""
    assert score_text_zh("公司有效避免风险") > 0


def test_future_growth_is_positive():
    """回归：未来增长不应因“未”字被误判为否定。"""
    assert score_text_zh("公司未来三年保持增长") > 0


def test_downslide_is_negative():
    assert score_text_zh("营收同比下滑") < 0


def test_score_range_zh():
    for t in ["利好大涨", "利空暴跌", "中性文本", ""]:
        assert -1.0 <= score_text_zh(t) <= 1.0


def test_english_negation():
    assert score_text_en("no growth expected") < 0
    assert score_text_en("strong growth expected") > 0


def test_score_text_routes():
    # 统一入口：中文走中文词典，英文走英文词典，空文本为 0
    assert score_text("") == 0.0
    assert -1.0 <= score_text("公司利好") <= 1.0
    assert -1.0 <= score_text("company beat estimates") <= 1.0


# ---------- 行情综述段不计入情绪（2026-11-09） ----------

_MKT_TITLE = "中际旭创成交额A股第一"
_MKT_BODY = ("【中际旭创成交额A股第一】11月9日早盘，A股主要指数集体上涨。截至午间收盘，"
             "上证指数报3665.94点，涨0.46%；深证成指涨0.27%，创业板指涨0.56%，"
             "科创综指涨0.78%。沪深北三市半日成交10181亿元；"
             "中际旭创成交额167.40亿元，位居A股第一。")


def test_market_wrap_text_scores_neutral():
    """回归：正文里的大盘综述曾被逐条记为利好（+0.83），而标题本身是 0 分。"""
    assert score_text_zh(_MKT_TITLE, _MKT_TITLE) == 0.0
    assert score_text_zh(_MKT_TITLE + " " + _MKT_BODY, _MKT_TITLE) == 0.0


def test_strip_market_wrap_keeps_event_sentence():
    assert strip_market_wrap(_MKT_BODY) == "中际旭创成交额167.40亿元，位居A股第一。"


def test_strip_market_wrap_keeps_real_news():
    """带指数名但没有行情数字的句子不能删（事件句优先）。"""
    s = "沪深300指数基金获批，首募规模50亿元"
    assert strip_market_wrap(s) == s
    t = "宁德时代：前三季度净利润同比增长35%"
    assert strip_market_wrap(t) == t and score_text_zh(t, t) > 0.3
