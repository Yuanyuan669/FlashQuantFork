"""富媒体图片卡片：把一轮告警渲染成 PNG（真词云 + 分组告警列表），经企微 msgtype=image 推送。

为什么用 Pillow 而不是 wordcloud / matplotlib（2026-11-09）：
- 香橙派 venv 里两者都没有，wordcloud 需要 Cython 编译，matplotlib 会拖进一堆科学栈依赖；
  Pillow 单包（manylinux/aarch64 有 wheel）就能画文字、排版、导出 PNG。
- 词云不需要专用库：按词频定字号 + 阿基米德螺线逐个试位 + 矩形碰撞检测，几十个词毫秒级。
- 企微群机器人的 markdown 不能内嵌图片，所以图片必须单独发一条 msgtype=image
  （base64 + md5，官方限 2MB）；文本摘要仍照发，图片不能复制检索，两者互补。

纯计算部分（cloud_font_size / place_cloud / wrap_cjk / build_payload）不依赖 Pillow，
measure 用回调注入，便于离线单测；只有 render 需要 PIL，导入放在函数内。
"""

from __future__ import annotations

import math
import os
import re

DEFAULT_WIDTH = 1080
MARGIN = 40
CLOUD_H = 360
MAX_IMAGE_BYTES = 1_900_000     # 官方 2MB，留 5% 余量：超限即跳过图片，不硬发

# 配色：深色卡片（企微聊天窗浅色/深色主题都能看清），A 股习惯红涨绿跌
BG = (18, 21, 28)
PANEL = (26, 30, 39)
FG = (233, 237, 244)
DIM = (148, 157, 172)
UP = (226, 90, 90)              # 利好 / 正分
DOWN = (62, 178, 122)           # 利空 / 负分
ACCENT = (240, 182, 55)         # 主题抬头
CLOUD_COLORS = (ACCENT, (120, 190, 240), (200, 150, 235), (150, 220, 170), FG)

FONT_CANDIDATES = (
    # 实测 Noto Sans CJK SC（简中字面）在 .ttc 里是 index 2，故按名字挑而不是写死序号
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "C:/Windows/Fonts/msyh.ttc",          # 开发机（Windows）预览用
    "C:/Windows/Fonts/simhei.ttf",
)

# 折行单位：英文/数字串是一个词（连写，不在中间断开），其余按单字符（中日韩、标点）
_TOKEN_RE = re.compile(r"[A-Za-z0-9%$+\-.,'&/:;_=#]+|[^\sA-Za-z0-9%$+\-.,'&/:;_=#]|\s+")


# ---------------- 纯计算（无需 Pillow，可离线单测） ----------------

def cloud_font_size(count, max_count, min_size: int = 26, max_size: int = 66) -> int:
    """词频 → 字号：幂律压缩（0.55），否则最高频词与其它词差距过大，词云会挤成一行。"""
    if max_count <= 0:
        return min_size
    ratio = min(1.0, max(0.0, float(count) / float(max_count)))
    return int(round(min_size + (max_size - min_size) * (ratio ** 0.55)))


def units(text: str) -> list:
    """把标题切成可断行单位：中日韩文字与标点逐字、英文数字串整块、空白折成一个空格。

    空格必须保留成一个单位：CJK 之间手写的间隔（"主线：A ×1 B ×1"）若被丢掉，
    折行后两段会粘成 "A×1B×1"，图就没法读了。
    """
    out: list = []
    for u in _TOKEN_RE.findall(text or ""):
        if u.isspace():
            if out and out[-1] != " ":
                out.append(" ")
        else:
            out.append(u)
    while out and out[-1] == " ":
        out.pop()
    return out


