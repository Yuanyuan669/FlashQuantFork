"""告警筛选层：注意力预算 + 同题去重（轮内/跨轮）+ 每主题配额。

背景（2026-11-08 实测 news/alerts.jsonl：104 轮 / 17541 条告警 / 25 个交易日）：
- 均值 168.7 条告警/轮，早间积压轮 max 451 条；旧摘要按 3800B 切块后共推送
  771 条企微消息（7.4 条/轮、约 31 条/交易日）→ 用户反馈刷屏。
- impact 中位 0.578，且 100% 紧贴 impact_min=0.45（0.45–0.50 区间占 9.8%）：
  阈值本身已无区分度，真正缺的是「按影响分取头部」的注意力预算。
- AI/算力 + 财报/公告 占主题命中 65%，Finnhub + 东财全球资讯 占来源 56%；
  同一事件被多源转述，完全同题重复占 14%（同一轮内就有 846 条）。

历史回放（104 轮全量，参数 = config 默认值：impact_min 0.50 / 预算 15 / 配额 3 /
冷却 12h / Jaccard 0.80 + 子串包含 + 数字护栏）：告警 17541→1472 条、企微消息
771→104 条（每轮恰好 1 条，单条最大 2871 字节），抑制构成 = 阈值 1718 /
轮内同题 967 / 跨轮冷却 26 / 主题配额 8412 / 注意力预算 4946；被预算挤掉的
impact≥p90(0.66) 高影响条目 4 条，选中告警的 impact 中位数由全量的 0.578 升到 0.659。

Jaccard 阈值取 0.80 的依据：回放 0.70/0.75/0.80/0.85 四档，选出条数与消息数完全相同
（0.75 与 0.80 均为 1472 条 / 104 条消息，只差 20 条轮内同题的归属），
而 0.74 附近存在一类模板化英文专栏标题
（"What Is Going On With Meta Stock on Tuesday?" vs "…Nvidia Stock…"，只差实体名），
取 0.80 留出余量，避免把不同标的的同栏位稿误并成一条。

误合并防护（三道，括号内为 2026-11-08 实测值）：
1. 数字护栏——长财报标题只差一位数字时二元组 Jaccard 也能到 0.882
   （「…净利润同比增长15.2%…」vs「…16.2%…」），护栏是这种情况下的唯一保护，
   故默认要求两边数字串互为子集才可能判同题；短模板标题
   （「万科A000002龙虎榜数据09-29」vs「…09-30」，Jaccard 0.750）本就落在
   0.80 阈值下方，护栏只是第二道保险；
2. 字面子串——「标题+导语」型转述只认连续子串，不认二元组重叠系数
   （归一化去空格后英文二元组类型饱和：两条毫不相干的长英文 Jaccard 仅 0.206，
   重叠系数却有 0.412，审计中最高的一对到 0.92；误并方向是漏报，比多推一条更糟）；
   仅靠子串命中的 41 对已逐条人工核对，全部是真重复（一家源在标题后多带了几个小句）。
   已知取舍：「旧标题 + 新增半句」的转述在冷却窗内会被吞掉（子串包含所致），
   这是有意的降噪——落选条目照写 alerts.jsonl（pushed=false, reason=同题-冷却），
   由 scripts/alert_review.py 用次日 |收益| 校验是否真丢了 alpha；
3. 长度门槛——子串判定只对 ≥12 字的短标题生效，避免「关于回购公司股份的公告」
   （归一化后 11 字）这类短模板标题被任意长标题吞掉。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta

# 标点/空白归一化（中英混合标题都要吃掉；'-' 会被并入数字，见 numbers_of）
_PUNC_RE = re.compile(r"[\s【】\[\]（）()：:，,。.、!！?？\"'“”‘’\-—…·/|,;；]")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")

REASON_DUP_ROUND = "同题-轮内"
REASON_DUP_COOLDOWN = "同题-冷却"
REASON_THEME_QUOTA = "主题配额"
REASON_BUDGET = "注意力预算"


def normalize_title(title: str) -> str:
    """去标点空白 + 小写，得到可比较的标题骨架。"""
    return _PUNC_RE.sub("", title or "").lower()


def numbers_of(norm_title: str) -> tuple:
    """标题里的数字串（日期/涨跌幅/代码/金额），用于阻止模板化标题被误并。"""
    return tuple(_NUM_RE.findall(norm_title))


def bigrams(norm_title: str) -> frozenset:
    if len(norm_title) < 2:
        return frozenset({norm_title}) if norm_title else frozenset()
    return frozenset(norm_title[i:i + 2] for i in range(len(norm_title) - 1))


def fingerprint(title: str) -> dict:
    """标题指纹：{"nt", "bg", "nums"}。"""
    nt = normalize_title(title)
    return {"nt": nt, "bg": bigrams(nt), "nums": numbers_of(nt)}


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap_coef(a: frozenset, b: frozenset) -> float:
    """重叠系数 |A∩B| / min(|A|,|B|)。

    ⚠ 只用于离线审计，不要拿它做同题判定：归一化会去掉空格，英文标题的字符二元组
    *类型* 集合很快饱和，实测两条毫不相干的长英文也能到 0.85（见 is_same_event 注释）。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def is_same_event(fp_a: dict, fp_b: dict, jac_threshold: float = 0.80,
                  require_same_numbers: bool = True,
                  substring_containment: bool = True, min_contain_chars: int = 12) -> bool:
    """两条标题是否指同一事件。

    判定顺序：
    1. 归一化后完全相同 → 是；
    2. 数字护栏（require_same_numbers）：两边数字串互不包含 → 否；
    3. 二元组 Jaccard ≥ jac_threshold → 是（等长标题的改写/换标点）；
    4. 子串包含（substring_containment）：短标题是长标题的连续子串且长度 ≥
       min_contain_chars → 是。用于一家源把「标题+导语」塞进 title 的情况
       （实测样例：金十「【商务部…反倾销立案调查】金十数据11月6日讯，自本公告…」
       vs 华尔街见闻「商务部…反倾销立案调查」，Jaccard 只有 0.50）。

    第 4 条刻意用「连续子串」而不是二元组重叠系数：英文标题归一化后会去掉空格，
    字符二元组 *类型* 集合很快饱和（常见字母对就那几十个），实测两条毫不相干的
    长英文（Finnhub 行情稿 vs Truth Social 帖）重叠系数也能到 0.85，
    会把真告警误并掉——这是危险方向（漏报），所以只认字面包含。

    数字护栏用「子集」而非「相等」：通讯社前缀常带日期（「财联社10月8日电，…」），
    要求完全相等会把真重复漏掉；而「万科A龙虎榜数据09-29」vs「…09-30」两边数字
    互不包含，仍会被正确判为不同事件。
    """
    na, nb = fp_a.get("nt") or "", fp_b.get("nt") or ""
    if not na or not nb:
        return False
    if na == nb:
        return True
    if require_same_numbers:
        sa = set(fp_a.get("nums") or ())
        sb = set(fp_b.get("nums") or ())
        if not (sa <= sb or sb <= sa):
            return False
    if jaccard(fp_a.get("bg") or frozenset(),
               fp_b.get("bg") or frozenset()) >= jac_threshold:
        return True
    if substring_containment:
        short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
        if len(short) >= min_contain_chars and short in long_:
            return True
    return False


