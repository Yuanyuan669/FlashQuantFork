"""告警筛选层单元测试：同题去重（含数字护栏）、主题配额、注意力预算、跨轮冷却。

样本标题取自 news/alerts.jsonl 的真实回放（2026-11-08），既覆盖多源转述的真重复，
也覆盖模板化标题的误合并风险。
"""

from datetime import datetime, timedelta

from news_aggregator.alert_filter import (
    REASON_BUDGET, REASON_DUP_COOLDOWN, REASON_DUP_ROUND, REASON_THEME_QUOTA,
    Cooldown, common_prefix_len, core_title, fingerprint, is_same_event, jaccard,
    normalize_title, overlap_coef, select_alerts, summarize_suppressed)


def _cand(i, impact=0.6, theme="AI/算力", title=None, score=0.5, macro=False):
    title = title if title is not None else f"新闻{i}"
    return {"item": {"id": str(i), "title": title}, "impact": impact,
            "score": score, "themes": [theme], "macro": macro,
            "fp": fingerprint(title)}


# ---------------- 同题判定 ----------------

def test_normalize_strips_punctuation_and_case():
    assert normalize_title("欧洲央行管委称：欧元区经济 有韧性!") == "欧洲央行管委称欧元区经济有韧性"
    assert normalize_title("Dow Jones Futures Fall") == "dowjonesfuturesfall"


def test_identical_title_is_same_event():
    a = fingerprint("也门胡塞武装称毙伤沙特支持的武装人员逾200人")
    b = fingerprint("也门胡塞武装称毙伤沙特支持的武装人员逾200人")
    assert is_same_event(a, b)


def test_multi_source_paraphrase_is_same_event():
    """同一事件被通讯社加前缀转述：Jaccard 只有 0.75，靠子串包含并掉。"""
    a = fingerprint("财联社10月8日电，美国健康保险公司股走弱，Humana跌3.2%，联合健康跌1.5%。")
    b = fingerprint("美国健康保险公司股走弱，Humana跌3.2%，联合健康跌1.5%")
    assert jaccard(a["bg"], b["bg"]) < 0.80          # 不是靠 Jaccard 并的
    assert is_same_event(a, b)
    assert not is_same_event(a, b, substring_containment=False)


def test_title_plus_lead_is_same_event_by_substring():
    """一家源把「标题+导语」塞进 title（实测样本，Jaccard 0.58）。"""
    a = fingerprint("商务部：对原产于欧盟、日本的进口乙叉降冰片烯发起反倾销立案调查")
    b = fingerprint("【商务部：对原产于欧盟、日本的进口乙叉降冰片烯发起反倾销立案调查】"
                    "金十数据11月6日讯，自本公告发布之日起，商务部对原产")
    assert jaccard(a["bg"], b["bg"]) < 0.80
    assert overlap_coef(a["bg"], b["bg"]) == 1.0
    assert is_same_event(a, b)


def test_templated_english_column_titles_are_not_same_event():
    """回归：同栏位稿只差实体名（实测 Jaccard 0.74），必须判为不同事件。

    这是 jac_threshold 取 0.80 而不是 0.75 的原因。
    """
    a = fingerprint("What Is Going On With Meta Stock on Tuesday?")
    b = fingerprint("What Is Going on With Nvidia Stock on Tuesday?")
    assert 0.70 < jaccard(a["bg"], b["bg"]) < 0.80
    assert not is_same_event(a, b)
    # 若把阈值降到 0.70 就会误并——说明这条用例真的在守门
    assert is_same_event(a, b, jac_threshold=0.70)


def test_unrelated_long_english_titles_are_not_same_event():
    """回归：英文归一化去空格后二元组类型饱和，重叠系数会误并；字面规则必须挡住。"""
    a = fingerprint("Riot Platforms, IREN, and MARA Drop Further as Bitcoin Miners Slide")
    b = fingerprint("My decision on Russia is a very simple one. I am in the process of "
                    "deciding whether or not to sanction Putin for ending the war")
    assert jaccard(a["bg"], b["bg"]) < 0.25          # 语义上确实不相干
    assert overlap_coef(a["bg"], b["bg"]) > 0.40      # 旧的重叠系数规则会把它拉到 2 倍
    assert not is_same_event(a, b)


