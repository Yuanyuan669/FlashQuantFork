"""中英文金融情绪打分（可插拔后端，与量化策略共用）。

后端：
- lexicon : 离线词典（默认、零依赖、永远可用）
- finbert : FinBERT 系列（transformers，首次评分时惰性加载，失败回退词典）
- llm     : OpenAI 兼容 chat/completions（结构化 JSON 输出，失败回退词典）

统一入口：
- score_text(text)      -> [-1, 1]，动态路由到当前后端
- score_batch(texts)    -> list[float]
- configure_backend(cfg) 读取 config['sentiment']['backend'] 切换后端

旧接口兼容：CN_POS / CN_NEG / score_text 仍可正常 import。
"""

import json
import os
import re

# 词典版本：写入产物的情绪分应带上它，便于区分"哪一版词典打的分"
# 2026-11-08 zh-v2.1：覆盖率 + 极性判定修复。120 条人工标注样本上，相对 zh-v1：
#   非中性召回率 34.0% -> 92.6%，符号正确率 29.8% -> 91.5%，方向打反 4 条 -> 1 条，
#   零分率 66.7% -> 15.8%，中性强误报率(|分|>=0.3) 30.8% -> 30.8%（未变差）。
# 全库 73229 条中文新闻：零分率 69.2% -> 42.7%，|分|>=0.3 占比 29.1% -> 47.4%。
# ⚠ 分布变了 = config.yaml 的 threshold_long/threshold_flat 语义变了，改前必读上面的数。
LEXICON_VERSION = "zh-v2.1/en-v1"

# ---------- 中文 ----------
# zh-v2（2026-11-08）：原 62 词对 A 股快讯覆盖不足——实测 56381 条中文新闻里
# 66.8% 一个词都命中不到（120 条人工标注样本上非中性召回率仅 34.0%）。
# 新增词全部来自 23653 条"零命中"语料的高频 n-gram 挖掘（频次≥40）+ 金融领域惯例，
# 刻意排除歧义词（升级/扩大/增加/调整/完成 等方向随主语变化的词）。
CN_POS = ["利好", "增长", "上涨", "涨停", "突破", "超预期", "回购", "增持", "中标",
          "盈利", "扭亏", "创新高", "获批", "签约", "分红", "业绩预增", "预增", "降准", "降息",
          "刺激", "支持", "扩产", "提价", "涨价", "翻倍", "大涨", "回升", "回暖", "改善", "走强",
          # --- 价格/行情方向 ---
          "上调", "上升", "升至", "涨超", "涨近", "涨幅", "收涨", "领涨", "高开", "拉升",
          "反弹", "飙升", "新高", "连板", "溢价", "增幅", "宽松", "复苏",
          # --- 资金/股东行为 ---
          "净流入", "净买入", "资金流入", "抢筹", "加仓", "认购", "增资",
          # --- 并购/订单/产能 ---
          "收购", "并购", "重组", "订单", "大单", "签署", "合作协议", "战略合作", "合作",
          "投产", "投运", "量产", "达产", "满产", "开工", "开通", "通车", "上线", "落地", "交付",
          "推出", "预售", "开售",
          # --- 审批/监管通过 ---
          "获准", "获受理", "受理", "批准", "备案", "过会", "注册生效",
          # --- 评级/预期 ---
          "看好", "看多", "买进", "买入评级", "增持评级", "信心", "乐观", "坚定",
          "提振", "受益", "赋能", "转型升级", "扶持", "补贴", "减税", "降费", "促就业",
          "领先", "超越", "正向贡献", "溢利",
          # --- 供需/经营正常 ---
          "供不应求", "供应紧张", "吃紧", "需求强劲", "需求旺盛", "满足需求", "供应充足",
          "保障", "一切正常", "有序推进", "顺利", "停火", "缓和", "和平协议"]
