"""实时事件监测器：秒级轮询快讯源 -> 事件词匹配 -> 影响分排序 -> 板块/个股映射 -> 多通道告警。

用法（在项目根目录执行）：
    .venv/Scripts/python.exe news_aggregator/monitor.py               # 常驻轮询
    .venv/Scripts/python.exe news_aggregator/monitor.py --once        # 只跑一轮即退（测试/定时）
    .venv/Scripts/python.exe news_aggregator/monitor.py --dry-run     # 只检测不推送
    .venv/Scripts/python.exe news_aggregator/monitor.py --interval 10  # 自定义轮询秒数
    .venv/Scripts/python.exe news_aggregator/monitor.py --no-boards    # 跳过板块缓存（更快/离线）

说明：
- 复用 news_aggregator/fetchers.py 的多源快讯、sentiment.py 情绪打分、
  impact.py 影响分排序、push.py 推送。
- seen.json 持久化去重：重启不重复告警；首次运行只建立基线、不告警历史旧闻。
- 按影响分降序告警，低于 monitor.impact_min 的主题告警会被过滤。
- 注意力预算（alert_filter.py）：过阈值的候选再经「同题去重 → 每主题配额 →
  每轮 top-N」筛选，只有选中的进摘要推送；落选者也写入 alerts.jsonl（pushed=false）
  供 dashboard / alert_review 回看，验证预算是否漏掉 alpha。
- 只告警，不自动下单。
"""

import argparse
import json
import os
import pathlib
import sys
import time
from datetime import datetime

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news_aggregator.fetchers import SOURCES, filter_recent, apply_primary_keys, load_env_file, load_news_universe, fetch_symbol_news, fetch_us_symbol_news  # noqa: E402
from news_aggregator.sentiment import score_text, configure_backend  # noqa: E402
from news_aggregator.push import push_alert  # noqa: E402
from news_aggregator.run import compute_daily, dedupe, load_history, save_history, upsert_history, append_items_by_date  # noqa: E402
from news_aggregator.boards import get_board_cache  # noqa: E402
from news_aggregator.impact import keyword_match, match_themes, compute_impact  # noqa: E402
from news_aggregator.stock_impact import map_news_to_stocks  # noqa: E402
from news_aggregator.entity_link import link_items, stats_line  # noqa: E402
from news_aggregator.event_filter import classify as classify_event  # noqa: E402
from news_aggregator.event_filter import volatility_ref  # noqa: E402
from news_aggregator.alert_filter import (  # noqa: E402
    REASON_BUDGET, REASON_DUP_COOLDOWN, REASON_DUP_ROUND, REASON_THEME_QUOTA,
    Cooldown, fingerprint, select_alerts, summarize_suppressed)

# Windows 控制台编码兼容（避免 emoji 等字符导致 print 崩溃）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

NEWS_DIR = ROOT / "news"
SEEN_PATH = NEWS_DIR / "seen.json"
ALERTS_PATH = NEWS_DIR / "alerts.jsonl"
STATE_PATH = NEWS_DIR / "alert_state.json"   # 跨轮同题冷却状态（已推送标题指纹）
SEEN_MAX = 20000  # 只保留最近 N 条 id，防无限增长


