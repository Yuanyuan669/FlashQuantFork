"""资金面标签（news_aggregator/fundflow.py）单元测试：只测格式化与失败降级。

真正的东财请求不在这里跑（网络、20s、限频），靠 snapshot() 的异常分支保证
「拿不到就返回空」，调用方据此不显示资金面。
"""

from news_aggregator import fundflow


def test_fmt_net_signs_and_units():
    assert fundflow.fmt_net(668_769_792) == "+6.7亿"
    assert fundflow.fmt_net(-1_234_000_000) == "-12.3亿"
    assert fundflow.fmt_net(35_000_000) == "+3500万"
    assert fundflow.fmt_net(-2_000) == "-2000元"
    assert fundflow.fmt_net(0) == "+0元"


def test_fmt_tag_includes_change():
    tag = fundflow.fmt_tag({"net": 668_769_792, "chg": 0.9})
    assert tag == "主力+6.7亿(涨+0.9%)"
    assert fundflow.fmt_tag({}) == ""


def test_snapshot_empty_and_bad_codes_without_network():
    """空输入/非 6 位代码直接返回空，不会去连东财。"""
    assert fundflow.snapshot([]) == {}
    assert fundflow.snapshot(["AAPL", "", "300308.0"]) == {}