def test_short_generic_title_not_swallowed():
    """长度门槛：11 字的通用公告标题不该被任意长标题吞掉。"""
    a = fingerprint("关于回购公司股份的公告")
    b = fingerprint("浙江某股份有限公司关于回购公司股份的公告暨召开股东大会的通知")
    assert len(a["nt"]) < 12
    assert not is_same_event(a, b)


def test_number_guard_is_load_bearing_for_long_financial_titles():
    """长财报标题只差一位数字时 Jaccard 已达 0.882，数字护栏是唯一保护。"""
    a = fingerprint("宁德时代发布2026年第三季度报告净利润同比增长15.2%超市场预期")
    b = fingerprint("宁德时代发布2026年第三季度报告净利润同比增长16.2%超市场预期")
    assert jaccard(a["bg"], b["bg"]) >= 0.80         # 光靠相似度会误并
    assert not is_same_event(a, b)                   # 护栏挡住了
    assert is_same_event(a, b, require_same_numbers=False)


def test_dateline_numbers_do_not_block_dedupe():
    """数字护栏用子集判定：前缀里的「10月8日」不该让真重复漏网。"""
    a = fingerprint("前三季度中国GDP同比增长4.6%")
    b = fingerprint("2026年前三季度中国GDP同比增长4.6%")
    assert is_same_event(a, b)


def test_templated_titles_with_different_dates_are_not_same_event():
    """龙虎榜/公告这类模板标题：只有日期不同也必须判为不同事件。"""
    a = fingerprint("万科A000002龙虎榜数据09-29")
    b = fingerprint("万科A000002龙虎榜数据09-30")
    assert abs(jaccard(a["bg"], b["bg"]) - 0.75) < 0.01   # 短模板标题本就落在阈值下
    assert not is_same_event(a, b)
    assert not is_same_event(a, b, require_same_numbers=False)


def test_empty_title_never_matches():
    assert not is_same_event(fingerprint(""), fingerprint(""))
    assert not is_same_event(fingerprint(""), fingerprint("某标题"))


# ---------------- 主干前缀（2026-11-09 新增，治改写式通稿） ----------------

def test_core_title_cuts_market_tail():
    assert core_title("苹果研究团队披露机器人“大脑”HALO进展！机器人ETF万家标的指数涨超3%！") \
        == "苹果研究团队披露机器人“大脑”HALO进展"
    # 主干太短（<8 字）就退回整标题，别让「苹果！」这种退化主干去吞不相干条目
    assert core_title("苹果！大涨") == "苹果！大涨"
    assert core_title("中国建筑前三季度归母净利润同比下降24.8%") \
        == "中国建筑前三季度归母净利润同比下降24.8%"     # 无盘面尾巴 → 主干即全标题


def test_fingerprint_carries_core_and_core_numbers():
    fp = fingerprint("苹果研究团队披露机器人“大脑”HALO进展，科创AIETF鹏华涨超2%")
    assert fp["core"].startswith("苹果研究团队披露机器人")
    assert "2" not in "".join(fp["cnums"])              # 尾巴里的 2% 不在主干数字里
    assert "2" in "".join(fp["nums"])                   # 全标题数字仍保留


def test_paraphrased_etf_wire_is_same_event_by_prefix():
    """11:36 那轮真实缺陷：同一 HALO 事件的两条转述因尾部 3%/2% 被数字护栏拆开。"""
    a = fingerprint("苹果研究团队披露机器人“大脑”HALO进展！机器人ETF万家标的指数涨超3%！")
    b = fingerprint("苹果研究团队披露机器人“大脑”进展，科创AIETF鹏华涨超2%")
    assert jaccard(a["bg"], b["bg"]) < 0.80                 # 相似度规则并不了
    assert not is_same_event(a, b, min_prefix_chars=0)      # 关掉前缀规则＝旧行为：尾部 3%/2% 被数字护栏拆开
    assert is_same_event(a, b)                              # 默认开：主干前缀把两条转述并上