CN_NEG = ["利空", "下跌", "跌停", "亏损", "减持", "违规", "处罚", "立案", "调查",
          "退市", "风险", "爆雷", "商誉减值", "质押", "冻结", "诉讼", "下调", "不及预期",
          "预亏", "预减", "停产", "召回", "违约", "债务", "暴跌", "走弱", "承压", "下挫",
          "下滑", "下降", "回落", "低于预期",
          # --- 价格/行情方向 ---
          "降至", "跌超", "跌近", "跌幅", "收跌", "领跌", "低开", "走低", "下行", "跌破",
          "跳水", "重挫", "大跌", "闪崩", "崩盘", "疲软", "低迷", "遇冷", "贬值",
          "放缓", "萎缩", "缩减", "收缩", "降低", "减少", "高企",
          # 注：减速 / 爆炸 / 限制 走 CN_NEG_PAT（需负向前瞻，见下方说明）
          # --- 资金/股东行为 ---
          "净流出", "净卖出", "资金流出", "抛售", "减仓", "清仓",
          # --- 进程受阻 ---
          "暂停", "中止", "终止", "停止", "叫停", "取消", "撤回", "撤单", "告吹", "退出",
          "撤离", "停牌", "预警", "无法持续", "依赖", "短缺", "断供", "供应中断", "瓶颈",
          "卡脖子", "受制", "不得", "禁止", "诱导",
          # --- 监管/法律 ---
          "起诉", "被诉", "仲裁", "警告", "警示", "通报批评", "批评", "谴责", "质疑",
          "罚款", "罚金", "整改", "违法", "失信", "关注函", "监管函", "警示函", "问询",
          "审查", "反垄断", "反倾销", "制裁", "禁令", "封锁", "关税", "暴利税",
          "加税", "征税", "破产", "清算", "重整", "逾期", "坏账", "减值", "计提", "减利",
          # --- 货币/成本 ---
          "加息", "升息", "紧缩", "收紧", "通胀", "通货膨胀", "成本上升",
          # --- 事故/安全/灾害 ---
          "事故", "火灾", "泄漏", "泄露", "黑客", "入侵", "攻击", "瘫痪", "故障",
          "减产", "停工", "罢工", "关停", "关闭", "暴雷", "跑路", "失联",
          "死亡", "遇难", "伤亡", "疫情", "感染", "灾害", "台风", "地震", "洪水", "干旱", "暴雨",
          # --- 地缘/情绪 ---
          "战争", "冲突", "紧张", "担忧", "恐慌", "焦虑", "不确定性", "压力", "应急", "紧急",
          "实弹", "军演", "射击",
          # --- 人事 ---
          "免职", "免去", "撤职", "辞职", "离职", "被免", "留置", "逮捕", "拘留", "双开"]

# 中文否定词（否定窗口用）。刻意排除单字“未/无/非/难”等歧义字，
# 避免“未来”“无锡”“非洲”等词被误判为否定。
CN_NEGATORS = ["不会", "不能", "未能", "没有", "并未", "不再", "尚未", "难以",
               "无法", "避免", "缺乏", "不及", "不", "没", "负"]

# 数值/方向型模式：词典是纯子串匹配，抓不到"涨4%""跌2.77%"这类裸单字+数字的写法
# （"涨"/"跌"单字入典会误伤"涨停板复盘"等，故只以正则形式收）。
# 另有一类必须走正则的：需要负向前瞻排除子串陷阱的词——
#   减速(器) 是机器人零部件、爆炸(式增长) 是利好、限制(性股票) 是股权激励。
CN_POS_PAT = [r"涨\s*\d", r"升\s*\d", r"涨幅扩大", r"站上\s*\d", r"突破\s*\d",
              r"同比(?:增长|增加|上升|提高)", r"环比(?:增长|增加|上升)",
              r"评级.{0,6}(?:上调|买进|买入|增持|优于|跑赢)",
              r"(?:达到|达|触及)主要终点", r"飙到|飙至",
              r"(?:不影响|不改变|不动摇).{0,8}(?:根基|基本面|经营|业绩|长期逻辑)"]
