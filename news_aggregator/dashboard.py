"""静态 Dashboard 构建器：把 news/ 下的聚合产物渲染成单文件 HTML。

数据源（均为聚合器既有产物，不重复计算）：
- news/raw/{date}.jsonl     原始新闻（含 impact / sentiment / impact_parts）
- news/daily_sentiment.json 市场 + 个股日度情绪
- news/alerts.jsonl         实际推送过的告警流水（monitor 非 dry-run 才写）

输出：单文件 dashboard/index.html，零外部依赖（无 CDN / 无框架），
纯标准库生成，Linux / Windows 通用；浏览器直接打开即可。

刷新方式：
- run.py 每日聚合后自动重建（钩子在 run.py main 末尾，失败不影响主流程）
- 或手动：python scripts/generate_dashboard.py [--days 7 --top 50 --watch 60]
"""

import json
import re
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_OUT = "dashboard/index.html"


# ---------------- 数据收集 ----------------

def _iter_raw(news_dir: Path, days: int) -> list:
    """读取最近 days 天的 raw jsonl（缺文件/坏行容错）。"""
    today = datetime.now(TZ).date()
    items = []
    for offset in range(days):
        d = today - timedelta(days=offset)
        path = news_dir / "raw" / f"{d.strftime('%Y%m%d')}.jsonl"
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                it = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(it, dict):
                items.append(it)
    return items


def _load_alerts(news_dir: Path, limit: int = 100) -> list:
    path = news_dir / "alerts.jsonl"
    if not path.exists():
        return []
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            a = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(a, dict):
            out.append(a)
    return list(reversed(out))  # 最新在前


def _trend(series: dict, limit: int = 60) -> list:
    """{date: score} -> 按 date 升序的 [{date, score}]，取最后 limit 天。"""
    out = [{"date": d, "score": float(s)} for d, s in sorted((series or {}).items())]
    return out[-limit:]