def test_prefix_rule_still_respects_core_numbers():
    """主干里数字不同（不是盘面尾巴）时，前缀规则也不许并。"""
    a = fingerprint("宁德时代发布2026年第三季度报告净利润同比增长15.2%！机器人ETF跟涨")
    b = fingerprint("宁德时代发布2026年第三季度报告净利润同比增长16.2%！机器人ETF跟涨")
    assert not is_same_event(a, b, min_prefix_chars=12)


def test_prefix_rule_needs_majority_of_shorter_core():
    """同栏位英文专栏：公共前缀 17 字够长，但只占较短主干 0.49，不该被并。"""
    a = fingerprint("What Is Going On With Meta Stock on Tuesday?")
    b = fingerprint("What Is Going on With Nvidia Stock on Tuesday?")
    assert common_prefix_len(a["core"], b["core"]) >= 12
    assert not is_same_event(a, b)


def test_legacy_state_fingerprint_without_core_still_works(tmp_path):
    """旧 alert_state.json 只有 nt/nums：载入不炸，冷却照常命中。"""
    import json

    p = tmp_path / "alert_state.json"
    payload = {"version": 1, "hours": 12, "pushed": [
        {"nt": "国务院印发新一轮稳增长政策", "nums": [], "ts": datetime.now().isoformat()}]}
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    cd = Cooldown(hours=12).load(p)
    assert len(cd) == 1
    assert cd.hit(fingerprint("国务院印发新一轮稳增长政策"))


# ---------------- 轮内去重 / 配额 / 预算 ----------------

def test_within_round_dedupe_keeps_highest_impact():
    cands = [_cand(1, 0.55, title="以色列政府发出旅行警告 提醒海外公民防范潜在安全风险"),
             _cand(2, 0.70, title="以色列政府发出旅行警告 提醒海外公民防范潜在安全风险"),
             _cand(3, 0.50, title="完全无关的另一条新闻")]
    picked, suppressed = select_alerts(cands, max_alerts=10, max_per_theme=0)
    assert [c["item"]["id"] for c in picked] == ["2", "3"]     # 影响分降序
    assert summarize_suppressed(suppressed) == {REASON_DUP_ROUND: 1}


def test_representative_carries_multi_source_count():
    """n_same = 1 + 被它吸收的同题转述数，摘要里渲染成 ×N源（多源互证信号）。"""
    same = "多源报道：某地宣布新一轮算力基建投资计划落地"
    cands = [_cand(1, 0.60, title=same), _cand(2, 0.80, title=same),
             _cand(3, 0.55, title=same), _cand(4, 0.90, title="独家：另一件事")]
    picked, _ = select_alerts(cands, max_alerts=10, max_per_theme=0)
    by_id = {c["item"]["id"]: c for c in picked}
    assert by_id["2"]["n_same"] == 3          # 影响分最高的代表吸收其余两条
    assert by_id["4"]["n_same"] == 1          # 无同题时保持 1，不渲染徽标


def test_cooldown_hit_does_not_inflate_multi_source_count():
    """跨轮冷却命中的候选不产生代表，也不会被计入别条的 n_same。"""
    old = "上一轮已推送：某部委发布算力基础设施专项行动方案"
    new = "本轮独立事件：另一家公司公告定向增发募资扩产"
    cooldown = Cooldown(hours=12)
    cooldown.remember(title=old)
    cands = [_cand(1, 0.90, title=old), _cand(2, 0.85, title=new)]
    picked, suppressed = select_alerts(cands, cooldown=cooldown,
                                       max_alerts=10, max_per_theme=0)
    assert [c["item"]["id"] for c in picked] == ["2"]
    assert picked[0]["n_same"] == 1
    assert summarize_suppressed(suppressed) == {REASON_DUP_COOLDOWN: 1}


def test_theme_quota_spreads_budget_across_themes():
    cands = ([_cand(i, 0.9 - i * 0.01, theme="财报/公告") for i in range(6)]
             + [_cand(100 + i, 0.6 - i * 0.01, theme="AI/算力") for i in range(3)])
    picked, suppressed = select_alerts(cands, max_alerts=15, max_per_theme=3)
    themes = [c["themes"][0] for c in picked]
    assert themes.count("财报/公告") == 3
    assert themes.count("AI/算力") == 3
    assert summarize_suppressed(suppressed)[REASON_THEME_QUOTA] == 3