CN_NEG_PAT = [r"跌\s*\d", r"降\s*\d", r"跌幅扩大", r"跌破\s*\d",
              r"同比(?:下降|减少|下滑|降低)", r"环比(?:下降|减少|下滑)",
              r"评级.{0,6}(?:下调|卖出|减持|劣于|跑输)",
              r"减速(?!器)", r"爆炸(?!式)", r"限制(?!性股票)"]

# 上下文反转：同一个词在不同主语下极性相反，纯词典会打反
# （120 条标注样本上 v1 有 4 条方向打反，其中 3 条属于这一类）。
# 形式为 (正则, 目标极性)：命中区间内的所有命中被**强制**为该极性
# （不是盲目取反——否则"增速无法持续"里的利空词 无法持续 会被翻成利好）。
CN_FLIP_PAT = [
    # 通胀/成本/利率/价格上行 = 利空（"日本央行上调明年核心CPI预期"）
    (r"(?:cpi|ppi|通胀|通货膨胀|物价|价格|利率|融资成本|成本|费用|负债|赤字|失业率|失业)"
     r".{0,12}(?:上调|上升|上涨|走高|升高|增加|提高|攀升|升至|涨)", -1),
    # 同上，方向词在前的写法（"上调明年核心CPI预期"）
    (r"(?:上调|上升|上涨|走高|升高|增加|提高|攀升|升至)"
     r".{0,12}(?:cpi|ppi|通胀|通货膨胀|物价|价格|利率|融资成本|成本|费用|负债|赤字|失业率|失业)", -1),
    # 增长/涨幅本身放缓 = 利空（"预警收入增速无法持续"）
    (r"(?:增长|增速|涨幅|营收|利润|业绩).{0,8}(?:放缓|回落|下降|下滑|收窄|减速|无法持续)", -1),
    # 补贴退坡 = 利空
    (r"补贴.{0,6}(?:退坡|减少|取消|下调|退出)", -1),
    # 限制/禁令被放宽 = 利好
    (r"(?:放宽|取消|解除|放开|松绑|下调|降低).{0,8}(?:限制|禁令|管制|门槛|税率|关税|准入)", 1),
    # 跌势/亏损收窄 = 利好
    (r"(?:跌幅|跌势|下跌|降势|亏损|亏).{0,6}(?:收窄|缩窄|缩小|收敛|放缓|减少)", 1),
    # 空头/融券仓位下降 = 利好（"高盛在哔哩哔哩的空头持仓从2.85%降至1.78%"）
    (r"(?:空头|空单|融券|沽空|做空).{0,20}(?:降至|减少|下降|回落|平仓|降低)", 1),
]

# ---------- 英文金融词典 ----------
# 含空格或连字符的短语走子串匹配；单词走边界匹配 + 否定窗口。

EN_POS = [
    "rate cut", "rate cuts", "cut rates", "beat estimates", "beat expectations",
    "better-than-expected", "better than expected", "record high", "all-time high",
    "all time high", "raise guidance", "raises guidance", "top estimates",
    "top expectations", "ceasefire", "peace deal", "trade deal",
    "surge", "surges", "surged", "rally", "rallies", "rallied",
    "soar", "soars", "soared", "jump", "jumps", "jumped",
    "gain", "gains", "gained", "upgrade", "upgrades", "upgraded",
    "outperform", "outperforms", "bullish", "upbeat", "beat", "beats",
    "breakthrough", "buyback", "buybacks", "dividend", "stimulus",
    "easing", "recovery", "dovish", "truce", "approved", "approval",
    "expansion", "boost", "boosted", "growth", "record", "strong",
    "optimistic", "profit", "profits", "winner", "winners", "positive",
]

