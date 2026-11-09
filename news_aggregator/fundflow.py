"""A 股主力资金净流入快照（东财，akshare）：一轮取一次，供摘要/卡片标注资金面。

为什么用榜单接口而不是逐股接口：`stock_individual_fund_flow` 每股一次请求，10 条
告警就是 10 次且容易被限频；`stock_individual_fund_flow_rank` 一次拿全市场（54 页
~20s），本轮所有代码一次查完。20s 换来「涨着但主力在出货」这条判据，只在真有
A 股条目时付。

失败一律静默降级（返回空字典）：资金面是增量信息，绝不能因为东财抽风把告警卡住。
"""
import io
import re
import socket
from contextlib import redirect_stderr

_CODE_RE = re.compile(r"\d{6}")


def snapshot(codes, timeout: float = 30.0) -> dict:
    """{6 位代码: {"net": 主力净额(元), "pct": 净占比%, "chg": 涨跌幅%, "price": 最新价}}"""
    want = {str(c) for c in (codes or []) if _CODE_RE.fullmatch(str(c or ""))}
    if not want:
        return {}
    try:
        import akshare as ak
    except Exception:  # noqa: BLE001 — 没装 akshare 就当作没这个能力
        return {}
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(float(timeout))
    try:
        with redirect_stderr(io.StringIO()):      # akshare 的分页进度条写到 stderr
            df = ak.stock_individual_fund_flow_rank(indicator="今日")
        out = {}
        for rec in df.to_dict("records"):
            code = str(rec.get("代码") or "").zfill(6)
            if code not in want:
                continue
            out[code] = {
                "net": float(rec.get("今日主力净流入-净额") or 0.0),
                "pct": float(rec.get("今日主力净流入-净占比") or 0.0),
                "chg": float(rec.get("今日涨跌幅") or 0.0),
                "price": float(rec.get("最新价") or 0.0),
            }
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[fundflow] 跳过（{type(e).__name__}: {e}）")
        return {}
    finally:
        socket.setdefaulttimeout(old)


def fmt_net(net: float) -> str:
    """元 → +6.7亿 / -1.2亿 / +3500万（正负号保留：净流出本身是信号）。"""
    sign = "+" if net >= 0 else "-"
    v = abs(float(net or 0.0))
    if v >= 1e8:
        return f"{sign}{v / 1e8:.1f}亿"
    if v >= 1e4:
        return f"{sign}{v / 1e4:.0f}万"
    return f"{sign}{v:.0f}元"


def fmt_tag(f: dict) -> str:
    """摘要/卡片用的一行：主力+6.7亿(涨+0.9%)。没有数据返回空串。"""
    if not f:
        return ""
    return f"主力{fmt_net(f.get('net') or 0.0)}(涨{f.get('chg') or 0.0:+.1f}%)"