def test_budget_caps_round_and_keeps_top_impact():
    cands = [_cand(i, impact=round(0.45 + i * 0.01, 3)) for i in range(30)]
    picked, suppressed = select_alerts(cands, max_alerts=15, max_per_theme=0)
    assert len(picked) == 15
    assert picked[0]["impact"] == max(c["impact"] for c in cands)
    assert min(c["impact"] for c in picked) > max(
        c["impact"] for c, reason in suppressed if reason == REASON_BUDGET)
    assert summarize_suppressed(suppressed)[REASON_BUDGET] == 15


def test_macro_reserve_keeps_market_level_news():
    """预算 8 时影响分排第 10 的宏大条目（关税/CPI 那类）本来被板块快讯挤掉，预留 1 名保住。"""
    sector = [_cand(i, impact=round(0.78 - i * 0.02, 3)) for i in range(9)]      # 0.78..0.62
    macro = _cand(100, impact=0.60, theme="白宫/贸易政策", macro=True)
    cands = sector + [macro]
    ids0 = [c["item"]["id"] for c in
            select_alerts(cands, max_alerts=8, max_per_theme=0, macro_reserve=0)[0]]
    assert "100" not in ids0                                                    # 旧行为：宏大条目出局
    picked, _ = select_alerts(cands, max_alerts=8, max_per_theme=0, macro_reserve=1)
    ids = [c["item"]["id"] for c in picked]
    assert "100" in ids and len(picked) == 8     # 预留是「置换」不是「加量」，总条数不突破预算


def test_macro_reserve_unused_slots_return_to_general_pool():
    """没有宏大候选时预留名额自动退回全池，不该出现「本轮只推 6 条」。"""
    cands = [_cand(i, impact=round(0.78 - i * 0.02, 3)) for i in range(20)]
    picked, _ = select_alerts(cands, max_alerts=8, max_per_theme=0, macro_reserve=2)
    assert len(picked) == 8
    assert all(not c.get("macro") for c in picked)


def test_macro_reserve_never_exceeds_budget():
    """预留数大于预算时按预算封顶。"""
    cands = ([_cand(100 + i, impact=0.40, theme="白宫/贸易政策", macro=True) for i in range(4)]
             + [_cand(i, impact=0.70) for i in range(3)])
    picked, _ = select_alerts(cands, max_alerts=3, max_per_theme=0, macro_reserve=3)
    assert len(picked) == 3 and all(c.get("macro") for c in picked)


def test_reserve_claims_dedupe_and_picked_stays_impact_sorted():
    """预留候选同样吃同题去重；名额退回全池；展示顺序仍按影响分降序（预留不是插队到最前）。"""
    cands = [_cand(i, impact=round(0.78 - i * 0.02, 3)) for i in range(9)]
    m1 = _cand(100, impact=0.60, theme="白宫/贸易政策", macro=True, title="关税暂停两个月")
    m2 = _cand(101, impact=0.59, theme="白宫/贸易政策", macro=True, title="关税暂停两个月")
    picked, suppressed = select_alerts(cands + [m1, m2], max_alerts=8,
                                       max_per_theme=0, macro_reserve=2)
    ids = [c["item"]["id"] for c in picked]
    assert "101" not in ids                                   # 同题转述不占第二个预留名额
    assert summarize_suppressed(suppressed)[REASON_DUP_ROUND] >= 1
    assert len(picked) == 8                                   # 省下的名额退回全池
    assert [c["impact"] for c in picked] == sorted((c["impact"] for c in picked), reverse=True)


def test_zero_budget_means_unlimited():
    cands = [_cand(i) for i in range(5)]
    picked, suppressed = select_alerts(cands, max_alerts=0, max_per_theme=0)
    assert len(picked) == 5 and not suppressed


# ---------------- 跨轮冷却 ----------------