EN_NEG = [
    "rate hike", "rate hikes", "hike rates", "below estimates",
    "below expectations", "worse-than-expected", "worse than expected",
    "cut guidance", "cuts guidance", "trade war", "bear market",
    "sell-off", "selloff", "risk-off", "risk off", "state of emergency",
    "plunge", "plunges", "plunged", "crash", "crashed", "slump", "slumped",
    "drop", "drops", "dropped", "decline", "declines", "declined",
    "downgrade", "downgrades", "downgraded", "underperform", "bearish",
    "downbeat", "miss", "misses", "missed", "layoff", "layoffs",
    "default", "defaults", "recession", "contraction", "warning", "warns",
    "warned", "crisis", "investigation", "probe", "lawsuit", "fined",
    "sanction", "sanctions", "tariff", "tariffs", "ban", "banned",
    "halt", "halts", "halted", "suspend", "suspended", "recall",
    "shortfall", "loss", "losses", "bankruptcy", "fraud", "fraudulent",
    "hawkish", "hike", "hikes", "hiked", "tightening", "escalation",
    "escalate", "conflict", "war", "invasion", "negative",
]

EN_NEGATORS = {"not", "no", "never", "without", "hardly", "barely", "n't", "nt"}


def _is_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _split_en_terms(terms):
    phrases = [t for t in terms if re.search(r"[\s-]", t)]
    words = {t for t in terms if not re.search(r"[\s-]", t)}
    return phrases, words


EN_POS_PHRASES, EN_POS_WORDS = _split_en_terms(EN_POS)
EN_NEG_PHRASES, EN_NEG_WORDS = _split_en_terms(EN_NEG)


def _negated(words, i: int) -> bool:
    for j in range(max(0, i - 3), i):
        w = words[j].rstrip(".,!?;:")
        if w in EN_NEGATORS or w.endswith("n't"):
            return True
    return False


def _occurrences(text: str, word: str) -> list:
    """返回 word 在 text 中的所有起始下标（不重叠，等价于 str.count 的语义）。"""
    idxs = []
    start = 0
    while True:
        i = text.find(word, start)
        if i < 0:
            break
        idxs.append(i)
        start = i + len(word)
    return idxs


def _negated_before(text: str, pos: int) -> bool:
    """判断 pos 前 4 字符窗口内是否出现否定词（中文否定窗口）。"""
    window = text[max(0, pos - 4):pos]
    return any(ng in window for ng in CN_NEGATORS)


BODY_DAMP = 0.25

# 弱信号词（样板/对冲用语）：120 条标注样本上，中性误报几乎全部由它们造成——
# 会议报道里的"合作/支持/签署"、央行讲话里的"风险/不确定性/压力"。
# 但不能直接删：与实词共现时它们确实在加强方向。故两层处理——
#   (1) 单个弱词按 WEAK_W 降权；
#   (2) 同极性弱词合计封顶 WEAK_CAP。
# 只靠 (1) 不够：座谈会通稿能堆出 11 个"合作/支持/赋能"，0.4×11=4.4 反而打成
# 0.81 的强利好。封顶后弱词类最多等价于一个"次阈值"推力（0.4/1.4=0.286 <
# config.yaml 的 threshold_long=0.3），必须有实词佐证才能触发交易。
WEAK_W = 0.4
WEAK_CAP = 0.4
CN_WEAK_POS = {"合作", "支持", "赋能", "落地", "领先", "信心", "坚定",
               "推出", "保障", "签署", "改善"}
CN_WEAK_NEG = {"风险", "不确定性", "压力", "紧张", "紧急", "担忧", "应急"}


def _hit_weight(matched: str) -> float:
    return WEAK_W if (matched in CN_WEAK_POS or matched in CN_WEAK_NEG) else 1.0


def _hit_pairs(hits, damp: float = 1.0) -> list:
    """把命中折算成 [(极性, 权重, 是否弱词)]，正文区域可按 damp 整体降权。"""
    out = []
    for h in hits:
        w = _hit_weight(h[3])
        out.append((h[2], w * damp, w < 1.0))
    return out