def collect(news_dir: Path, days: int = 7, top: int = 100) -> dict:
    """聚合各数据源为渲染用 payload。"""
    items = _iter_raw(news_dir, days)
    alerts = _load_alerts(news_dir)

    total = len(items)
    themed = [it for it in items if (it.get("impact_parts") or {}).get("theme", 0.0) > 0]
    pos = sum(1 for it in items if float(it.get("sentiment") or 0) > 0.2)
    neg = sum(1 for it in items if float(it.get("sentiment") or 0) < -0.2)
    avg_impact = (sum(float(it.get("impact") or 0) for it in items) / total) if total else 0.0

    # 情绪分布（窗口全量，供环形图；与 KPI 同口径）
    def _bucket(v):
        return 0 if v <= -0.6 else 1 if v <= -0.2 else 2 if v < 0.2 else 3 if v < 0.6 else 4
    sentiment_dist = [0, 0, 0, 0, 0]
    for it in items:
        sentiment_dist[_bucket(float(it.get("sentiment") or 0))] += 1

    # 标的代码 -> 中文名（来自 universe_tiers 清单；未收录的显示代码）
    names = {}
    ut = Path(__file__).resolve().parents[1] / "data" / "universe_tiers.yaml"
    if ut.exists():
        try:
            import yaml as _yaml
            for s in (_yaml.safe_load(ut.read_text(encoding="utf-8")) or {}).get("symbols") or []:
                sym = s.get("symbol") or s.get("code")
                if sym:
                    names[sym] = s.get("name") or sym
        except Exception:  # noqa: BLE001
            pass

    # 来源分布
    src = {}
    for it in items:
        s = it.get("source") or "未知"
        st = src.setdefault(s, {"count": 0, "sent_sum": 0.0})
        st["count"] += 1
        st["sent_sum"] += float(it.get("sentiment") or 0)
    source_stats = sorted(
        ({"source": s, "count": v["count"],
          "avg_sentiment": round(v["sent_sum"] / v["count"], 3)}
         for s, v in src.items()),
        key=lambda x: x["count"], reverse=True)[:15]

    # 高影响新闻
    ranked = sorted(items, key=lambda it: (float(it.get("impact") or 0),
                                           it.get("ts") or ""), reverse=True)
    top_set = ranked[:top]

    # 每源保底：窗口内有产出的来源至少 3 条进入候选池（来源筛选的完备性——
    # 不然低量/低分源在筛选器里永远缺席）。补进的条目排尾部。
    top_ids = {it.get("id") for it in top_set}
    by_src_best = {}
    for it in ranked:
        s = it.get("source") or "未知"
        by_src_best.setdefault(s, []).append(it)
    backup = []
    for s, lst in by_src_best.items():
        have = sum(1 for it in top_set if (it.get("source") or "未知") == s)
        for it in lst[:3]:
            if it.get("id") not in top_ids and have < 3:
                backup.append(it)
                have += 1
    ranked_final = list(top_set) + backup

    _SCHEME = re.compile(r"https?:?//", re.I)

    def _safe_url(u):
        """历史数据坏链修复：双 scheme 拼接取最后一个 scheme 起点重建；修不了置空。"""
        u = (u or "").strip()
        if not u:
            return ""
        ms = [m for m in _SCHEME.finditer(u)
              if m.start() == 0 or u[m.start() - 1] not in "=&?"]
        if len(ms) >= 2:
            u = u[ms[-1].start():]
            if not u.lower().startswith(("http://", "https://")):
                u = re.sub(r"^(https?)//", r"\1://", u, flags=re.I)
        return u if u.startswith(("http://", "https://")) else ""

    top_news = [{
        "ts": (it.get("ts") or "")[:16].replace("T", " "),
        "source": it.get("source") or "",
        "title": (it.get("title") or it.get("content") or "")[:120],
        "url": _safe_url(it.get("url")),
        "sentiment": round(float(it.get("sentiment") or 0), 3),
        "impact": round(float(it.get("impact") or 0), 3),
        "symbols": (it.get("symbols") or [])[:6],
        "theme": (it.get("impact_parts") or {}).get("theme", 0.0) > 0,
        "lang": it.get("lang") or "",
    } for it in ranked_final]

    # 日度情绪：优先 sentiment_history.json（monitor 每轮增量更新，最实时）；
    # daily_sentiment.json 仅由 run.py 日聚合刷新，作回退
    market_trend, symbol_trends = [], {}
    sh_path = news_dir / "sentiment_history.json"
    if sh_path.exists():
        try:
            sh_data = json.loads(sh_path.read_text(encoding="utf-8"))
            market_trend = _trend(sh_data.get("market"))
            symbol_trends = {k: _trend(v, 30) for k, v in (sh_data.get("symbols") or {}).items()}
        except (json.JSONDecodeError, ValueError):
            pass
    if not market_trend:
        ds_path = news_dir / "daily_sentiment.json"
        if ds_path.exists():
            try:
                ds_data = json.loads(ds_path.read_text(encoding="utf-8"))
                market_trend = _trend(ds_data.get("market"))
                symbol_trends = {k: _trend(v, 30) for k, v in (ds_data.get("symbols") or {}).items()}
            except (json.JSONDecodeError, OSError):
                pass

    alert_rows = [{
        "ts": (a.get("ts") or "")[:19].replace("T", " "),
        "theme": a.get("theme") or "",
        "event_type": a.get("event_type") or "",
        "impact": round(float(a.get("impact") or 0), 3),
        "score": round(float(a.get("score") or 0), 3),
        "source": a.get("source") or "",
        "symbols": (a.get("symbols") or [])[:6],
    } for a in alerts]

    dates = sorted({it.get("date") for it in items if it.get("date")})
    return {
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "days": days,
        "range": f"{dates[0]} ~ {dates[-1]}" if dates else "-",
        "kpi": {
            "total": total,
            "themed": len(themed),
            "avg_impact": round(avg_impact, 3),
            "pos": pos,
            "neg": neg,
            "alerts": len(alert_rows),
            "sources": len(src),
        },
        "source_stats": source_stats,
        "sentiment_dist": sentiment_dist,
        "names": names,
        "top_news": top_news,
        "market_trend": market_trend,
        "symbol_trends": symbol_trends,
        "alerts": alert_rows,
    }


# ---------------- HTML 渲染 ----------------