def log_alerts(rows: list) -> None:
    """把本轮告警流水追加到 news/alerts.jsonl（供 dashboard / alert_review.py 回看）。

    一次 open 写完整轮，避免逐条打开文件；rows 里既含实际推送的（pushed=true），
    也含被注意力预算/同题去重/主题配额筛掉的候选（pushed=false + reason），
    这样才能事后验证「少推的那些是否真的没有 alpha」。
    """
    if not rows:
        return
    NEWS_DIR.mkdir(exist_ok=True)
    with open(ALERTS_PATH, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_config() -> dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def alert_row(it: dict, round_id: str, theme: str, event_type: str, impact: float,
              score: float, pushed: bool, reason: str = "", kind: str = "event") -> dict:
    """alerts.jsonl 的一行。

    2026-11-08 起新增 round/kind/pushed/reason 四个字段：pushed=false 的行是
    「过了阈值但被同题去重/主题配额/注意力预算筛掉」的候选，留着做闭环验证
    （scripts/alert_review.py 对比两组次日波动，确认少推的那批确实没 alpha）。
    旧行没有 pushed 字段，消费方一律按 pushed=true 处理。
    """
    return {
        "news_id": it.get("id"),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "round": round_id,
        "date": it.get("date") or "",
        "theme": theme,
        "event_type": event_type,
        "impact": float(impact or 0.0),
        "score": float(score or 0.0),
        "source": it.get("source") or "",
        "symbols": it.get("symbols") or [],
        "kind": kind,
        "pushed": bool(pushed),
        "reason": reason or "",
    }


def load_themes(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return (data or {}).get("themes") or []


def load_seen() -> list:
    """返回按加入顺序的 id 列表（seen.json 为 JSON 数组，天然有序）。

    返回 list 而非 set：裁剪时按「先到先淘汰」保留最近 SEEN_MAX 条。
    """
    if SEEN_PATH.exists():
        try:
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_seen(seen: list) -> None:
    NEWS_DIR.mkdir(exist_ok=True)
    # 按加入顺序保留最近 SEEN_MAX 条（旧 set 实现随机丢弃，可能丢新留旧）
    tmp = SEEN_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen[-SEEN_MAX:], f, ensure_ascii=False)
    tmp.replace(SEEN_PATH)  # 原子替换：写入中断不会损坏 seen.json


def fetch_new_items(enabled_sources: list | None, max_workers: int = 4,
                    timeout: float = 90.0) -> list:
    """并发抓取全部源；单个源失败/超时不中断。整体软超时后使用已返回的结果。

    旧实现串行抓 40+ 源（Google News RSS 23 次 + akshare 快讯），一轮可长达数分钟；
    并发后一轮时间 ≈ 最慢几个源的时间。超时的源在其线程内自然结束（requests 有超时）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    jobs = [(name, fn) for name, fn in SOURCES
            if not enabled_sources or name in enabled_sources]

    def _safe(name, fn):
        try:
            return name, fn() or []
        except Exception as e:  # noqa: BLE001
            return name, None

    items = []
    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futs = {ex.submit(_safe, n, f): n for n, f in jobs}
        for fut in as_completed(futs, timeout=timeout):
            name, got = fut.result()
            if got is None:
                print(f"[monitor] {name}: 失败")
            else:
                items.extend(got)
    except TimeoutError:
        print(f"[monitor] 抓取超过 {timeout:.0f}s，使用已返回的 {len(items)} 条结果继续")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return items


def append_raw(items: list) -> None:
    """按日期追加写原始新闻（复用 run.append_items_by_date，保证两处归档规则一致）。"""
    append_items_by_date(items, NEWS_DIR / "raw")


def update_history(items: list) -> None:
    """把新条目并入历史情绪库。

    注意：不能用「本轮增量」的均值直接覆盖当日值——每轮增量只是全天的一部分，
    会让历史情绪被最后一批增量稀释失真。正确做法：先归档（调用方已 append_raw），
    再从 raw 文件重算受影响日期的全部新闻。
    """
    if not items:
        return
    days = sorted({str(it.get("date") or "").replace("-", "") for it in items})
    days = [d for d in days if d and d != "unknown"]
    if not days:
        return
    h = load_history()
    raw_dir = NEWS_DIR / "raw"
    for day in days:
        path = raw_dir / f"{day}.jsonl"
        if not path.exists():
            continue
        day_items = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                day_items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not day_items:
            continue
        market, sym_out = compute_daily(dedupe(day_items))
        h = upsert_history(h, market, sym_out)
    save_history(h)


def fmt_boards(themes_list: list, hits, boards, theme_boards, top_n: int) -> list:
    """组装板块展示行（合并告警时遍历多个主题）。"""
    lines = []
    for th in themes_list:
        for bname in (theme_boards or {}).get(th["name"], []):
            info = (boards or {}).get(bname)
            cons = (info or {}).get("constituents") or []
            if cons:
                detail = "、".join(f"{c['name']} {c['pct']:+.1f}%" for c in cons[:top_n])
            else:
                detail = "(成分股未获取)"
            kind = (info or {}).get("kind") or "concept"
            kind_cn = "概念" if kind == "concept" else "行业"
            lines.append(f"- {bname}({kind_cn})：{detail}")
    return lines


def _event_note(ev) -> str:
    """详细告警里的事件类型行：类型(方向，是否有 alpha)。"""
    if not ev:
        return ""
    typ, direction, whitelisted = ev
    return f"{typ}({direction}，{'有alpha' if whitelisted else '无alpha'})"


def _event_short(ev) -> str:
    """摘要里的事件类型（去掉 alpha 标注，省字节）。"""
    if not ev:
        return ""
    typ, direction, _wl = ev
    return f"{typ}({direction})"


DIR_SCORE_EPS = 0.15      # 与 config.yaml 的 monitor.score_min 同量级：低于它不算有方向


def _event_dir(ev, score: float = 0.0) -> str:
    """多空方向（利好/利空/中性），用于摘要抬头的多空统计。

    优先用事件分类给出的方向；没匹配到事件类型时回落到情绪分符号。
    2026-11-09 dry-run 实测：15 条推送里 11 条没有事件类型，全记「中性」会让
    抬头显示「利好4/中性11」，而实际 13 条情绪为正——统计失真，回落后才有信息量。
    """
    if ev and len(ev) > 1 and ev[1]:
        return str(ev[1])
    s = float(score or 0.0)
    if s >= DIR_SCORE_EPS:
        return "利好"
    if s <= -DIR_SCORE_EPS:
        return "利空"
    return "中性"


def build_alert(item: dict, score: float, theme: dict, hits: list,
                boards: dict, theme_boards: dict, top_n: int,
                stock_map: list | None = None, event_note: str = "",
                vol_ref: dict | None = None, themes_list: list | None = None) -> tuple[str, str]:
    title = f"[事件告警] {theme['name']}"
    src = item.get("source") or ""
    ts = item.get("ts") or item.get("date") or ""
    text = (item.get("title") or item.get("content") or "").strip()
    if len(text) > 160:
        text = text[:160] + "…"

    lines = []
    lines.append(f"**主题**：{theme['name']}")
    lines.append(f"**命中关键词**：{' / '.join(hits)}")
    lines.append(f"**影响分**：{item.get('impact', 0.0):.3f}")
    lines.append(f"**情绪分**：{score:+.3f}")
    if event_note:
        lines.append(f"**事件类型**：{event_note}")
    if vol_ref:
        lines.append(
            f"**历史波动参考**：同类「{vol_ref['typ']}」事件次日平均波动 "
            f"{vol_ref['mean_abs']*100:.1f}%，次日|波动|>3% 的概率 {vol_ref['big_rate']*100:.0f}%"
            f"（全样本校准，仅衡量波动大小、不预测方向）")
    lines.append(f"**来源**：{src}　**时间**：{ts}")
    lines.append(f"**原文**：{text}")
    if theme.get("macro"):
        lines.append("**类型**：宏观事件（关注利率/避险相关板块）")
    if stock_map:
        lines.append("**个股影响（LLM 判断）**：")
        for r in stock_map:
            arrow = {"利好": "↑", "利空": "↓", "中性": "→"}.get(r.get("direction"), "")
            lines.append(
                f"- {r.get('name')}({r.get('code')}) {r.get('direction')}{arrow}"
                f"（强度{r.get('magnitude')}）：{r.get('reason')}")
    bl = fmt_boards(themes_list or [theme], hits, boards, theme_boards, top_n)
    if bl:
        lines.append("**相关板块**：")
        lines.extend(bl)
    return title, "\n".join(lines)


def _politician_allowed(pol: str, cfg: dict) -> bool:
    watch = (cfg.get("politicians") or [])
    if not watch:
        return True
    pol = (pol or "").lower()
    return any(str(w).lower() in pol for w in watch)


def build_ticker_alert(item: dict) -> tuple[str, str]:
    kind_label = "国会交易" if item.get("kind") == "congress_trade" else "内部人交易"
    pol = item.get("politician") or "未知"
    ticker = item.get("ticker") or "-"
    title = f"[{kind_label}] {pol} {ticker}"
    lines = [
        f"**类型**：{kind_label}",
        f"**人物**：{pol}",
        f"**标的**：{ticker}",
        f"**内容**：{item.get('title') or ''}",
        f"**日期**：{item.get('date') or ''}",
        f"**来源**：{item.get('source') or ''}",
    ]
    return title, "\n".join(lines)


def digest_line(title: str, impact: float, score: float, ev_short: str = "",
                source: str = "", n_same: int = 1, title_chars: int = 48) -> str:
    """主题分组内的一行（约 110 字节）：· 标题｜影响 · 情绪 · 事件 · 来源 [· ×N源]。

    2026-11-09 二版：用户反馈「一条一条抓不到重点」。改为按主题分组 + 抬头给
    主线/多空统计后，单行不再重复主题名（分组标题已给），标题截断从 64 字收到
    48 字；×N源 表示轮内有 N 家源在报同一事件（多源互证=重要性的客观信号）。
    """
    t = (title or "").strip().replace("\n", " ")
    if len(t) > title_chars:
        t = t[:title_chars] + "…"
    meta = [f"{float(impact or 0.0):.2f}", f"{float(score or 0.0):+.2f}"]
    if ev_short:
        meta.append(ev_short)
    if source:
        meta.append(source)
    if int(n_same or 1) > 1:
        meta.append(f"×{int(n_same)}源")
    return f"· {t}｜{' · '.join(meta)}"


def digest_group(theme: str, lines: list, tone: str = "") -> str:
    """一个主题分组的摘要块：组标题（条数+多空）+ 组内行。整块参与切块，不被拆散。

    条数只统计以「·」开头的条目行，缩进的板块明细行不计入。
    """
    n = sum(1 for l in lines if l.startswith("·"))
    head = f"**{theme or '其他'}（{n}" + (f"，{tone}" if tone else "") + "）**"
    return head + "\n" + "\n".join(lines)


def _tone_str(counter: dict) -> str:
    """多空统计文本：利好4/利空1（为 0 的方向不出现，全 0 返回空串）。"""
    parts = [f"{name}{counter.get(name, 0)}"
             for name in ("利好", "利空", "中性") if counter.get(name, 0)]
    return "/".join(parts)


def digest_keywords(counter: dict, top_n: int = 8, min_count: int = 1) -> str:
    """关键词权重榜（文本版词云）：`算力12 · 回购8 · 三季报6`。

    2026-11-09 用户提的富媒体方案里，词云是「先看什么最热，再看具体条目」。
    企微群机器人的 markdown 不能内嵌图片，真词云得单独发 image 消息并给香橙派
    装 wordcloud+matplotlib+中文字体；这里用零依赖的权重榜承载同一信息：
    频次即视觉权重，降序即排名。统计口径只含本轮实际推送的条目。
    """
    if not counter or top_n <= 0:
        return ""
    ranked = sorted(((k, int(v)) for k, v in counter.items() if int(v) >= min_count),
                    key=lambda kv: (-kv[1], kv[0]))
    return " · ".join(f"{k}{v}" for k, v in ranked[:top_n])


def digest_mainline(counts: dict, order: list) -> str:
    """抬头「主线」：主题×条数，按条数降序（同条数保持首次出现序）。

    2026-11-09 dry-run 实测：按出现序会排出「白宫/贸易政策 1 · 机器人 3 …」——
    因为该组含全局最高影响分那条。题头叫「主线」就该按条数排；影响分顺序由
    正文分组顺序保持，两个维度各司其职。
    """
    items = sorted(((k, int(counts.get(k, 0))) for k in order), key=lambda kv: -kv[1])
    return " · ".join(f"{k} {n}" for k, n in items) or "（无）"


def build_digest(entries: list, header: str = "", max_bytes: int = 3800) -> list:
    """把摘要条目文本列表切成企微 markdown 消息块（每块 <= max_bytes 字节）。

    header 会附加到每一块开头，保证任意一条消息单独转发也能看懂；切块时
    先扣掉 header 的字节数，避免加完抬头反而超过企微 4096 字节上限。
    """
    entries = [e for e in (entries or []) if (e or "").strip()]
    if not entries:
        return []
    head = (header or "").strip()
    body_budget = max_bytes
    if head:
        body_budget = max(200, max_bytes - len(head.encode("utf-8")) - 2)
    chunks, cur = [], ""
    for line in entries:
        cand = f"{cur}\n\n{line}" if cur else line
        if cur and len(cand.encode("utf-8")) > body_budget:
            chunks.append(cur)
            cur = line
        else:
            cur = cand
    if cur:
        chunks.append(cur)
    return [f"{head}\n\n{c}" if head else c for c in chunks]


def run_once(cfg: dict, themes: list, seen: list, cold_start: bool,
             dry_run: bool, boards: dict, theme_boards: dict, top_n: int,
             no_stock_impact: bool = False) -> int:
    """一轮：抓取 -> 去重 -> 影响分排序 -> 匹配 -> 告警。返回本轮新条目数。

    seen 为按加入顺序的 id 列表；函数内用 seen_set 保证 O(1) 查重，
    同时按顺序 append 以支持 save_seen 的「先到先淘汰」裁剪。
    """
    seen_set = set(seen)
    enabled = ((cfg.get("monitor") or {}).get("enabled_sources")
               or (cfg.get("news") or {}).get("enabled_sources") or None)

    all_items = fetch_new_items(enabled)
    # 扩围逐股新闻（universe_tiers 清单，A股东财 + 美股 Finnhub）
    per_sym = (cfg.get("news") or {}).get("per_symbol") or {}
    if per_sym.get("enabled", True):
        try:
            uni = load_news_universe(cfg)
            sleep = float(per_sym.get("sleep", 0.25))
            sym_items = fetch_symbol_news(uni, sleep=sleep)
            all_items.extend(sym_items)
            fh_token = os.environ.get("FINNHUB_API_KEY", "").strip()
            us_items = fetch_us_symbol_news(uni, fh_token, days=2)
            all_items.extend(us_items)
            print(f"[monitor] 逐股新闻: A股 {len(sym_items)} 条 / 美股 {len(us_items)} 条"
                  f"（清单 {len(uni)} 只）")
        except Exception as e:  # noqa: BLE001
            print(f"[monitor] 逐股新闻失败: {type(e).__name__}")
    # 分派策略预警（ops-lessons §五：单批 >1000 条英文应转 Windows）
    en_vol = sum(1 for it in all_items if it.get("lang") == "en")
    en_warn = int((cfg.get("monitor") or {}).get("en_volume_warn", 1000))
    if en_vol > en_warn:
        # 邮件+横幅由 TM 侧摄取负责（data.notify + payload.warnings）；
        # 这里只留日志，避免同一预警走企微打扰用户
        print(f"[monitor] ⚠ 本轮英文条目 {en_vol} 条 > {en_warn} 阈值——"
              f"批量重打分如需执行应转 Windows（Pi 仅适合日常增量）", flush=True)
    # 去重
    uniq = {}
    for it in all_items:
        uniq.setdefault(it["id"], it)
    new_items = filter_recent([it for it in uniq.values() if it["id"] not in seen_set], days=30)

    # 实体链接：把市场级快讯归属到个股（monitor 此前完全没做文本打标，
    # 导致非逐股抓取的条目 symbols 恒空，个股情绪只剩逐股 feed 一条来源）。
    if (cfg.get("news") or {}).get("entity_link", True) and new_items:
        try:
            st = link_items(new_items)
            print(stats_line(st))
        except Exception as e:  # noqa: BLE001
            print(f"[monitor] 实体链接失败 {type(e).__name__}: {e}")

    if cold_start and new_items:
        print(f"[monitor] 首次运行：仅建立基线，跳过 {len(new_items)} 条历史消息，不告警")
        for it in new_items:
            seen.append(it["id"])
            seen_set.add(it["id"])
        save_seen(seen)
        append_raw(new_items)
        return len(new_items)

    # 影响分排序
    imp_cfg = cfg.get("impact") or {}
    weights = imp_cfg.get("weights") or None
    window_minutes = int(imp_cfg.get("window_minutes", 60))
    burst_cap = int(imp_cfg.get("burst_cap", 4))
    mon_cfg = cfg.get("monitor") or {}
    impact_min = float(mon_cfg.get("impact_min", 0.0))
    score_min = float(mon_cfg.get("score_min", 0.0))
    si_cfg = mon_cfg.get("stock_impact") or {}
    si_enabled = bool(si_cfg.get("enabled", False)) and not no_stock_impact
    si_max = int(si_cfg.get("max_stocks", 6))
    # 聚合推送：一轮内所有告警攒起来，run 末尾按摘要块合并推送（避免刷屏+企微限流）
    batch = bool(mon_cfg.get("push_batch", True))
    # 注意力预算（2026-11-08）：历史 104 轮实测均值 168.7 条告警/轮（早间积压轮 max 451），
    # 旧摘要格式共推 771 条企微消息 / 25 个交易日 ≈ 31 条/日，用户反馈刷屏。
    # 改为「同题去重 → 每主题配额 → 每轮 top-N」后回放为 1472 条告警 / 104 条消息（每轮 1 条，
    # 单条最大 2871 字节），被预算挤掉的 impact≥p90(0.66) 高影响条目 4 条。
    # 2026-11-09 香橙派 --once --dry-run 实测：新增 983 → 过阈值 198 → 推送 15，
    # 企微消息 1 条 / 3319 字节（同题抑制 18 / 配额 43 / 预算 122）。
    # 落选者仍写 alerts.jsonl（pushed=false）供 alert_review.py 闭环验证。
    max_alerts = int(mon_cfg.get("max_alerts_per_round", 15))
    max_per_theme = int(mon_cfg.get("max_per_theme", 3))
    dd_cfg = mon_cfg.get("dedupe") or {}
    cooldown = Cooldown(
        hours=float(dd_cfg.get("hours", 12)),
        jac_threshold=float(dd_cfg.get("jac_threshold", 0.80)),
        require_same_numbers=bool(dd_cfg.get("require_same_numbers", True)),
        enabled=bool(dd_cfg.get("enabled", True)),
        substring_containment=bool(dd_cfg.get("substring_containment", True)),
        min_contain_chars=int(dd_cfg.get("min_contain_chars", 12)))
    if not dry_run:
        try:
            cooldown.load(STATE_PATH).prune()
        except Exception as e:  # noqa: BLE001
            print(f"[monitor] 同题冷却状态载入失败（按空状态继续）: {type(e).__name__}")
    top_detail = int(mon_cfg.get("digest_top_detail", 3))
    top_keywords = int(mon_cfg.get("digest_top_keywords", 8))
    now = datetime.now()
    round_id = now.isoformat(timespec="minutes")

    new_items = compute_impact(new_items, themes, weights, window_minutes, burst_cap)

    # ---- 阶段 1：收集候选。这里刻意不做 LLM 个股映射（旧实现对每条命中都调一次，
    #      一轮 169 条就是 169 次；现在只对最终选中的十几条调）----
    cands: list = []
    ticker_cands: list = []
    n_below = 0
    for it in new_items:
        seen.append(it["id"])
        seen_set.add(it["id"])
        # 另类数据（国会/内部人交易）→ ticker 告警，不参与主题词匹配、不占主题预算
        kind = it.get("kind")
        if kind in ("congress_trade", "insider_trade") and it.get("ticker"):
            if _politician_allowed(it.get("politician"), cfg):
                ticker_cands.append(it)
            continue
        text = f"{it.get('title', '')} {it.get('content', '')}"
        score = it.get("sentiment", score_text(text))
        matched = match_themes(text, themes)
        if not matched:
            continue
        imp = float(it.get("impact") or 0.0)
        if impact_min > 0 and imp < impact_min:
            n_below += 1
            continue
        # 情绪地板：无方向性（词典/模型均未命中）的告警对用户是噪声
        if score_min > 0 and abs(float(score)) < score_min:
            n_below += 1
            continue
        cands.append({
            "item": it,
            "impact": imp,
            "score": float(score),
            "matched": matched,
            "themes": [th["name"] for th, _ in matched],
            # 事件类型标注（白名单=有 alpha，非白名单=无 alpha/反向）
            "ev": classify_event(text),
            # 历史波动参考（影响分校准：同类事件次日平均波动与大动静概率）
            "vol_ref": volatility_ref(text),
            "fp": fingerprint(it.get("title") or (it.get("content") or "")[:80]),
        })

    # ---- 阶段 2：筛选 ----
    picked, suppressed = select_alerts(
        cands, max_alerts=max_alerts, max_per_theme=max_per_theme, cooldown=cooldown,
        jac_threshold=cooldown.jac_threshold,
        require_same_numbers=cooldown.require_same_numbers,
        substring_containment=cooldown.substring_containment,
        min_contain_chars=cooldown.min_contain_chars)
    sup_sum = summarize_suppressed(suppressed)
    n_dup = sup_sum.get(REASON_DUP_ROUND, 0) + sup_sum.get(REASON_DUP_COOLDOWN, 0)
    n_quota = sup_sum.get(REASON_THEME_QUOTA, 0)
    n_budget = sup_sum.get(REASON_BUDGET, 0)

    # ---- 阶段 3：只对选中的做重活并生成告警 ----
    pending: list = []    # [(title, content)]，非聚合模式逐条推送用
    rows: list = []       # alerts.jsonl 流水（含落选候选）
    # 摘要按主题分组（2026-11-09 二版）：用户反馈平铺 15 条「一条一条抓不到重点」，
    # 改为抬头给主线（主题×条数）+ 多空统计，正文按主题分组、组内按影响分降序。
    groups: dict = {}          # 主题 -> [digest_line / 明细行]
    group_order: list = []     # 主题首次出现顺序（picked 已按影响分降序 ⇒ 组间即重要性序）
    group_tone: dict = {}      # 主题 -> {利好/利空/中性: n}
    tone_all: dict = {}
    kw_count: dict = {}        # 命中关键词 -> 出现条数（只统计实际推送的，做权重榜）
    # 明细行去重：板块明细只取决于命中的主题，同轮两条同主题新闻会渲染出
    # 一模一样的「人工智能(概念)：奥雅股份 +12.5%…」（2026-11-09 dry-run 实测），
    # 合并成一条消息后这种逐字重复特别显眼且白占字节，故同轮只保留首次出现。
    seen_details: set = set()

    for c in picked:
        it = c["item"]
        text = f"{it.get('title', '')} {it.get('content', '')}"
        # 合并告警：一条新闻只推一次，多主题合并展示（旧行为每主题各推一条，刷屏严重）
        merged_theme = {
            "name": "、".join(c["themes"]),
            "macro": any(th.get("macro") for th, _ in c["matched"]),
        }
        merged_hits, seen_h = [], set()
        for _th, hs in c["matched"]:
            for h in hs:
                if h not in seen_h:
                    seen_h.add(h)
                    merged_hits.append(h)
        for h in merged_hits:                    # 每条新闻对同一关键词只计 1 次
            kw_count[h] = kw_count.get(h, 0) + 1
        # LLM 个股影响映射：只对选中的告警调用（失败不阻塞）
        stock_map = []
        if si_enabled:
            try:
                stock_map = map_news_to_stocks(text, max_stocks=si_max)
            except Exception as e:  # noqa: BLE001
                print(f"  [monitor] 个股映射失败: {type(e).__name__}")
        title, content = build_alert(it, c["score"], merged_theme, merged_hits, boards,
                                     theme_boards, top_n, stock_map,
                                     _event_note(c["ev"]), c["vol_ref"],
                                     [th for th, _ in c["matched"]])
        print("\n" + "=" * 60)
        print(title)
        print(content)
        print("=" * 60 + "\n")
        key = (c["themes"] or ["其他"])[0]
        if key not in groups:
            groups[key] = []
            group_order.append(key)
            group_tone[key] = {}
        groups[key].append(digest_line(it.get("title") or text[:60], c["impact"],
                                       c["score"], _event_short(c["ev"]),
                                       it.get("source") or "", c.get("n_same", 1)))
        # 板块/个股明细：只给前 top_detail 个「互不相同」的，作为组内附加行
        detail = ""
        if len(seen_details) < top_detail:
            bl = fmt_boards([th for th, _ in c["matched"]], merged_hits, boards,
                            theme_boards, top_n)
            if bl:
                detail = bl[0]
            elif stock_map:
                r = stock_map[0]
                detail = (f"- {r.get('name')}({r.get('code')}) {r.get('direction')}："
                          f"{r.get('reason')}")
        if detail and detail in seen_details:
            detail = ""                      # 同轮已展示过同一份板块明细，不再重复
        elif detail:
            seen_details.add(detail)
            groups[key].append("  " + detail.strip())
        d = _event_dir(c["ev"], c["score"])
        group_tone[key][d] = group_tone[key].get(d, 0) + 1
        tone_all[d] = tone_all.get(d, 0) + 1
        pending.append((title, content))
        rows.append(alert_row(it, round_id, merged_theme["name"],
                              (c["vol_ref"] or {}).get("typ", ""), c["impact"],
                              c["score"], True, ""))

    # ticker 告警（国会/内部人交易）：量小价值高，始终推送，单独成组排在最前
    ticker_lines: list = []
    for it in ticker_cands:
        title, content = build_ticker_alert(it)
        print("\n" + "=" * 60)
        print(title)
        print(content)
        print("=" * 60 + "\n")
        label = "国会交易" if it.get("kind") == "congress_trade" else "内部人交易"
        ticker_lines.append(digest_line(f"{it.get('ticker') or '-'} {it.get('title') or ''}",
                                        float(it.get("impact") or 0.0),
                                        float(it.get("sentiment") or 0.0),
                                        "", it.get("source") or ""))
        pending.insert(0, (title, content))
        rows.append(alert_row(it, round_id, label, "", it.get("impact") or 0.0,
                              it.get("sentiment") or 0.0, True, "",
                              kind=it.get("kind") or "ticker"))

    # 落选候选入库（pushed=false + reason）：dashboard 默认不显示，
    # alert_review.py 用它对比「推了的 vs 没推的」次日波动，验证预算没有漏掉 alpha
    for c, reason in suppressed:
        it = c["item"]
        rows.append(alert_row(it, round_id, "、".join(c["themes"]),
                              (c["vol_ref"] or {}).get("typ", ""), c["impact"],
                              c["score"], False, reason))

    entries = []
    if ticker_lines:
        entries.append(digest_group("另类数据（国会/内部人交易）", ticker_lines))
    entries += [digest_group(k, groups[k], _tone_str(group_tone[k])) for k in group_order]

    alerts = len(picked) + len(ticker_cands)
    group_counts = {k: sum(1 for l in groups[k] if l.startswith("·")) for k in group_order}
    main_line = digest_mainline(group_counts, group_order)
    kw_line = digest_keywords(kw_count, top_keywords)
    head_lines = [
        f"**[事件告警汇总] {round_id[5:].replace('T', ' ')} · 推送 {alerts} 条"
        f"（过阈值 {len(cands)} / 新增 {len(new_items)}）**",
    ]
    if kw_line:                      # 热词榜排在最前：先看什么最热，再看具体条目
        head_lines.append(f"热词：{kw_line}")
    head_lines += [
        f"主线：{main_line}",
        f"多空：{_tone_str(tone_all) or '中性'}",
        f"抑制：同题 {n_dup} · 主题配额 {n_quota} · 预算 {n_budget}"
        f"（落选明细见 dashboard 告警历史）",
    ]
    header = "\n".join(head_lines)
    chunks = build_digest(entries, header)
    n_msgs = 0
    if entries:
        if dry_run:
            print("\n" + "#" * 60)
            for k, chunk in enumerate(chunks, 1):
                print(f"--- dry-run 摘要预览 {k}/{len(chunks)}"
                      f"（{len(chunk.encode('utf-8'))} 字节）---")
                print(chunk)
            print("#" * 60 + "\n")
        elif batch:
            for k, chunk in enumerate(chunks, 1):
                push_alert(cfg, f"[事件告警汇总] {len(entries)} 条（{k}/{len(chunks)}）",
                           chunk)
            n_msgs = len(chunks)
        else:
            for title, content in pending:
                push_alert(cfg, title, content)
            n_msgs = len(pending)

    if new_items:
        if dry_run:
            # dry-run 只读：不写 raw/history/seen，也不写告警流水与冷却状态，
            # 否则会「吃掉」真实轮次的新增（旧实现在 dry-run 下同样落盘，是个坑）
            print(f"[monitor] dry-run：跳过 raw/history/seen/alerts 落盘")
        else:
            append_raw(new_items)
            update_history(new_items)
            save_seen(seen)
            log_alerts(rows)
            for c in picked:
                cooldown.remember(fp=c["fp"], ts=now)
            for it in ticker_cands:
                cooldown.remember(it.get("title") or "", ts=now)
            cooldown.save()
        print(f"[monitor] 本轮新增 {len(new_items)} 条，过阈值候选 {len(cands)} 条"
              f"（低于阈值 {n_below} 条），推送 {alerts} 条"
              f"[同题抑制 {n_dup} / 配额 {n_quota} / 预算 {n_budget}]，"
              f"企微消息 {n_msgs if not dry_run else len(chunks)} 条"
              f"{'（dry-run 未推送）' if dry_run else ''}")
    else:
        print("[monitor] 本轮无新增")
    return len(new_items)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮即退")
    ap.add_argument("--interval", type=int, default=None, help="轮询秒数")
    ap.add_argument("--dry-run", action="store_true",
                    help="只检测不推送：跳过 raw/history/seen/alerts/state 全部落盘，打印摘要预览与字节数")
    ap.add_argument("--no-boards", action="store_true", help="跳过板块缓存构建")
    ap.add_argument("--no-stock-impact", action="store_true",
                    help="跳过 LLM 个股影响映射（API 不可用时避免每条新闻挂 90s）")
    args = ap.parse_args()

    load_env_file(ROOT / ".env")
    cfg = load_config()
    apply_primary_keys(cfg.get("primary") or {})
    configure_backend(cfg)
    mon = cfg.get("monitor") or {}
    interval = args.interval or int(mon.get("poll_interval", 15))
    top_n = int(mon.get("top_constituents", 5))
    refresh_hours = int(mon.get("board_cache_refresh_hours", 24))
    impact_min = float(mon.get("impact_min", 0.0))
    themes_path = mon.get("themes_path", "news_aggregator/themes.yaml")
    if not pathlib.Path(themes_path).is_absolute():
        themes_path = str(ROOT / themes_path)

    themes = load_themes(themes_path)
    print(f"[monitor] 载入主题 {len(themes)} 个，impact_min={impact_min}")

    seen = load_seen()
    cold_start = not SEEN_PATH.exists()

    boards, theme_boards = {}, {}
    if not args.no_boards:
        try:
            theme_names = {
                th["name"]: {
                    "concept_boards": th.get("concept_boards") or [],
                    "industry_boards": th.get("industry_boards") or [],
                }
                for th in themes
            }
            boards, theme_boards = get_board_cache(theme_names, top_n, refresh_hours)
        except Exception as e:  # noqa: BLE001
            print(f"[monitor] 板块缓存不可用（不影响告警）: {type(e).__name__}")

    print(f"[monitor] 启动：interval={interval}s dry_run={args.dry_run} "
          f"cold_start={cold_start} boards={len(boards)}")

    if args.once:
        run_once(cfg, themes, seen, cold_start, args.dry_run, boards, theme_boards,
                 top_n, args.no_stock_impact)
        return 0

    first = True
    while True:
        try:
            run_once(cfg, themes, seen, cold_start and first, args.dry_run, boards,
                     theme_boards, top_n, args.no_stock_impact)
            first = False
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[monitor] 收到中断，保存状态后退出")
            save_seen(seen)
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"[monitor] 本轮异常: {type(e).__name__}: {e}")
            time.sleep(max(interval, 5))


if __name__ == "__main__":
    sys.exit(main())