def _count_weighted(pairs) -> tuple:
    """汇总成 (正权重, 负权重)：实词全额累加，弱词同极性合计封顶 WEAK_CAP。

    封顶必须是**全局一次**的：标题区与正文区各自封顶会让通稿里的样板词
    拿到两倍额度（实测把中性强误报率从 30.8% 抬回 38.5%）。
    """
    pos = neg = wpos = wneg = 0.0
    for pol, w, weak in pairs:
        if pol > 0:
            if weak:
                wpos += w
            else:
                pos += w
        elif weak:
            wneg += w
        else:
            neg += w
    return pos + min(wpos, WEAK_CAP), neg + min(wneg, WEAK_CAP)


# 行情综述分句（2026-11-09）：东财/同花顺通稿习惯把「上证指数报3665.94点，涨0.46%；
# 深证成指涨0.27%……」整段拼在个股或排行榜快讯后面。词典会把每个「涨0.46%」记一次
# 利好——实测「中际旭创成交额A股第一」正文因此得 +0.83，而标题单独打分是 0.0，
# 这条与中际旭创的方向无关。只删纯播报分句（带指数名且带点位/涨跌幅，或两市成交额），
# 事件句（中标/业绩/公告）不受影响。
_MKT_WRAP_RE = re.compile(
    r"(上证指数|深证成指|创业板指|科创综指|沪深300|北证50|中证500|中证1000|"
    r"A股主要指数|三大指数|沪深北三市|两市|全市场)")
_MKT_MOVE_RE = re.compile(
    r"(?:涨|跌)\s*[\d.]+|报\s*[\d.]+\s*点|集体(?:上涨|下跌)|全线(?:飘红|下跌)|"
    r"[+-]?[\d.]+\s*%")
_MKT_TURNOVER_RE = re.compile(r"(?:两市|沪深北三市|全市场|A股).{0,10}成交.{0,10}(?:亿|万亿)")


def strip_market_wrap(text: str) -> str:
    """删掉纯行情综述分句（指数点位/涨跌幅/两市成交额），其余原样保留。"""
    if not text:
        return text
    out = []
    for s in re.split(r"(?<=[。；;！!])", str(text)):
        if not s.strip():
            continue
        if _MKT_WRAP_RE.search(s) and _MKT_MOVE_RE.search(s):
            continue
        if _MKT_TURNOVER_RE.search(s):
            continue
        out.append(s)
    return "".join(out)


def score_text_zh(text: str, title: str = "", body_damp: float = BODY_DAMP) -> float:
    """中文词典打分（zh-v2 命中引擎）。

    与 v1 的 (pos-neg)/(pos+neg+1) 公式、值域完全一致，改动只在"怎么数命中"：
    1. 词表 + 正则合并成一个按长度降序的 alternation，最左最长匹配一次成功，
       天然消除 v1 的重复计数（"业绩预增"曾被 业绩预增/预增 各数一次）。
    2. 跨极性重叠保留更长的那个（"下跌2.7%" 只记 下跌，不再叠加 跌+数字）。
    3. CN_FLIP_PAT 上下文反转：命中区间内的词被**强制**为目标极性
       （"上调CPI预期"=利空；强制而非取反，否则利空词会被翻成利好）。
    4. 否定窗口沿用 v1（命中点前 4 字符内出现 CN_NEGATORS 则翻极性）。
    5. 弱信号词降权（WEAK_W）：样板/对冲用语（合作、支持、风险、不确定性…）
       单独出现不足以触发交易阈值，需两个以上互相佐证。
    6. 自适应标题优先：标题本身带命中时，正文按 body_damp 降权；标题无命中时
       正文保持全权重。两类错误各自对应一档：
       - 标题有信号、正文是背景/反向数字 → 必须降权。#6"子公司选尾系统获准复产"
         正文在讲"此前因安全事故停产"，#12"净利润同比下降35.86%"正文另有 4 处
         同比增长，全文等权会把这两条打反。
       - 标题无信号、正文才是全部信息 → 不能降权。全库实测 新浪7x24 65.7%、
         金十快讯 63.6%、华尔街见闻 48.8% 的条目只有正文命中，一刀切降权会
         把这些源整体清零。
       （东方财富公告 498 条 content 全为空，标题即全文，不受本项影响。）
       不传 title 时行为与逐字打分完全一致（向后兼容所有既有调用方）。
    """
    if not text:
        return 0.0
    text = str(text)
    title = str(title or "").strip()
    if title:
        body = text[len(title):] if text.startswith(title) else text
        thits = zh_hits(title)
        pairs = _hit_pairs(thits) + _hit_pairs(zh_hits(strip_market_wrap(body)),
                                               body_damp if thits else 1.0)
    else:
        pairs = _hit_pairs(zh_hits(strip_market_wrap(text)))
    pos, neg = _count_weighted(pairs)
    if pos + neg <= 0:
        return 0.0
    return (pos - neg) / (pos + neg + 1)


