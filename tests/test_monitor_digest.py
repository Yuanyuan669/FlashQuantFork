"""聚合推送摘要（digest_line + digest_group + build_digest）单元测试。

2026-11-08 一版：条目从「标题 + 情绪分行 + 原文行（≤160 字）」瘦身为两行。
2026-11-09 二版：用户反馈平铺条目「抓不到重点」，改为按主题分组 + 抬头给
主线/多空统计；单行不再重复主题名，标题截断 64→48 字，并新增 ×N源 多源互证标记。
"""

from news_aggregator.monitor import (_event_dir, _tone_str, build_digest, clip_title,
                                     digest_group, digest_keywords, digest_line,
                                     digest_mainline, order_themes_by_title)

HEADER = ("**[事件告警汇总] 11-06 15:07 · 推送 15 条（过阈值 169 / 新增 901）**\n"
          "热词：算力9 · 回购6 · 三季报5 · 中标3 · 减持2\n"
          "主线：财报/公告 5 · AI/算力 4 · 机器人 3 · 中国外交/地缘 2 · 券商/金融 1\n"
          "多空：利好9/利空3/中性3\n"
          "抑制：同题 42 · 主题配额 88 · 预算 24（落选明细见 dashboard 告警历史）")


def _line(i, n_same=1):
    return digest_line(f"新闻标题{i}", 0.62, 0.8, "财报预增(利好)", "东财全球资讯", n_same)


def test_digest_line_compact():
    e = _line(1)
    assert e.startswith("· ")
    assert "新闻标题1" in e
    assert "0.62" in e and "+0.80" in e
    assert "东财全球资讯" in e and "财报预增(利好)" in e
    # 分组后单行不再带主题名：目标 < 160 字节（一版两行约 180 字节，旧格式约 330）
    assert len(e.encode("utf-8")) < 160


def test_digest_line_truncates_long_title():
    e = digest_line("长" * 200, 0.5, -0.3)
    assert "…" in e
    assert len(e.encode("utf-8")) < 200


def test_digest_line_multi_source_badge():
    assert "×3源" in _line(1, n_same=3)
    assert "×" not in _line(1, n_same=1)     # 单源不加徽标（省字节）


def test_digest_group_counts_items_not_detail_lines():
    g = digest_group("财报/公告",
                     [_line(1), "  - 算力概念(概念)：润建股份 +10.0%", _line(2)],
                     "利好2")
    assert g.startswith("**财报/公告（2，利好2）**")
    assert "算力概念" in g


def test_tone_str():
    assert _tone_str({"利好": 4, "利空": 1}) == "利好4/利空1"
    assert _tone_str({"中性": 2}) == "中性2"
    assert _tone_str({}) == ""


# ---------------- 关键词权重榜（文本版词云） ----------------

def test_keywords_ranked_by_count_desc():
    got = digest_keywords({"算力": 9, "回购": 6, "三季报": 5})
    assert got == "算力9 · 回购6 · 三季报5"


def test_keywords_respect_top_n():
    counter = {f"关键词{i:02d}": 20 - i for i in range(12)}
    got = digest_keywords(counter, top_n=8)
    parts = got.split(" · ")
    assert len(parts) == 8
    assert parts[0] == "关键词0020" and parts[-1] == "关键词0713"


def test_keywords_tie_broken_by_name_for_determinism():
    """同频次时按词名排序：同一轮重跑必须得到逐字相同的摘要。"""
    assert digest_keywords({"b词": 3, "a词": 3}) == "a词3 · b词3"


def test_keywords_empty_cases():
    assert digest_keywords({}) == ""
    assert digest_keywords({"算力": 5}, top_n=0) == ""


# ---------------- 多空方向 / 主线排序 ----------------

def test_event_dir_prefers_event_then_falls_back_to_score():
    """实测 15 条里 11 条没有事件类型：不回落到情绪分就会全记「中性」。"""
    assert _event_dir(("财报预增", "利好"), -0.90) == "利好"   # 事件分类优先
    assert _event_dir(None, 0.56) == "利好"
    assert _event_dir(None, -0.56) == "利空"
    assert _event_dir(None, 0.05) == "中性"                   # 两边都不足则不表态
    assert _event_dir(("某事件", ""), 0.40) == "利好"         # 事件给了空方向


def test_mainline_sorted_by_count_not_by_appearance():
    order = ["白宫/贸易政策", "机器人", "财报/公告", "AI/算力", "券商/金融"]
    counts = {"白宫/贸易政策": 1, "机器人": 3, "财报/公告": 3,
              "AI/算力": 3, "券商/金融": 2}
    got = digest_mainline(counts, order)
    assert got.startswith("机器人 3 · 财报/公告 3 · AI/算力 3")   # 同条数保持出现序
    assert got.endswith("白宫/贸易政策 1")


def test_mainline_empty():
    assert digest_mainline({}, []) == "（无）"


def test_digest_chunks_respect_byte_limit():
    idx = [i for k in range(20) for i in range(k * 3, k * 3 + 6)]
    groups = [digest_group(f"主题{k}", [_line(i) for i in range(k * 3, k * 3 + 6)])
              for k in range(20)]
    chunks = build_digest(groups, max_bytes=3800)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.encode("utf-8")) <= 3800
    joined = "\n".join(chunks)
    assert all(f"新闻标题{i}" in joined for i in idx)


