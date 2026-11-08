"""实体链接：把市场级快讯归属到具体个股。

为什么需要
----------
`tagger.tag()` 只对 config.yaml 里的 5 只做名称/代码匹配，`monitor.py` 甚至
没调用它。结果：118,390 条语料里只有 19.4% 带 symbols，而中文市场级快讯
几乎全部未归属（东财全球资讯 24,484 条 → 0.1%，华尔街见闻 7,315 → 0.0%，
财联社 3,051 → 0.1%）。这些条目已经在抓了，只是没人把它挂到股票上。

设计取舍（均有实测依据）
------------------------
1. **名称表用 data/stock_list.csv（5,549 只全 A 股），不用 config 的 5 只。**
   实测：未归属中文条目里有 9,342 条标题直接含公司名，命中 2,950 个代码。

2. **只匹配 >=3 字的名称。** 2 字名（197 个）歧义严重——`*ST发展` 去掉
   前缀后是「发展」，实测在 4,418 条标注样本上误配 9 次，在未归属语料上
   单是这一个键就刷出 3,234 次「提及」。宁可少认，不可错认。

3. **ST/*ST/退市/N/C 前缀剥离后若不足 3 字则丢弃。** `*ST发展` → `发展`
   被长度规则自动排除，不需要额外黑名单。

4. **标题命中进 `symbols`，正文独有命中只进 `symbols_body`。**
   实测主体级精度：仅标题 82.3%、标题首个 80.4%、全文本提及级 81.3%、
   标题退化为正文首个 58.2%。正文首个明显不可用（正文第一家公司常不是
   主体），所以正文命中不参与下游情绪聚合，只留档备查。

5. **全角转半角 + 去内部空格**：`万  科Ａ` → `万科A`，否则匹配不到。

已知局限（不掩饰）
------------------
- 82% 是相对「该条目来自哪只票的抓取源」的精度下限。剩余 18% 里，一部分
  是多公司同篇报道（合理），一部分是行业综述根本没点名（文本可匹配不到）。
- 同名词/品牌词仍可能误配（如「东方财富」既指公司也指平台）。名称表已
  去重歧义键，但无法消除行业惯用语的干扰。
- 该模块只处理 A 股 6 位代码名称；美股走 fetch_us_symbol_news 的 ticker
  精确标签，不经过这里。
"""
import csv
import re
import unicodedata

DEFAULT_LIST = "data/stock_list.csv"
MIN_NAME_LEN = 3

_SPACE_RE = re.compile(r"[\s\u3000]+")
_ST_PREFIX = re.compile(r"^(\*?ST|退市|N|C)(?=.)")

_index_cache = {}


def _norm(name: str) -> str:
    """全角转半角并去掉所有空白：`万  科Ａ` -> `万科A`。"""
    return _SPACE_RE.sub("", unicodedata.normalize("NFKC", str(name or "")))


def build_index(path=DEFAULT_LIST, min_len=MIN_NAME_LEN):
    """名称 -> 代码。歧义同名（映射到多个代码）整体弃用。

    返回 (name2code, regex)，regex 按名称长度降序排列，保证最长优先匹配
    （否则「中国石油」会先命中「中国石油化工」的前缀）。
    """
    key = (str(path), min_len)
    if key in _index_cache:
        return _index_cache[key]

    pairs = {}
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = (row.get("code") or "").strip()
            name = _norm(row.get("name"))
            if not code or not name:
                continue
            cands = {name}
            bare = _ST_PREFIX.sub("", name)
            if bare and bare != name:
                cands.add(bare)
            for c in cands:
                if len(c) < min_len:
                    continue
                pairs.setdefault(c, set()).add(code)

    ambiguous = {k for k, v in pairs.items() if len(v) > 1}
    name2code = {k: next(iter(v)) for k, v in pairs.items() if k not in ambiguous}
    keys = sorted(name2code, key=len, reverse=True)
    regex = re.compile("|".join(re.escape(k) for k in keys)) if keys else None

    out = (name2code, regex, len(ambiguous))
    _index_cache[key] = out
    return out


def link_text(title: str, content: str = "", path=DEFAULT_LIST):
    """返回 (标题命中代码, 仅正文命中代码)，均为去重后的有序列表。"""
    name2code, regex, _ = build_index(path)
    if regex is None:
        return [], []
    head = name2code_lookup(regex, str(title or ""))
    if not head:
        # 标题没有公司名时仍值得看正文，但只作为低置信档
        body = name2code_lookup(regex, str(content or ""))
        return [], sorted({name2code[n] for n in body})
    title_codes = sorted({name2code[n] for n in head})
    rest = name2code_lookup(regex, str(content or "")) - head
    return title_codes, sorted({name2code[n] for n in rest})


def name2code_lookup(regex, text: str) -> set:
    return {m.group(0) for m in regex.finditer(text)} if text else set()


def link_items(items: list, path=DEFAULT_LIST, min_len=MIN_NAME_LEN) -> dict:
    """原地给 items 补 symbols / symbols_body，返回统计。

    - 已有 symbols（抓取方精确打标）与标题链接结果取并集；
    - symbols_body 只记录正文独有的命中，**不并入 symbols**（精度 58%，见模块文档）；
    - 链接结果另存 symbols_linked，便于区分「抓来的」和「推出来的」。
    """
    name2code, regex, n_amb = build_index(path, min_len)
    if regex is None:
        return {"n_items": 0, "n_title": 0, "n_body": 0, "n_ambiguous": n_amb}

    n_title = n_body = 0
    n_items = 0
    for it in items:
        title = str(it.get("title", "") or "")
        content = str(it.get("content", "") or "")
        head = name2code_lookup(regex, title)
        body = name2code_lookup(regex, content) - head

        if head:
            codes = sorted({name2code[n] for n in head})
            cur = list(it.get("symbols") or [])
            added = [c for c in codes if c not in cur]
            if added:
                it["symbols"] = cur + added
                it["symbols_linked"] = codes
                n_title += 1
                n_items += 1
        if body:
            codes_b = sorted({name2code[n] for n in body})
            it["symbols_body"] = codes_b
            n_body += 1
            n_items += 1
    return {"n_items": n_items, "n_title": n_title, "n_body": n_body,
            "n_ambiguous": n_amb}


def stats_line(st: dict) -> str:
    return (f"[entity-link] 处理 {st['n_items']} 条 | 标题命中并入 symbols "
            f"{st['n_title']} 条 | 仅正文命中留档 {st['n_body']} 条 | "
            f"歧义名称弃用 {st['n_ambiguous']} 个")
