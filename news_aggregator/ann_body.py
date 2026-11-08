"""东财公告正文拉取与「值不值得拉」的判定。

背景（实测，2026-11）
--------------------
列表接口 np-anotice-stock 只给标题，content 恒为空；正文要另调
np-cnotice-stock/api/content/ann?art_code=... 。138 条抽样结论：

- 正文不是样板：法定免责声明占全文 >10% 的 0/138，中位正文 2,836 字；
- 数字密度 6.97%，正文里确实带业绩/金额数字；
- 接口单篇最多给 5,000 字（49/138 触顶），再长要下 PDF 附件；
- 59 种 column_name 里有 24 种一个信号词都没有（法律意见书、议事规则…），
  逐条拉是纯浪费——按列名 + 标题关键词筛掉。

因此本模块提供两个东西：
1. `wants_body(column_name, title)`：只对高信号公告返回 True；
2. `fetch_body(art_code)`：拉正文并去 HTML 标签。

注意：`高管人员持股变动` 这类是表格式公告，正文里信息在附件表格中，
文本信号弱；不能因为 wants_body=False 就认为「没有信息」，只是本流水线
不做表格抽取。
"""

import html
import json
import re
import urllib.parse
import urllib.request

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}
BODY_API = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
MAX_BODY_CHARS = 5000  # 接口上限

# 列名允许：这些类型正文有实质内容（回购/增减持/业绩/分配/股本/重大事项）
_ALLOW_RE = re.compile(
    r"回购|增持|减持|股权激励|业绩|年报|季报|半年报|分配|权益分派|送转|"
    r"股本变动|重大合同|中标|收购|重组|出售资产|对外投资|投资设立|增资|转让|"
    r"诉讼|仲裁|处罚|监管|问询|关注函|"
    r"停牌|复牌|退市|风险提示|质押|担保|关联交易|减值|商誉|立案|违规|"
    r"可转债|增发|配股|定增"
)
# 列名拒绝：纯法律/程序性文书，正文是模板（即使标题里有「回购」字样也不拉）
_DENY_RE = re.compile(
    r"法律意见书|保荐|核查意见|管理办法|议事规则|实施细则|公司章程|"
    r"独立董事|监事会决议|董事会决议|股东大会决议|股东大会通知|"
    r"审计报告|内部控制|会计师|持续督导|专项说明|调研|投资者关系|"
    r"募集资金"
)
# 标题信号：列名为「其他」时靠标题兜底
_TITLE_RE = re.compile(
    r"回购|增持|减持|业绩|预告|快报|扭亏|预增|预减|预警|分红|派息|权益分派|送转|"
    r"中标|合同|收购|重组|出售|对外投资|投资设立|增资|转让|立案|处罚|警示|问询|关注函|"
    r"停牌|复牌|退市|诉讼|仲裁|质押|担保|激励|解禁|增发|配股|可转债|定增|违规|"
    r"减值|计提|商誉|亏损|盈利|举牌|要约|控制权|实控人"
)
_WS_RE = re.compile(r"[ \t\r\f\v\u3000]+")
_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NL = re.compile(r"\n{3,}")


def wants_body(column_name: str, title: str) -> bool:
    """按公告分类 + 标题判断是否值得拉正文（省掉 ~24/59 的纯模板类）。"""
    col = str(column_name or "")
    ttl = str(title or "")
    if col and _DENY_RE.search(col):
        return False
    if col and _ALLOW_RE.search(col):
        return True
    return bool(_TITLE_RE.search(ttl))


def _html_to_text(s: str) -> str:
    s = _TAG_RE.sub("\n", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s)
    s = "\n".join(ln.strip() for ln in s.split("\n"))
    return _MULTI_NL.sub("\n\n", s).strip()


def fetch_body(art_code: str, timeout: int = 25) -> str:
    """拉单篇公告正文（纯文本，最多 5,000 字）；失败返回空串，不抛异常。"""
    art_code = str(art_code or "").strip()
    if not art_code:
        return ""
    qs = urllib.parse.urlencode({"art_code": art_code, "client_source": "web",
                                 "page_index": "1"})
    try:
        req = urllib.request.Request(BODY_API + "?" + qs, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            obj = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return ""
    data = obj.get("data") or {}
    txt = str(data.get("notice_content") or "")
    if not txt:
        return ""
    return _html_to_text(txt)[:MAX_BODY_CHARS]


_ART_RE = re.compile(r"/detail/\d+/([A-Za-z0-9_]+)\.html")


def art_code_of(item: dict) -> str:
    """从 item 取 art_code：优先字段，旧数据回落到 detail URL。"""
    art = str(item.get("art_code") or "").strip()
    if art:
        return art
    m = _ART_RE.search(str(item.get("detail") or item.get("url") or ""))
    return m.group(1) if m else ""