class Cooldown:
    """跨轮同题冷却：只记「实际推送过」的标题指纹，窗口内近似转述不再推。

    状态落盘 news/alert_state.json（与 seen.json 同目录，随 cron 提交入库，便于回看）。
    """

    def __init__(self, hours: float = 12.0, jac_threshold: float = 0.80,
                 require_same_numbers: bool = True, enabled: bool = True,
                 substring_containment: bool = True, min_contain_chars: int = 12):
        self.hours = float(hours)
        self.jac_threshold = float(jac_threshold)
        self.require_same_numbers = bool(require_same_numbers)
        self.substring_containment = bool(substring_containment)
        self.min_contain_chars = int(min_contain_chars)
        self.enabled = bool(enabled) and self.hours > 0
        self._entries: list = []      # [{"nt","nums","ts"}]
        self._fps: list = []          # 与 _entries 对齐的指纹（bg 惰性算好）
        self.path: str | None = None

    # ---- 持久化 ----
    def load(self, path) -> "Cooldown":
        self.path = str(path)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return self
        for e in (data.get("pushed") or []):
            nt = e.get("nt") or ""
            if not nt:
                continue
            self._entries.append({"nt": nt, "nums": tuple(e.get("nums") or ()),
                                  "ts": e.get("ts") or ""})
            self._fps.append({"nt": nt, "bg": bigrams(nt),
                              "nums": tuple(e.get("nums") or ())})
        return self

    def save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "hours": self.hours,
                           "pushed": self._entries}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def prune(self, now: datetime | None = None) -> int:
        """丢弃超窗条目，返回剩余条数。"""
        if not self.enabled or not self._entries:
            return len(self._entries)
        now = now or datetime.now()
        cutoff = now - timedelta(hours=self.hours)
        keep_e, keep_f = [], []
        for e, fp in zip(self._entries, self._fps):
            try:
                ts = datetime.fromisoformat(e["ts"])
            except (KeyError, ValueError):
                continue
            if ts >= cutoff:
                keep_e.append(e)
                keep_f.append(fp)
        self._entries, self._fps = keep_e, keep_f
        return len(self._entries)

    def hit(self, fp: dict) -> bool:
        """该指纹是否与冷却窗口内已推送事件重复。"""
        if not self.enabled:
            return False
        return any(is_same_event(fp, other, self.jac_threshold,
                                 self.require_same_numbers, self.substring_containment,
                                 self.min_contain_chars) for other in self._fps)

    def remember(self, title: str = "", ts: datetime | None = None,
                 fp: dict | None = None) -> None:
        """记住一条已推送事件的标题指纹（fp 已知时直接复用，省一次计算）。"""
        if not self.enabled:
            return
        fp = fp or fingerprint(title)
        if not fp.get("nt"):
            return
        self._entries.append({"nt": fp["nt"], "nums": list(fp["nums"]),
                              "ts": (ts or datetime.now()).isoformat(timespec="seconds")})
        self._fps.append(fp)

    def __len__(self) -> int:
        return len(self._entries)