_CN_POS_RE = None
_CN_NEG_RE = None
_CN_FLIP_RES = None


def _build_cn_regexes() -> None:
    """把词表与模式编译成 alternation（模式在前、字面量按长度降序 → 最左最长匹配）。"""
    global _CN_POS_RE, _CN_NEG_RE, _CN_FLIP_RES
    pos_alts = list(CN_POS_PAT) + [re.escape(w) for w in
                                   sorted(set(CN_POS), key=len, reverse=True)]
    neg_alts = list(CN_NEG_PAT) + [re.escape(w) for w in
                                   sorted(set(CN_NEG), key=len, reverse=True)]
    _CN_POS_RE = re.compile("|".join(pos_alts))
    _CN_NEG_RE = re.compile("|".join(neg_alts))
    _CN_FLIP_RES = [(re.compile(p), pol) for p, pol in CN_FLIP_PAT]


def zh_hits(text: str) -> list:
    """返回 [(start, end, polarity, matched)]，已做重叠消解/上下文反转/否定翻转。

    单独暴露便于诊断（"这条为什么打这个分"）与离线评测。
    """
    if _CN_POS_RE is None:
        _build_cn_regexes()
    t = str(text).lower()
    hits = [(m.start(), m.end(), 1, m.group()) for m in _CN_POS_RE.finditer(t)]
    hits += [(m.start(), m.end(), -1, m.group()) for m in _CN_NEG_RE.finditer(t)]
    # 跨极性重叠：保留更长的命中（同极性重叠已被 alternation 的最左最长匹配消除）
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    kept = []
    for s, e, pol, w in hits:
        if any(ks <= s and e <= ke and (ke - ks) > (e - s) for ks, ke, _, _ in kept):
            continue
        kept.append((s, e, pol, w))
    hits = kept
    # 上下文反转：命中区间内的词被**强制**为模式声明的目标极性（不是盲目取反）
    for pat, target_pol in _CN_FLIP_RES:
        for m in pat.finditer(t):
            for k, (s, e, pol, w) in enumerate(hits):
                if s >= m.start() and e <= m.end():
                    hits[k] = (s, e, target_pol, w)
    # 否定窗口
    return [(s, e, (-pol if _negated_before(t, s) else pol), w) for s, e, pol, w in hits]


def score_text_en(text: str) -> float:
    if not text:
        return 0.0
    t = str(text).lower()
    pos = neg = 0
    for ph in EN_POS_PHRASES:
        pos += t.count(ph)
    for ph in EN_NEG_PHRASES:
        neg += t.count(ph)
    words = re.findall(r"[a-z']+", t)
    for i, raw in enumerate(words):
        w = raw.rstrip(".,!?;:")
        if w in EN_POS_WORDS or w in EN_NEG_WORDS:
            flip = _negated(words, i)
            if w in EN_POS_WORDS:
                if flip:
                    neg += 1
                else:
                    pos += 1
            else:
                if flip:
                    pos += 1
                else:
                    neg += 1
    if pos + neg == 0:
        return 0.0
    return (pos - neg) / (pos + neg + 1)


def _lexicon_score(text: str, title: str = "") -> float:
    """词典打分（语言自动路由），永远可用，作为各后端的兜底。

    title 仅在中文路径生效（标题优先，见 score_text_zh）；英文 FinBERT 系
    后端不吃 title，避免在未标注的英文样本上引入未测量的行为变化。
    """
    if not text:
        return 0.0
    s = str(text)
    if _is_cjk(s):
        return score_text_zh(s, title)
    return score_text_en(s)