def test_cooldown_suppresses_repeat_in_later_round():
    cd = Cooldown(hours=12)
    t0 = datetime(2026, 11, 6, 15, 0, 0)
    # 真实回放样本：同一场讲话被两家源转录，仅标点不同
    r1 = [_cand(1, title="欧洲央行管委科赫尔称，欧元区经济对冲击表现出韧性，能源价格对预测构成上行风险")]
    p1, s1 = select_alerts(r1, cooldown=cd, max_per_theme=0)
    assert len(p1) == 1 and not s1
    for c in p1:
        cd.remember(fp=c["fp"], ts=t0)

    r2 = [_cand(2, title="欧洲央行管委科赫尔称：欧元区经济对冲击表现出韧性 能源价格对预测构成上行风险")]
    p2, s2 = select_alerts(r2, cooldown=cd, max_per_theme=0)
    assert p2 == []
    assert summarize_suppressed(s2) == {REASON_DUP_COOLDOWN: 1}


def test_cooldown_suppresses_title_plus_extension():
    """已知取舍：旧标题后多带半句的转述，冷却窗内按子串包含判同题（有意降噪）。"""
    t0 = datetime(2026, 11, 6, 15, 0, 0)
    old = "欧洲央行管委科赫尔称，欧元区经济对冲击表现出韧性"
    new = "欧洲央行管委科赫尔称，欧元区经济对冲击表现出韧性，能源价格对预测构成上行风险"
    cd = Cooldown(hours=12)
    cd.remember(old, ts=t0)
    picked, suppressed = select_alerts([_cand(2, title=new)], cooldown=cd, max_per_theme=0)
    assert picked == []
    assert summarize_suppressed(suppressed) == {REASON_DUP_COOLDOWN: 1}

    # 关掉子串包含和前缀规则才不会被吞（2026-11-09 加了主干前缀规则后，
    # 单关子串仍会被前缀规则并掉——这条转述主干公共前缀 22 字、占短主干 0.61）：
    # 落选条目仍写 alerts.jsonl，alpha 损失交 alert_review 校验
    cd2 = Cooldown(hours=12, substring_containment=False, min_prefix_chars=0)
    cd2.remember(old, ts=t0)
    picked2, sup2 = select_alerts([_cand(3, title=new)], cooldown=cd2, max_per_theme=0)
    assert len(picked2) == 1 and not sup2


def test_cooldown_does_not_suppress_different_event():
    """同一个人同一场讲话的另一个论点（实测 Jaccard 0.20），不该被冷却吞掉。"""
    cd = Cooldown(hours=12)
    cd.remember("欧洲央行管委科赫尔称，欧元区经济对冲击表现出韧性",
                ts=datetime(2026, 11, 6, 15, 0, 0))
    r2 = [_cand(2, title="欧洲央行管委科赫尔称，通胀回落进程可能比预期更慢，需要维持限制性利率")]
    picked, suppressed = select_alerts(r2, cooldown=cd, max_per_theme=0)
    assert len(picked) == 1 and not suppressed


def test_cooldown_expires_after_window():
    cd = Cooldown(hours=12)
    t0 = datetime(2026, 11, 6, 15, 0, 0)
    cd.remember("某条已推送的新闻标题", ts=t0)
    cd.prune(t0 + timedelta(hours=11))
    assert cd.hit(fingerprint("某条已推送的新闻标题"))
    cd.prune(t0 + timedelta(hours=13))
    assert not cd.hit(fingerprint("某条已推送的新闻标题"))
    assert len(cd) == 0


def test_cooldown_disabled_short_circuits():
    cd = Cooldown(hours=0)
    cd.remember("某条标题")
    assert not cd.hit(fingerprint("某条标题"))


def test_cooldown_state_roundtrip(tmp_path):
    p = tmp_path / "alert_state.json"
    cd = Cooldown(hours=12).load(p)
    cd.remember("国务院印发新一轮稳增长政策", ts=datetime(2026, 11, 6, 15, 0, 0))
    cd.save()
    again = Cooldown(hours=12).load(p)
    again.prune(datetime(2026, 11, 6, 18, 0, 0))
    assert len(again) == 1
    assert again.hit(fingerprint("国务院印发新一轮稳增长政策"))


def test_cooldown_load_missing_file_is_safe(tmp_path):
    import json as _json

    p = tmp_path / "nope.json"
    cd = Cooldown().load(p)
    assert len(cd) == 0
    cd.save()                       # 不该抛异常
    assert _json.loads(p.read_text(encoding="utf-8"))["pushed"] == []