_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
__REFRESH__<title>FlashQuant · 舆情 Dashboard</title>
<style>
:root{
  --bg:#0d1117;--panel:#161b22;--border:#21262d;--fg:#c9d1d9;--dim:#8b949e;
  --green:#3fb950;--red:#f85149;--blue:#58a6ff;--gold:#d29922;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:24px}
.wrap{max-width:1200px;margin:0 auto}
h1{font-size:20px;font-weight:600}
.meta{color:var(--dim);font-size:12px;margin:4px 0 20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.card .v{font-size:24px;font-weight:700}
.card .k{color:var(--dim);font-size:12px;margin-top:2px}
.card.g .v{color:var(--green)}.card.r .v{color:var(--red)}.card.b .v{color:var(--blue)}.card.y .v{color:var(--gold)}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:20px}
.panel h2{font-size:14px;font-weight:600;margin-bottom:12px;color:var(--fg)}
.panel h2 small{color:var(--dim);font-weight:400;margin-left:8px}
.hbar{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:12px}
.hbar .label{width:130px;text-align:right;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hbar .track{flex:1;background:var(--border);border-radius:3px;height:14px;overflow:hidden}
.hbar .fill{height:100%;background:var(--blue);border-radius:3px;min-width:1px}
.hbar .num{width:56px;color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:var(--dim);text-align:left;font-weight:500;padding:6px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid var(--border);vertical-align:top}
tr:hover td{background:#1c2129}
.chip{display:inline-block;padding:0 6px;border-radius:10px;font-size:11px;margin:1px 2px 1px 0}
.chip.pos{background:#0d4429;color:var(--green)}.chip.neg{background:#5c1a1a;color:var(--red)}.chip.mid{background:#30363d;color:var(--dim)}
.chip.theme{background:#1f3a5f;color:var(--blue)}
.chip.sym{background:#2d2a12;color:var(--gold)}
.ibar{display:inline-block;height:10px;background:linear-gradient(90deg,#1f6feb,#58a6ff);border-radius:2px;vertical-align:middle}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
.controls{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:12px;font-size:12px}
.controls input,.controls select{background:#0d1117;border:1px solid var(--border);color:var(--fg);border-radius:6px;padding:4px 8px}
.controls input[type=text]{width:220px}
.empty{color:var(--dim);text-align:center;padding:24px}
.hint{color:var(--dim);font-size:12px;margin-top:8px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:800px){.two{grid-template-columns:1fr}}
svg text{fill:var(--dim);font-size:10px}
</style>
</head>
<body>
<div class="wrap">
  <h1>⚡ FlashQuant · 舆情 Dashboard</h1>
  <div class="meta">生成时间 <span id="gen"></span> ｜ 数据区间 <span id="range"></span>（近 <span id="days"></span> 天）｜ 刷新：<code>python scripts/generate_dashboard.py</code></div>
  <div id="warnbar"></div>

  <div class="cards" id="cards"></div>

  <div class="panel">
    <h2>市场日度情绪 <small>sentiment_history.json · 近 60 天 · 点色：绿=当日≥0，红=当日&lt;0</small></h2>
    <div id="market"></div>
    <div id="symbols" style="margin-top:12px"></div>
  </div>

  <div class="two">
    <div class="panel">
      <h2 id="h2src">来源分布 <small>窗口全量（与 KPI 同口径）</small></h2>
      <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
        <div id="srcdonut"></div><div id="srclegend" style="flex:1;min-width:220px"></div>
      </div>
    </div>
    <div class="panel">
      <h2>情绪分布 <small>窗口全量（与 KPI 同口径）</small></h2>
      <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
        <div id="sentdonut"></div><div id="sentlegend" style="flex:1;min-width:220px"></div>
      </div>
    </div>
  </div>

  <div class="panel">
    <h2>高影响新闻 <small>影响分 Top 切片（展示容量 300，全量在归档/TM 库）</small></h2>
    <div class="controls">
      <select id="tn"><option value="25" selected>25</option><option value="50">50</option><option value="100">100</option><option value="99999">全部</option></select>
      <label>每源限额 <select id="srccap"><option value="0">不限</option><option value="20" selected>20 条</option><option value="10">10 条</option><option value="5">5 条</option></select></label><input type="text" id="q" placeholder="搜索标题...">
      <select id="src"><option value="">全部来源</option></select>
      <select id="sort">
        <option value="impact">按影响分</option>
        <option value="time">按时间</option>
        <option value="sent">按情绪</option>
      </select>
      <label>影响分 ≥ <span id="imv">0</span> <input type="range" id="imin" min="0" max="100" value="0"></label>
      <label><input type="checkbox" id="onlyth"> 只看命中主题</label>
      
    </div>
    <div id="newstable"></div>
  </div>

  <div class="panel">
    <h2>告警历史 <small>alerts.jsonl · 仅记录实际推送过的告警</small></h2>
    <div id="alerttable"></div>
  </div>
</div>

<script id="payload" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('payload').textContent);
const esc = s => String(s??'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (v,d=3) => (v>0?'+':'')+Number(v).toFixed(d);

// 预警横幅（payload.warnings）
const warns = DATA.warnings || [];
if (warns.length) {
  document.getElementById('warnbar').innerHTML = warns.map(w =>
    `<div style="background:#5c1a1a;color:#f85149;padding:8px 12px;border-radius:6px;margin-bottom:8px">⚠ ${esc(w)}</div>`).join('');
}

// KPI
const k = DATA.kpi;
document.getElementById('gen').textContent = DATA.generated_at;
document.getElementById('range').textContent = DATA.range;
document.getElementById('days').textContent = DATA.days;
document.getElementById('cards').innerHTML = [
  ['新闻总数', k.total, ''], ['命中主题', k.themed, 'y'], ['数据源', k.sources, ''],
  ['平均影响分', k.avg_impact.toFixed(3), 'b'],
  ['正面新闻', k.pos, 'g'], ['负面新闻', k.neg, 'r'], ['告警', k.alerts, 'r'],
].map(([t,v,c]) => `<div class="card ${c}"><div class="v">${v}</div><div class="k">${t}</div></div>`).join('');

// 情绪折线（SVG）
function lineChart(rows, h=120){
  if(!rows.length) return '<div class="empty">暂无数据（run.py 生成 daily_sentiment.json 后刷新）</div>';
  const w = 900, pad = 30;
  const vs = rows.map(r=>r.score), mx = Math.max(0.2, ...vs.map(Math.abs));
  const x = i => pad + i*(w-2*pad)/Math.max(1,rows.length-1);
  const y = v => h/2 - v/mx*(h/2-10);
  const path = rows.map((r,i)=>(i?'L':'M')+x(i).toFixed(1)+','+y(r.score).toFixed(1)).join('');
  const pts = rows.map((r,i)=>`<circle cx="${x(i).toFixed(1)}" cy="${y(r.score).toFixed(1)}" r="3" fill="${r.score>=0?'#3fb950':'#f85149'}" style="cursor:pointer"><title>${rows[i].date}：${r.score.toFixed(3)}</title></circle>`).join('');
  const labels = [0, Math.floor(rows.length/2), rows.length-1]
    .filter((v,i,a)=>a.indexOf(v)===i && rows[v])
    .map(i=>`<text x="${x(i)}" y="${h-2}" text-anchor="middle">${rows[i].date}</text>`).join('');
  return `<svg viewBox="0 0 ${w} ${h+14}" style="width:100%">`+
    `<line x1="${pad}" y1="${h/2}" x2="${w-pad}" y2="${h/2}" stroke="#21262d"/>`+
    `<path d="${path}" fill="none" stroke="#58a6ff" stroke-width="1.5"/>${pts}${labels}</svg>`;
}
document.getElementById('market').innerHTML = lineChart(DATA.market_trend);

// 个股情绪（默认折叠表格：中文名 / 最新分 / 迷你走势 / 覆盖天数）
const nm = DATA.names || {};
const sym = DATA.symbol_trends || {};
const symRows = Object.entries(sym).map(([s, rows]) => ({
  s, rows, last: rows.length ? rows[rows.length - 1].score : 0
})).sort((a, b) => b.last - a.last);
function spark(rows){
  if(!rows.length) return '';
  const w=64,h=16,vs=rows.map(r=>r.score);
  const mx=Math.max(0.05,...vs.map(Math.abs));
  const pts=rows.map((r,i)=>`${(i*w/Math.max(1,rows.length-1)).toFixed(1)},${(h/2-r.score/mx*(h/2-1)).toFixed(1)}`).join(' ');
  return `<svg width="${w}" height="${h}" style="vertical-align:middle"><polyline points="${pts}" fill="none" stroke="#58a6ff" stroke-width="1"/></svg>`;
}
const symChip = v => v>0.2?'<span class="chip pos">'+fmt(v,2)+'</span>'
  : v<-0.2?'<span class="chip neg">'+fmt(v,2)+'</span>'
  : '<span class="chip mid">'+fmt(v,2)+'</span>';
document.getElementById('symbols').innerHTML = symRows.length
  ? `<details><summary style="cursor:pointer">共 ${symRows.length} 只（点击展开）</summary>
      <table style="margin-top:8px"><tr><th>标的</th><th>最新情绪分</th><th>近30日走势</th><th>覆盖天数</th></tr>` +
      symRows.map(r=>`<tr><td title="${esc(r.s)}">${esc(nm[r.s] || r.s)}</td><td>${symChip(r.last)}</td>` +
        `<td>${spark(r.rows)}</td><td>${r.rows.length}</td></tr>`).join('') +
      `</table></details>`
  : '<div class="hint">无个股情绪（A股代码出现在新闻中时积累）</div>';

function donut(data){ // data: [{label, value, color}]，SVG 环形图 + 原生 tooltip
  const total = data.reduce((s,d)=>s+d.value,0) || 1;
  let acc = 0; const segs = [];
  const R=52, C=2*Math.PI*R;
  for(const d of data){
    const frac = d.value/total;
    segs.push(`<circle r="${R}" cx="70" cy="70" fill="none" stroke="${d.color}" stroke-width="20" stroke-dasharray="${(frac*C).toFixed(2)} ${(C+1).toFixed(2)}" stroke-dashoffset="${(-acc*C).toFixed(2)}" transform="rotate(-90 70 70)"><title>${esc(d.label)}：${d.value}（${(frac*100).toFixed(1)}%）</title></circle>`);
    acc += frac;
  }
  return `<svg width="140" height="140" viewBox="0 0 140 140">${segs.join('')}<text x="70" y="67" text-anchor="middle" fill="#c9d1d9" font-size="14" font-weight="600">${total}</text><text x="70" y="83" text-anchor="middle" fill="#8b949e" font-size="9">总计</text></svg>`;
}
function legend(data){
  const total = data.reduce((s,d)=>s+d.value,0) || 1;
  return data.map(d=>`<div class="hbar"><span class="label" title="${esc(d.label)}">${esc(d.label)}</span><span class="track"><span class="fill" style="width:${(d.value/total*100).toFixed(1)}%;background:${d.color}"></span></span><span class="num">${d.value}（${(d.value/total*100).toFixed(0)}%）</span></div>`).join('');
}
const PALETTE = ['#58a6ff','#3fb950','#d29922','#f85149','#bc8cff','#39c5cf','#ff7b72','#79c0ff','#d2a8ff','#7ee787','#ffa657','#8b949e'];

// 来源分布
const srcAll = DATA.source_stats;
const srcTop = srcAll.slice(0, 8);
const srcRest = srcAll.slice(8).reduce((s,d)=>s+d.count,0);
const srcData = (srcRest ? srcTop.concat([{label:'其他', value:srcRest}]) : srcTop)
  .map(d => ({label: d.source || d.label, value: d.value || d.count, color: PALETTE[0]}));
srcData.forEach((d,i)=> d.color = PALETTE[i % PALETTE.length]);
document.getElementById('srcdonut').innerHTML = donut(srcData);
document.getElementById('srclegend').innerHTML = legend(srcData);

// 情绪分布（窗口全量，payload 预计算）
const SENT_NAMES = ['强负面','负面','中性','正面','强正面'];
const SENT_COLORS = ['#f85149','#f8514988','#8b949e','#3fb95088','#3fb950'];
const buckets = DATA.sentiment_dist || [0,0,0,0,0];
const sentData = SENT_NAMES.map((n,i)=>({label:n, value:buckets[i], color:SENT_COLORS[i]}));
document.getElementById('sentdonut').innerHTML = donut(sentData);
document.getElementById('sentlegend').innerHTML = legend(sentData);

// 新闻表（可过滤）

const srcSel = document.getElementById('src');
[...new Set(DATA.top_news.map(n=>n.source))].sort().forEach(s=>{
  const o=document.createElement('option'); o.value=s; o.textContent=s; srcSel.appendChild(o);
});
const sentChip = v => v>0.2?'<span class="chip pos">'+fmt(v,2)+'</span>'
  : v<-0.2?'<span class="chip neg">'+fmt(v,2)+'</span>'
  : '<span class="chip mid">'+fmt(v,2)+'</span>';
function renderNews(){
  const q=document.getElementById('q').value.toLowerCase();
  const s=srcSel.value, sort=document.getElementById('sort').value;
  const im=document.getElementById('imin').value/100;
  document.getElementById('imv').textContent=im.toFixed(2);
  const only=document.getElementById('onlyth').checked;
  let rows=DATA.top_news.filter(n=>
    (!q || (n.title+n.symbols.join('')).toLowerCase().includes(q)) &&
    (!s || n.source===s) && n.impact>=im && (!only || n.theme));
  if(sort==='time') rows=rows.slice().sort((a,b)=>b.ts.localeCompare(a.ts));
  else if(sort==='sent') rows=rows.slice().sort((a,b)=>b.sentiment-a.sentiment);
  const tn=parseInt(document.getElementById('tn').value,10)||rows.length;
  rows=rows.slice(0,tn);
  // 每源限额：防止单一高量渠道霸占整个 top 榜（cap 设 0 = 关闭）
  const cap=parseInt(document.getElementById('srccap').value,10)||0;
  if(cap){
    const cnt={};
    rows=rows.slice().sort((a,b)=>b.impact-a.impact).filter(n=>{
      cnt[n.source]=(cnt[n.source]||0)+1;
      return cnt[n.source]<=cap;
    });
  }
  document.getElementById('newstable').innerHTML = rows.length
    ? `<table><tr><th>时间</th><th>来源</th><th>标题</th><th>情绪</th><th>影响分</th><th>标签</th></tr>`+
      rows.map(n=>`<tr><td style="white-space:nowrap">${esc(n.ts)}</td><td style="white-space:nowrap">${esc(n.source)}</td>`+
      `<td>${n.url?`<a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a>`:esc(n.title)}</td>`+
      `<td>${sentChip(n.sentiment)}</td>`+
      `<td><span class="ibar" style="width:${(n.impact*100).toFixed(0)}px"></span> ${n.impact.toFixed(3)}</td>`+
      `<td>${n.theme?'<span class="chip theme">主题</span>':''}${n.symbols.map(x=>'<span class="chip sym">'+esc(x)+'</span>').join('')}</td></tr>`).join('')+
      '</table>'
    : '<div class="empty">无匹配新闻</div>';
}
['q','src','sort','imin','onlyth','tn','srccap'].forEach(id=>{
  const el=document.getElementById(id);
  el.addEventListener(el.type==='range'?'input':'change',renderNews);
  el.addEventListener('input',renderNews);
});
renderNews();

// 告警表（默认折叠）
const alerts=DATA.alerts;
document.getElementById('alerttable').innerHTML = alerts.length
  ? `<details><summary style="cursor:pointer">共 ${alerts.length} 条（点击展开）</summary>
     <table style="margin-top:8px"><tr><th>时间</th><th>主题</th><th>事件类型</th><th>影响分</th><th>情绪</th><th>来源</th><th>标的</th></tr>`+
    alerts.map(a=>`<tr><td style="white-space:nowrap">${esc(a.ts)}</td><td>${esc(a.theme)}</td><td>${esc(a.event_type)}</td>`+
    `<td>${a.impact.toFixed(3)}</td><td>${sentChip(a.score)}</td><td>${esc(a.source)}</td>`+
    `<td>${a.symbols.map(x=>'<span class="chip sym">'+esc(x)+'</span>').join('')}</td></tr>`).join('')+'</table></details>'
  : '<div class="empty">暂无告警记录（alerts.jsonl 仅在非 dry-run 推送成功后写入）</div>';
</script>
</body>
</html>
"""


def render(data: dict, auto_refresh_sec: int = 0) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    refresh = (f'<meta http-equiv="refresh" content="{int(auto_refresh_sec)}">'
               if auto_refresh_sec > 0 else "")
    return _HTML.replace("__REFRESH__", refresh).replace("__DATA__", payload)


def build(news_dir: Path, out: Path, days: int = 7, top: int = 100) -> Path:
    """收集数据并写出单文件 HTML，返回输出路径。"""
    data = collect(news_dir, days=days, top=top)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8")
    return out


def render_fresh(days: int = 7, top: int = 100, auto_refresh_sec: int = 60) -> str:
    """每次请求实时重建（serve 模式用）：收集 -> 渲染。"""
    root = Path(__file__).resolve().parents[1]
    data = collect(root / "news", days=days, top=top)
    return render(data, auto_refresh_sec=auto_refresh_sec)


def build_from_root(root: Path, days: int = 7, top: int = 100) -> Path:
    return build(root / "news", root / DEFAULT_OUT, days=days, top=top)


def open_in_browser(path: Path) -> None:
    webbrowser.open(path.resolve().as_uri())