# ================= 后端抽象 =================

class SentimentBackend:
    """情绪打分后端基类。实现 score / score_batch。"""
    name = "base"

    def score(self, text: str, title: str = "") -> float:
        raise NotImplementedError

    def score_batch(self, texts: list, titles: list = None) -> list:
        texts = list(texts or [])
        titles = list(titles or []) + [""] * max(0, len(texts) - len(titles or []))
        return [self.score(t, ti) for t, ti in zip(texts, titles)]


class LexiconBackend(SentimentBackend):
    name = "lexicon"

    def score(self, text: str, title: str = "") -> float:
        return _lexicon_score(text, title)


class FinBertBackend(SentimentBackend):
    """FinBERT 系列后端。惰性加载，任何异常回退词典。

    混合路由：FinBERT 仅擅长英文金融文本——含 CJK 的文本自动走词典，
    避免 中文->FinBERT 的垃圾输出。
    """

    name = "finbert"

    def __init__(self, model_name: str = "ProsusAI/finbert",
                 device=None, batch_size: int = 16):
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self._pipe = None
        self._ok = False
        self._err = None

    def _ensure(self) -> bool:
        if self._ok:
            return True
        if self._err is not None:
            return False
        try:
            import torch
            from transformers import pipeline  # noqa: E402
            # Pi 等小内存设备：限制线程数，避免 OOM/换页颠簸
            try:
                torch.set_num_threads(4)
            except Exception:  # noqa: BLE001
                pass
            self._pipe = pipeline("text-classification", model=self.model_name,
                                  top_k=None, device=self.device,
                                  truncation=True, max_length=256)
            self._ok = True
        except Exception as e:  # noqa: BLE001
            self._err = e
        return self._ok

    def _map(self, out) -> float:
        pos = neg = 0.0
        for r in out or []:
            lab = str(r.get("label") or "").strip().lower()
            s = float(r.get("score") or 0.0)
            if "positive" in lab or "bullish" in lab or lab in ("pos", "up", "gain", "gains"):
                pos += s
            elif "negative" in lab or "bearish" in lab or lab in ("neg", "down", "loss", "losses"):
                neg += s
        score = pos - neg
        # 量纲对齐：FinBERT 概率差的幅度系统性高于词典公式 (p-n)/(p+n+1)
        # （实测混跑 Finnhub |sent| 0.384 vs 词典系 0.14-0.27），直接混入 impact
        # 的 intensity 分量会结构性抬高英文源。0.6 ≈ 词典强新闻上限 / FinBERT 强新闻均值。
        return max(-1.0, min(1.0, score * 0.6))

    def score(self, text: str, title: str = "") -> float:
        if not text or _is_cjk(str(text)):
            return _lexicon_score(text, title)
        if not self._ensure():
            return _lexicon_score(text, title)
        try:
            # 截断超长文本：FinBERT 上限 512 token，超长序列（实测 1596 token）
            # 会触发 O(n²) 注意力内存膨胀 -> Pi 上被内核 OOM kill
            return self._map(self._pipe(str(text)[:1500])[0])
        except Exception:  # noqa: BLE001
            return _lexicon_score(text, title)

    def score_batch(self, texts: list, titles: list = None) -> list:
        if not texts:
            return []
        texts = list(texts)
        titles = list(titles or []) + [""] * max(0, len(texts) - len(titles or []))
        # 混合路由：中文走词典，英文走 FinBERT 批量
        out, en_idx, en_texts = [0.0] * len(texts), [], []
        for i, t in enumerate(texts):
            if t and _is_cjk(str(t)):
                out[i] = _lexicon_score(t, titles[i])
            else:
                en_idx.append(i)
                en_texts.append(str(t))
        if en_texts and self._ensure():
            try:
                import time as _t
                en_texts = [x[:1500] for x in en_texts]   # 截断超长，防 OOM
                t0 = _t.time()
                preds = []
                B = self.batch_size
                for i in range(0, len(en_texts), B):
                    chunk = en_texts[i:i + B]
                    preds.extend(self._pipe(chunk, batch_size=B))
                    done = min(i + B, len(en_texts))
                    if done % 200 < B or done == len(en_texts):
                        rate = done / max(_t.time() - t0, 1e-6)
                        eta = (len(en_texts) - done) / max(rate, 1e-6)
                        print(f"[finbert] 打分 {done}/{len(en_texts)} "
                              f"({rate:.1f} 条/秒，剩余约 {eta:.0f} 秒)", flush=True)
                for i, pred in zip(en_idx, preds):
                    out[i] = self._map(pred)
            except Exception:  # noqa: BLE001
                for i in en_idx:
                    out[i] = _lexicon_score(texts[i], titles[i])
        elif en_texts:
            for i in en_idx:
                out[i] = _lexicon_score(texts[i], titles[i])
        return out