def wrap(text: str, max_w: float, measure, size: int, max_lines: int = 2,
         ellipsis: str = "…") -> list:
    """按像素宽度折行，最多 max_lines 行；有剩余单位时末行以省略号收尾。

    measure(text, size) -> 宽度（float）。原文里的空白折成一个空格并保留；
    CJK 之间本来没有空格的地方不会被硬塞空格。
    """
    us = units(text)
    if not us:
        return []
    lines, cur = [], ""
    for u in us:
        cand = cur + u
        if cur and measure(cand, size) > max_w:
            lines.append(cur.rstrip())
            cur = "" if u.isspace() else u
            if len(lines) == max_lines:
                break
        else:
            cur = cand
    if len(lines) < max_lines:
        lines.append(cur.rstrip())
    elif cur:                       # 吃满 max_lines 后仍有剩余：末行截短加省略号
        last = lines[-1]
        while last and measure(last + ellipsis, size) > max_w:
            last = last[:-1]
        lines[-1] = (last or lines[-1][:1]).rstrip() + ellipsis
    return [l for l in lines if l]


def _short_round(round_id) -> str:
    """轮次 ID 精简成 "11-09 12:42"，与文本摘要抬头一致（图上放 ISO 全串太长且难读）。"""
    s = str(round_id or "").strip().replace("T", " ")
    if len(s) >= 16 and s[4] == "-" and s[7] == "-":
        return s[5:16]
    return s


def place_cloud(words, area_w: float, area_h: float, measure, *,
                min_size: int = 24, max_size: int = 64,
                gap: int = 8, max_steps: int = 6000) -> tuple:
    """词云排布：词频定字号，从中心沿阿基米德螺线找第一个不碰撞的空位。

    words: [(文本, 频次)]，按频次降序放置（高频先占中心位）。
    measure(text, size) -> (w, h)。
    返回 (placed, skipped)：placed = [{text,size,x,y,w,h,color_idx}]，
    skipped = 放不下（区域太小/词太长）的词列表——调用方据此决定要不要缩小字号。
    """
    ordered = sorted(words or [], key=lambda kv: -int(kv[1] or 0))
    ordered = [kv for kv in ordered if str(kv[0] or "").strip()]
    if not ordered:
        return [], []
    max_count = max(int(c) for _t, c in ordered) or 1
    cx, cy = area_w / 2.0, area_h / 2.0
    placed: list = []
    skipped: list = []
    for text, count in ordered:
        size = cloud_font_size(count, max_count, min_size, max_size)
        w, h = measure(text, size)
        w += gap
        h += gap
        if w > area_w or h > area_h:
            skipped.append(text)
            continue
        step_a, step_r = 0.35, 2.2
        got = None
        for i in range(max_steps):
            r = step_r * i * 0.06
            ang = step_a * i
            x = cx + r * math.cos(ang) - w / 2.0
            y = cy + r * 0.62 * math.sin(ang) - h / 2.0
            if x < 0 or y < 0 or x + w > area_w or y + h > area_h:
                continue
            if any(not (x + w <= p["x"] or x >= p["x"] + p["w"]
                        or y + h <= p["y"] or y >= p["y"] + p["h"]) for p in placed):
                continue
            got = (x, y)
            break
        if got is None:
            skipped.append(text)
            continue
        placed.append({"text": text, "size": size, "count": int(count),
                       "x": int(got[0]), "y": int(got[1]), "w": int(w), "h": int(h),
                       "color_idx": len(placed) % len(CLOUD_COLORS)})
    return placed, skipped


def build_payload(round_id: str, stats: dict, groups: list, hot_words: dict,
                  tone_all: dict, suppressed: dict, top_n: int = 8) -> dict:
    """组卡片数据（纯字典，便于单测与复用）。

    groups 元素：{"theme": str, "tone": {方向: n}, "items": [{"title","impact","score",
    "ev","source","n_same"}]}，顺序即展示顺序（重要性降序）。
    """
    hot = sorted((hot_words or {}).items(), key=lambda kv: (-int(kv[1]), kv[0]))[:top_n]
    main = []
    for g in (groups or []):
        n = len(g.get("items") or [])
        if n:
            main.append((g.get("theme") or "其他", n))
    return {"round_id": round_id, "stats": dict(stats or {}), "hot": hot,
            "mainline": main, "tone": dict(tone_all or {}),
            "suppressed": dict(suppressed or {}), "groups": groups or []}


