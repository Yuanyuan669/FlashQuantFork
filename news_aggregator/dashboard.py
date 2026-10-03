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


def collect(news_dir: Path, days: int = 7, top: int = 50) -> dict:
    """聚合各数据源为渲染用 payload。"""
    items = _iter_raw(news_dir, days)
    alerts = _load_alerts(news_dir)

    total = len(items)
    themed = [it for it in items if (it.get("impact_parts") or {}).get("theme", 0.0) > 0]
    pos = sum(1 for it in items if float(it.get("sentiment") or 0) > 0.2)
    neg = sum(1 for it in items if float(it.get("sentiment") or 0) < -0.2)
    avg_impact = (sum(float(it.get("impact") or 0) for it in items) / total) if total else 0.0

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
                                           it.get("ts") or ""), reverse=True)[:top]
    top_news = [{
        "ts": (it.get("ts") or "")[:16].replace("T", " "),
        "source": it.get("source") or "",
        "title": (it.get("title") or it.get("content") or "")[:120],
        "url": it.get("url") or "",
        "sentiment": round(float(it.get("sentiment") or 0), 3),
        "impact": round(float(it.get("impact") or 0), 3),
        "symbols": (it.get("symbols") or [])[:6],
        "theme": (it.get("impact_parts") or {}).get("theme", 0.0) > 0,
        "lang": it.get("lang") or "",
    } for it in ranked]

    # 日度情绪
    market_trend, symbol_trends = [], {}
    ds_path = news_dir / "daily_sentiment.json"
    if ds_path.exists():
        try:
            ds = json.loads(ds_path.read_text(encoding="utf-8"))
            market_trend = _trend(ds.get("market"))
            symbol_trends = {k: _trend(v, 30) for k, v in (ds.get("symbols") or {}).items()}
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

  <div class="cards" id="cards"></div>

  <div class="panel">
    <h2>市场日度情绪 <small>daily_sentiment.json · 近 60 天</small></h2>
    <div id="market"></div>
    <div id="symbols" style="margin-top:12px"></div>
  </div>

  <div class="two">
    <div class="panel">
      <h2>来源分布 <small>Top 15</small></h2>
      <div id="sources"></div>
    </div>
    <div class="panel">
      <h2>情绪分布</h2>
      <div id="sentdist"></div>
    </div>
  </div>

  <div class="panel">
    <h2>高影响新闻 <small>按影响分排序 · Top <span id="topn"></span></small></h2>
    <div class="controls">
      <input type="text" id="q" placeholder="搜索标题...">
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
  const pts = rows.map((r,i)=>`<circle cx="${x(i).toFixed(1)}" cy="${y(r.score).toFixed(1)}" r="2" fill="${r.score>=0?'#3fb950':'#f85149'}"/>`).join('');
  const labels = [0, Math.floor(rows.length/2), rows.length-1]
    .filter((v,i,a)=>a.indexOf(v)===i && rows[v])
    .map(i=>`<text x="${x(i)}" y="${h-2}" text-anchor="middle">${rows[i].date}</text>`).join('');
  return `<svg viewBox="0 0 ${w} ${h+14}" style="width:100%">`+
    `<line x1="${pad}" y1="${h/2}" x2="${w-pad}" y2="${h/2}" stroke="#21262d"/>`+
    `<path d="${path}" fill="none" stroke="#58a6ff" stroke-width="1.5"/>${pts}${labels}</svg>`;
}
document.getElementById('market').innerHTML = lineChart(DATA.market_trend);

// 个股情绪
const sym = DATA.symbol_trends;
document.getElementById('symbols').innerHTML = Object.keys(sym).length
  ? Object.entries(sym).map(([s,rows])=>{
      const last = rows.length?rows[rows.length-1].score:0;
      return `<div class="hbar"><span class="label">${esc(s)}</span><span class="track"><span class="fill" style="width:${Math.min(100,Math.abs(last)*100)}%;background:${last>=0?'#3fb950':'#f85149'}"></span></span><span class="num">${fmt(last,2)}</span></div>`;
    }).join('')
  : '<div class="hint">无个股情绪（A股代码出现在新闻中时积累）</div>';

// 来源分布
const mx = Math.max(1, ...DATA.source_stats.map(s=>s.count));
document.getElementById('sources').innerHTML = DATA.source_stats.length
  ? DATA.source_stats.map(s=>`<div class="hbar"><span class="label" title="${esc(s.source)}">${esc(s.source)}</span><span class="track"><span class="fill" style="width:${(s.count/mx*100).toFixed(1)}%"></span></span><span class="num">${s.count} 条</span></div>`).join('')
  : '<div class="empty">暂无数据</div>';

// 情绪分布
function bucket(v){ return v<=-0.6?0 : v<=-0.2?1 : v<0.2?2 : v<0.6?3 : 4; }
const names=['强负面','负面','中性','正面','强正面'], colors=['#f85149','#f8514988','#8b949e','#3fb95088','#3fb950'];
const buckets=[0,0,0,0,0];
DATA.top_news.forEach(n=>buckets[bucket(n.sentiment)]++);
const bm=Math.max(1,...buckets);
document.getElementById('sentdist').innerHTML = names.map((n,i)=>
  `<div class="hbar"><span class="label">${n}</span><span class="track"><span class="fill" style="width:${(buckets[i]/bm*100).toFixed(1)}%;background:${colors[i]}"></span></span><span class="num">${buckets[i]}</span></div>`).join('');

// 新闻表（可过滤）
document.getElementById('topn').textContent = DATA.top_news.length;
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
['q','src','sort','imin','onlyth'].forEach(id=>{
  const el=document.getElementById(id);
  el.addEventListener(el.type==='range'?'input':'change',renderNews);
  el.addEventListener('input',renderNews);
});
renderNews();

// 告警表
const alerts=DATA.alerts;
document.getElementById('alerttable').innerHTML = alerts.length
  ? `<table><tr><th>时间</th><th>主题</th><th>事件类型</th><th>影响分</th><th>情绪</th><th>来源</th><th>标的</th></tr>`+
    alerts.map(a=>`<tr><td style="white-space:nowrap">${esc(a.ts)}</td><td>${esc(a.theme)}</td><td>${esc(a.event_type)}</td>`+
    `<td>${a.impact.toFixed(3)}</td><td>${sentChip(a.score)}</td><td>${esc(a.source)}</td>`+
    `<td>${a.symbols.map(x=>'<span class="chip sym">'+esc(x)+'</span>').join('')}</td></tr>`).join('')+'</table>'
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


def build(news_dir: Path, out: Path, days: int = 7, top: int = 50) -> Path:
    """收集数据并写出单文件 HTML，返回输出路径。"""
    data = collect(news_dir, days=days, top=top)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8")
    return out


def render_fresh(days: int = 7, top: int = 50, auto_refresh_sec: int = 60) -> str:
    """每次请求实时重建（serve 模式用）：收集 -> 渲染。"""
    root = Path(__file__).resolve().parents[1]
    data = collect(root / "news", days=days, top=top)
    return render(data, auto_refresh_sec=auto_refresh_sec)


def build_from_root(root: Path, days: int = 7, top: int = 50) -> Path:
    return build(root / "news", root / DEFAULT_OUT, days=days, top=top)


def open_in_browser(path: Path) -> None:
    webbrowser.open(path.resolve().as_uri())