class LLMBackend(SentimentBackend):
    """OpenAI 兼容 chat/completions 后端，结构化 JSON 输出 sentiment。"""
    name = "llm"

    def __init__(self, api_base: str = "", api_key: str = "",
                 model: str = "", timeout: int = 20):
        self.api_base = (api_base or os.environ.get("OPENAI_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.timeout = timeout

    def score(self, text: str, title: str = "") -> float:
        if not self.api_key:
            return _lexicon_score(text, title)
        try:
            import requests  # noqa: E402
            prompt = (
                "你是金融新闻情绪分析师。请对下面的新闻打分，只返回 JSON："
                '{"sentiment": 在[-1,1]之间的情绪分, "reason": "一句话理由"}\n\n'
                f"新闻：{text}"
            )
            r = requests.post(
                f"{self.api_base}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=self.timeout,
            )
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", content, re.S)
            obj = json.loads(m.group(0)) if m else {}
            val = float(obj.get("sentiment", 0.0))
            return max(-1.0, min(1.0, val))
        except Exception:  # noqa: BLE001
            return _lexicon_score(text)


# ================= 动态路由 =================

_active_backend: SentimentBackend = LexiconBackend()


def set_backend(backend: SentimentBackend) -> None:
    global _active_backend
    _active_backend = backend


def get_backend() -> SentimentBackend:
    return _active_backend


def score_text(text: str, title: str = "") -> float:
    """统一入口：路由到当前后端（默认词典）。

    title 可选：传入后中文路径启用标题优先打分（正文命中降权），
    英文/FinBERT 路径忽略它。不传时与旧行为逐字一致。
    """
    if not text:
        return 0.0
    return _active_backend.score(str(text), str(title or ""))


def score_batch(texts: list, titles: list = None) -> list:
    return _active_backend.score_batch([str(t) for t in (texts or [])],
                                       [str(t or "") for t in (titles or [])])


def configure_backend(cfg: dict) -> SentimentBackend:
    """按 config['sentiment'] 构建并激活后端。失败不抛异常，回退词典。"""
    global _active_backend
    s = (cfg.get("sentiment") or {}) if isinstance(cfg, dict) else {}
    name = str(s.get("backend") or "lexicon").strip().lower()

    if name == "finbert":
        _active_backend = FinBertBackend(
            model_name=s.get("finbert_model") or "ProsusAI/finbert",
            device=s.get("finbert_device") or None,
        )
        print(f"[sentiment] 后端=finbert（{_active_backend.model_name}，首次评分时加载，失败回退词典）")
    elif name == "llm":
        _active_backend = LLMBackend(
            api_base=s.get("llm_api_base") or "",
            api_key=s.get("llm_api_key") or "",
            model=s.get("llm_model") or "",
        )
        print(f"[sentiment] 后端=llm（{_active_backend.model}，失败回退词典）")
    else:
        _active_backend = LexiconBackend()
        print("[sentiment] 后端=lexicon（离线词典）")

    return _active_backend