def select_alerts(cands: list, *, max_alerts: int = 15, max_per_theme: int = 3,
                  cooldown: Cooldown | None = None, jac_threshold: float = 0.80,
                  require_same_numbers: bool = True, substring_containment: bool = True,
                  min_contain_chars: int = 12) -> tuple[list, list]:
    """按「同题去重 → 每主题配额 → 注意力预算」筛出本轮实际推送的告警。

    cands 元素需含：
      item     原始新闻 dict（用于取 title/symbols 等）
      impact   影响分（float）
      score    情绪分（float，带符号）
      themes   命中主题名列表（第一个作为配额归属）
      fp       标题指纹（fingerprint() 的结果，缺省时按 item.title 现算）

    返回 (picked, suppressed)；suppressed 元素为 (cand, reason)。
    picked 按影响分降序（与推送展示顺序一致）。
    每个 picked 候选会被写入 n_same（int，默认 1）：轮内被它吸收的同题转述数 +1，
    即「这条事件有几家源在报」。多源互证是事件重要性的客观信号，摘要里显示为 ×N源。
    """
    ordered = sorted(cands, key=lambda c: (-float(c.get("impact") or 0.0),
                                           -abs(float(c.get("score") or 0.0))))
    picked, suppressed, reps = [], [], []    # reps: [(fp, 代表候选)]
    theme_used: dict = {}
    for c in ordered:
        fp = c.get("fp") or fingerprint((c.get("item") or {}).get("title") or "")
        # 1) 轮内同题：同一事件多源转述，只留影响分最高的那条，
        #    并把被吸收的转述数记在代表候选上（n_same）
        owner = None
        for r_fp, r_cand in reps:
            if is_same_event(fp, r_fp, jac_threshold, require_same_numbers,
                             substring_containment, min_contain_chars):
                owner = r_cand
                break
        if owner is not None:
            owner["n_same"] = int(owner.get("n_same") or 1) + 1
            suppressed.append((c, REASON_DUP_ROUND))
            continue
        # 2) 跨轮冷却：窗口内已推送过的同题不再打扰
        if cooldown is not None and cooldown.hit(fp):
            suppressed.append((c, REASON_DUP_COOLDOWN))
            continue
        reps.append((fp, c))
        c.setdefault("n_same", 1)
        # 3) 每主题配额：防止 AI/算力 + 财报/公告 吃掉全部预算
        first = (c.get("themes") or [""])[0]
        if max_per_theme > 0 and theme_used.get(first, 0) >= max_per_theme:
            suppressed.append((c, REASON_THEME_QUOTA))
            continue
        # 4) 注意力预算：每轮最多 max_alerts 条
        if max_alerts > 0 and len(picked) >= max_alerts:
            suppressed.append((c, REASON_BUDGET))
            continue
        picked.append(c)
        theme_used[first] = theme_used.get(first, 0) + 1
    return picked, suppressed


def summarize_suppressed(suppressed: list) -> dict:
    """{reason: count}，用于推送抬头与 stdout 摘要。"""
    out: dict = {}
    for _c, reason in suppressed:
        out[reason] = out.get(reason, 0) + 1
    return out