# ---------------- 渲染（需要 Pillow） ----------------

def find_font(path: str = "", bold: bool = True) -> tuple:
    """返回 (字体路径, ttc index)：优先挑简中（SC）字面，其次任意非 Mono 字面。

    没找到返回 ("", 0)，由 render() 明确报错，而不是用 PIL 默认字体静默画出中文方框。
    """
    import glob

    cands = [path] if path else list(FONT_CANDIDATES)
    fallback = None
    for pat in cands:
        for p in (glob.glob(pat) if "*" in pat else [pat]):
            if not p or not os.path.exists(p):
                continue
            for idx in range(0, 12):
                try:
                    fam, style = _pil_font(p, 20, idx).getname()
                except Exception:  # noqa: BLE001 — index 越界即该集合已遍历完
                    break
                name = f"{fam} {style}"
                if "Mono" in name:
                    continue
                if fallback is None:
                    fallback = (p, idx)
                if "SC" in name and (("Bold" in name) == bool(bold)):
                    return p, idx
            if fallback is None:
                fallback = (p, 0)
    return fallback or ("", 0)


def _pil_font(path: str, size: int, index: int = 0):
    from PIL import ImageFont

    return ImageFont.truetype(path, size, index=index)


def render(payload: dict, out_path: str, *, width: int = DEFAULT_WIDTH,
           font_bold: str = "", font_regular: str = "") -> dict:
    """渲染 PNG 到 out_path。返回 {path, bytes, width, height, skipped_cloud}。

    失败（无字体/无 Pillow/超大）抛异常，由调用方降级为只发文本。
    """
    from PIL import Image, ImageDraw

    bp, bi = find_font(font_bold, bold=True)
    rp, ri = find_font(font_regular or bp, bold=False)
    if not bp:
        raise RuntimeError("未找到中文字体（Noto Sans CJK / 微软雅黑），图片卡片跳过")
    cache: dict = {}

    def font(size, bold):
        key = (size, bool(bold))
        if key not in cache:
            cache[key] = _pil_font(bp if bold else rp, size, bi if bold else ri)
        return cache[key]

    def measure(text, size, bold=False):
        f = font(size, bold)
        b = f.getbbox(text)
        return b[2] - b[0], b[3] - b[1]

    def measure_w(text, size, bold=False):
        return measure(text, size, bold)[0]

    inner = width - 2 * MARGIN
    groups = payload.get("groups") or []
    ops: list = []            # [(kind, args...)]，两遍：先量高再画
    y = MARGIN

    # 抬头
    rid = _short_round(payload.get("round_id"))
    title = "事件告警汇总"
    ops.append(("text", MARGIN, y, title, 46, True, FG))
    tw = measure_w(title, 46, True)
    if rid:
        ops.append(("text", MARGIN + tw + 18, y + 12, rid, 32, False, DIM))
    st = payload.get("stats") or {}
    n_pushed = int(st.get("pushed") or sum(len(g.get("items") or []) for g in groups))
    right = f"推送 {n_pushed} 条"
    if st.get("over") is not None:
        right += f" / 过阈值 {st['over']}"
    if st.get("new") is not None:
        right += f" / 新增 {st['new']}"
    ops.append(("text", width - MARGIN - measure_w(right, 30), y + 14, right, 30, False, DIM))
    y += 66
    ops.append(("line", MARGIN, y, width - MARGIN, y, 2, (58, 66, 82)))
    y += 22

    # 主线 + 多空
    main = payload.get("mainline") or []
    if main:
        line = "主线：" + " · ".join(f"{t} ×{n}" for t, n in main)
        for seg in wrap(line, inner, measure_w, 32, max_lines=2):
            ops.append(("text", MARGIN, y, seg, 32, False, FG))
            y += 42
    tone = payload.get("tone") or {}
    if tone:
        parts = [(f"利好 {tone.get('利好', 0)}", UP), (f"利空 {tone.get('利空', 0)}", DOWN)]
        if tone.get("中性"):
            parts.append((f"中性 {tone['中性']}", DIM))
        x = MARGIN
        for txt, col in parts:
            ops.append(("text", x, y, txt, 32, True, col))
            x += measure_w(txt, 32, True) + 28
        y += 48

    # 词云
    hot = payload.get("hot") or []
    skipped = []
    if hot:
        # 条带高度随词数收缩：固定 360px 在只有 3~4 个热词时会留大片空白，图上很空
        cloud_h = max(150, min(CLOUD_H, 96 + 33 * len(hot)))
        cloud_top = y
        placed, skipped = place_cloud([(t, c) for t, c in hot], inner, cloud_h,
                                     lambda t, s: measure(t, s, True),
                                     min_size=24, max_size=58)
        for p in placed:
            ops.append(("text", MARGIN + p["x"] + 4, cloud_top + p["y"] + 4, p["text"],
                        p["size"], True, CLOUD_COLORS[p["color_idx"]]))
        y = cloud_top + cloud_h
        ops.append(("line", MARGIN, y, width - MARGIN, y, 2, (58, 66, 82)))
        y += 20

    # 分组条目
    for g in groups:
        items = g.get("items") or []
        if not items:
            continue
        th = g.get("theme") or "其他"
        gt = g.get("tone") or {}
        head = th
        if gt:
            head += f"　利好{gt.get('利好', 0)}/利空{gt.get('利空', 0)}"
        ops.append(("rect", MARGIN, y + 6, MARGIN + 8, y + 40, ACCENT))
        ops.append(("text", MARGIN + 20, y, head, 34, True, ACCENT))
        y += 54
        for it in items:
            title = str(it.get("title") or "").strip()
            for i, seg in enumerate(wrap(title, inner - 24, measure_w, 31, max_lines=2)):
                ops.append(("text", MARGIN + 24, y, seg, 31, False, FG if i == 0 else DIM))
                y += 40
            meta = []
            if it.get("impact") is not None:
                meta.append(f"影响 {float(it['impact']):.2f}")
            if it.get("score") is not None:
                sc = float(it["score"])
                meta.append(f"情绪 {'+' if sc >= 0 else ''}{sc:.2f}")
            if it.get("fund"):          # A 股主力净流入：涨着但主力净流出=出货嫌疑
                meta.append(str(it["fund"]))
            if it.get("ev"):
                meta.append(str(it["ev"]))
            if it.get("source"):
                meta.append(str(it["source"]))
            if int(it.get("n_same") or 1) > 1:
                meta.append(f"×{it['n_same']}源")
            mtxt = " · ".join(meta)
            scol = float(it.get("score") or 0.0)
            ops.append(("text", MARGIN + 24, y, mtxt, 26, False,
                        DOWN if scol < 0 else (UP if scol > 0 else DIM)))
            y += 44
        y += 6

    # 页脚
    sup = payload.get("suppressed") or {}
    if sup:
        f = ("抑制：" + " · ".join(f"{k} {v}" for k, v in sup.items())
             + "（落选明细见 dashboard）")
        ops.append(("text", MARGIN, y, f, 26, False, DIM))
        y += 40

    height = int(y + MARGIN)
    img = Image.new("RGB", (width, height), BG)
    dr = ImageDraw.Draw(img)
    for op in ops:
        kind = op[0]
        if kind == "text":
            _x, _y, txt, size, bold, col = op[1:]
            dr.text((_x, _y), txt, font=font(size, bold), fill=col)
        elif kind == "line":
            dr.line((op[1], op[2], op[3], op[4]), width=op[5], fill=op[6])
        elif kind == "rect":
            dr.rectangle((op[1], op[2], op[3], op[4]), fill=op[5])
    tmp = out_path + ".tmp"
    img.save(tmp, "PNG", optimize=True)
    os.replace(tmp, out_path)
    size_b = os.path.getsize(out_path)
    return {"path": out_path, "bytes": size_b, "width": width, "height": height,
            "skipped_cloud": skipped}