def test_digest_header_in_every_chunk_without_overflow():
    groups = [digest_group(f"主题{k}", [_line(i) for i in range(k * 3, k * 3 + 6)])
              for k in range(20)]
    chunks = build_digest(groups, HEADER, max_bytes=3800)
    assert len(chunks) > 1
    for c in chunks:
        assert c.startswith(HEADER)
        assert len(c.encode("utf-8")) <= 3800


def test_group_is_never_split_across_chunks():
    """分组是原子单位：即使单组超预算也整块成一条消息，不把组标题和条目拆开。"""
    big = digest_group("财报/公告", [_line(i) for i in range(40)])
    chunks = build_digest([big], HEADER, max_bytes=1200)
    assert len(chunks) == 1
    assert chunks[0].count("**财报/公告（40）**") == 1


def test_typical_round_is_a_single_message():
    """核心指标：每轮预算 15 条（约 5 个主题组）只产生 1 条企微消息。"""
    groups = [digest_group(f"主题{k}", [_line(i) for i in range(k * 3, k * 3 + 3)],
                           "利好3") for k in range(5)]
    chunks = build_digest(groups, HEADER)
    assert len(chunks) == 1
    assert len(chunks[0].encode("utf-8")) <= 3800


def test_digest_empty():
    assert build_digest([]) == []
    assert build_digest([], HEADER) == []


# ---------------- 标题收刀 + 主题排序（2026-11-09 三版） ----------------

def test_clip_title_cuts_at_clause_boundary():
    """48 字硬截会砍出「（15905…」这种半截括号，改成句读收刀。"""
    t = ("苹果发布机器人自主控制系统HALO、全球科技巨头加速布局机器人产业！"
         "机器人ETF广发（159050）标的指数盘中一度涨超4%")
    out = clip_title(t, 48)
    assert out == "苹果发布机器人自主控制系统HALO、全球科技巨头加速布局机器人产业"
    assert "…" not in out                 # 收成一句完整话，不加省略号
    assert "ETF" not in out               # 盘面噪声尾巴被丢掉


def test_clip_title_keeps_numbers_intact():
    """边界集合里不能有 ASCII 的 . 和 ,，否则「增长24.8%」会被砍成「增长24」。"""
    t = "中国建筑前三季度归母净利润同比下降24.8%，公司称系行业结算节奏变化所致影响有限"
    out = clip_title(t, 30)
    assert "24.8%" in out
    assert not out.endswith(".") and not out.endswith(",")


def test_clip_title_short_and_hard_cases():
    assert clip_title("短标题", 48) == "短标题"
    assert clip_title("无边界" * 30, 20).endswith("…")


def test_clip_title_english_backs_off_word_boundary():
    """2026-11-09 生产 dry-run 实测：英文逐股标题没有中文句读，硬切会砍出「52-Wee…」。"""
    t = "Why Did DKNG, SOUN, ORCL Stocks Plunge To 52-Week Lows On Tuesday?"
    out = clip_title(t, 48)
    assert out == "Why Did DKNG, SOUN, ORCL Stocks Plunge To"  # 退回空格，不是 52- 的连字符
    assert not out.endswith("…")
    # 词边界太靠前（低于 limit*0.55）时宁可保留断词，也不把标题截成三个词
    long_token = "Aa Bb " + "X" * 45 + " tail"
    assert clip_title(long_token, 48).endswith("…")


def _th(name, kws):
    return {"name": name, "keywords": kws, "macro": False}


def test_order_themes_prefers_title_hits():
    """「军工电子板块走高」正文带 ETF 通稿尾巴（成交额），分组必须落在军工。"""
    th_jr, th_wg = _th("券商/金融", ["成交额", "证券"]), _th("军工", ["军工", "航天"])
    title = "军工电子板块走高，铖昌科技涨停"
    content = "航天发展、华力创通等走高，军工龙头ETF富国（512710）涨1.95%，成交额1.6亿元"
    matched = [(th_jr, ["成交额"]), (th_wg, ["军工", "航天"])]      # themes.yaml 原序
    out = order_themes_by_title(matched, title)
    assert [t["name"] for t, _ in out] == ["军工", "券商/金融"]


def test_order_themes_keeps_order_when_title_hits_nothing():
    matched = [(_th("券商/金融", ["成交额"]), ["成交额"]),
               (_th("军工", ["航天"]), ["航天"])]
    assert order_themes_by_title(matched, "某条正文里带这些词但标题没写") == matched


def test_order_themes_more_title_keywords_wins():
    a, b = _th("AI/算力", ["AI"]), _th("机器人", ["机器人", "具身智能"])
    matched = [(a, ["AI", "算力"]), (b, ["机器人", "具身智能"])]
    out = order_themes_by_title(matched, "智元机器人具身智能医疗应用与AI落地")
    assert out[0][0]["name"] == "机器人"        # 标题里中了两个词，压过只中一个的
