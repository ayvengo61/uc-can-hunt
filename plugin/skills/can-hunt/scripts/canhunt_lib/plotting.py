# -*- coding: utf-8 -*-
"""PNG-графики через PIL (без matplotlib).

plot_fields — значения полей по времени (панель на поле), фазы закрашены и подписаны сверху,
              отметки — красные вертикали; оси с подписями.
plot_id     — «тепловая полоса» байтов одного ID: строка на байт, цвет = значение 0..255.
"""
import math
import os
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .fieldspec import decode_phys, fmt_num

BG = (255, 255, 255)
FG = (33, 37, 41)
GRID = (226, 229, 233)
AXIS = (120, 126, 134)
MARK = (214, 39, 40)
LINE_COLORS = [(31, 119, 180), (44, 160, 44), (148, 103, 189), (255, 127, 14), (23, 190, 207), (140, 86, 75)]
PHASE_COLORS = [(255, 224, 178), (200, 230, 201), (187, 222, 251), (248, 187, 208), (225, 190, 231),
                (255, 245, 157), (178, 235, 242), (215, 204, 200), (220, 237, 200), (255, 204, 188)]
VIRIDIS = [(68, 1, 84), (72, 40, 120), (62, 74, 137), (49, 104, 142), (38, 130, 142), (31, 158, 137),
           (53, 183, 121), (109, 205, 89), (180, 222, 44), (253, 231, 37)]

_FONT_CACHE = {}


def font(size, bold=False):
    k = (size, bold)
    f = _FONT_CACHE.get(k)
    if f is not None:
        return f
    windir = os.environ.get("WINDIR", r"C:\Windows")
    names = ["arialbd.ttf", "arial.ttf"] if bold else ["arial.ttf"]
    for n in names:
        for p in (os.path.join(windir, "Fonts", n), n):
            try:
                f = ImageFont.truetype(p, size)
                _FONT_CACHE[k] = f
                return f
            except (OSError, IOError):
                continue
    try:
        f = ImageFont.load_default(size=size)
    except TypeError:
        f = ImageFont.load_default()
    _FONT_CACHE[k] = f
    return f


def text_w(draw, s, f):
    try:
        b = draw.textbbox((0, 0), s, font=f)
        return b[2] - b[0]
    except AttributeError:
        return draw.textsize(s, font=f)[0]


def nice_ticks(lo, hi, n=6):
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return [], 1.0
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / max(1, n)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if step >= raw:
            break
    start = math.ceil(lo / step) * step
    ticks = []
    v = start
    while v <= hi + step * 1e-9:
        ticks.append(round(v, 10))
        v += step
    return ticks, step


def fmt_tick(v, step):
    if step >= 1:
        return fmt_num(round(v))
    dec = max(0, -int(math.floor(math.log10(step))) + (1 if step * 10 ** -math.floor(math.log10(step)) == 2.5 else 0))
    return ("%%.%df" % dec) % v


def phase_colors(sess):
    labs = sess.phase_labels()
    return {lab: PHASE_COLORS[i % len(PHASE_COLORS)] for i, lab in enumerate(labs)}


def _safe(s):
    return re.sub(r"[^0-9A-Za-z_.+-]+", "-", s).strip("-")[:80]


def default_out(sess, name):
    d = os.path.join(sess.dir or ".", "plots")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name + ".png")


def _draw_phase_band(draw, sess, x_of, x0, x1, y0, y1, colors, f):
    """Полоса с подписями фаз над графиками."""
    draw.rectangle([x0, y0, x1, y1], fill=(248, 249, 250), outline=GRID)
    for iv in sess.phases:
        a, b = x_of(iv.t0), x_of(iv.t1)
        a, b = max(a, x0), min(b, x1)
        if b <= a:
            continue
        c = colors.get(iv.label, PHASE_COLORS[0])
        draw.rectangle([a, y0 + 1, b, y1 - 1], fill=c, outline=tuple(max(0, v - 60) for v in c))
        lab = iv.label
        w = text_w(draw, lab, f)
        while w > (b - a - 4) and len(lab) > 1:
            lab = lab[:-1]
            w = text_w(draw, lab + "…", f)
        if lab != iv.label:
            lab = lab + "…" if len(lab) > 0 else ""
        if b - a >= 8 and lab:
            draw.text((a + (b - a - text_w(draw, lab, f)) / 2, y0 + (y1 - y0 - f.size) / 2 - 1), lab, fill=FG, font=f)


def _draw_marks(draw, sess, x_of, x0, x1, y0, y1, dashed=True):
    for m in sess.marks:
        x = x_of(m["t"])
        if x < x0 or x > x1:
            continue
        if dashed:
            y = y0
            while y < y1:
                draw.line([x, y, x, min(y1, y + 6)], fill=MARK, width=2)
                y += 10
        else:
            draw.line([x, y0, x, y1], fill=MARK, width=2)


def _time_axis(draw, sess, x_of, x0, x1, y, t0, t1, f, grid_top=None):
    ticks, step = nice_ticks(t0 - sess.t0, t1 - sess.t0, max(4, int((x1 - x0) / 110)))
    for tv in ticks:
        x = x_of(tv + sess.t0)
        if x < x0 - 1 or x > x1 + 1:
            continue
        if grid_top is not None:
            for gt, gb in grid_top:
                draw.line([x, gt, x, gb], fill=GRID)
        draw.line([x, y, x, y + 5], fill=AXIS)
        s = fmt_tick(tv, step)
        draw.text((x - text_w(draw, s, f) / 2, y + 7), s, fill=FG, font=f)
    lab = "время, с (от начала записи)"
    draw.text(((x0 + x1) / 2 - text_w(draw, lab, f) / 2, y + 7 + f.size + 4), lab, fill=AXIS, font=f)


def _series_pixels(t, v, x_of, y_of, x0, x1, max_gap):
    """Сегменты ломаной с прореживанием (min/max по пиксельным столбцам)."""
    ok = ~np.isnan(v)
    t, v = t[ok], v[ok]
    if len(t) == 0:
        return []
    xs = x_of(t)
    segs = []
    # разрывы по времени
    br = np.nonzero(np.diff(t) > max_gap)[0] + 1
    starts = np.concatenate([[0], br])
    ends = np.concatenate([br, [len(t)]])
    for a, b in zip(starts, ends):
        xx = xs[a:b]
        vv = v[a:b]
        if b - a <= 3 * (x1 - x0):
            pts = [(float(x), float(y_of(y))) for x, y in zip(xx, vv)]
            segs.append(("line", pts))
            continue
        col = np.floor(xx).astype(np.int64)
        cut = np.nonzero(np.diff(col))[0] + 1
        cs = np.concatenate([[0], cut])
        ce = np.concatenate([cut, [len(col)]])
        mn = np.minimum.reduceat(vv, cs)
        mx = np.maximum.reduceat(vv, cs)
        first = vv[cs]
        last = vv[ce - 1]
        cols = col[cs]
        pts = []
        prev = None
        for c, lo, hi, fi, la in zip(cols, mn, mx, first, last):
            if prev is not None:
                pts.append(("l", (prev[0], y_of(prev[1]), c, y_of(fi))))
            pts.append(("l", (c, y_of(lo), c, y_of(hi))))
            prev = (c, la)
        segs.append(("cols", pts))
    return segs


def plot_fields(sess, items, out_path, width=1400, title=None):
    """items — список (spec, name). Возвращает путь к PNG."""
    colors = phase_colors(sess)
    n = len(items)
    f_small, f_norm, f_bold = font(12), font(13), font(15, True)
    left, right = 92, 24
    title_h, band_h, gap, bottom = 34, 26, 16, 58
    panel_h = 300 if n == 1 else (250 if n == 2 else 200)
    H = title_h + band_h + 6 + n * panel_h + (n - 1) * gap + bottom
    img = Image.new("RGB", (width, H), BG)
    dr = ImageDraw.Draw(img)
    x0, x1 = left, width - right
    t0, t1 = sess.t0, max(sess.t_end, sess.t0 + 1e-3)

    def x_of(t):
        return x0 + (np.asarray(t, dtype=np.float64) - t0) / (t1 - t0) * (x1 - x0)
    ttl = title or ("Поля: " + ", ".join(str(sp) for sp, _ in items))
    dr.text((x0, 8), ttl[:160], fill=FG, font=f_bold)
    _draw_phase_band(dr, sess, lambda t: float(x_of(t)), x0, x1, title_h, title_h + band_h, colors, f_small)
    grid_spans = []
    py = title_h + band_h + 6
    panels = []
    for k, (spec, name) in enumerate(items):
        s = sess.stream(spec.ch, spec.can_id, spec.ext)
        top = py + k * (panel_h + gap)
        bot = top + panel_h
        panels.append((top, bot))
        grid_spans.append((top, bot))
        dr.rectangle([x0, top, x1, bot], fill=BG, outline=AXIS)
        # фазы — заливка
        for iv in sess.phases:
            a, b = float(x_of(iv.t0)), float(x_of(iv.t1))
            a, b = max(a, x0 + 1), min(b, x1 - 1)
            if b > a:
                c = colors.get(iv.label, PHASE_COLORS[0])
                light = tuple(int(255 - (255 - v) * 0.45) for v in c)
                dr.rectangle([a, top + 1, b, bot - 1], fill=light)
        if s is None or s.n == 0:
            dr.text((x0 + 10, top + 10), "%s — нет кадров" % spec, fill=MARK, font=f_norm)
            continue
        v = decode_phys(s.data, s.dlen, spec)
        t = s.t
        ok = ~np.isnan(v)
        if not ok.any():
            dr.text((x0 + 10, top + 10), "%s — поле вне данных кадра" % spec, fill=MARK, font=f_norm)
            continue
        lo, hi = float(np.nanmin(v)), float(np.nanmax(v))
        if hi == lo:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.07
        lo, hi = lo - pad, hi + pad
        ticks, step = nice_ticks(lo, hi, max(3, panel_h // 45))

        def y_of(y, top=top, bot=bot, lo=lo, hi=hi):
            # сверху оставлено место под подпись панели
            return bot - 6 - (y - lo) / (hi - lo) * (bot - top - 34)
        for tv in ticks:
            y = y_of(tv)
            if y < top or y > bot:
                continue
            dr.line([x0 + 1, y, x1 - 1, y], fill=GRID)
            s_ = fmt_tick(tv, step)
            dr.line([x0 - 5, y, x0, y], fill=AXIS)
            dr.text((x0 - 8 - text_w(dr, s_, f_small), y - f_small.size / 2 - 1), s_, fill=FG, font=f_small)
        per = s.period() or 0.1
        col = LINE_COLORS[k % len(LINE_COLORS)]
        for kind, pts in _series_pixels(t, v, x_of, y_of, x0, x1, max(1.0, 5 * per)):
            if kind == "line":
                if len(pts) == 1:
                    x, y = pts[0]
                    dr.ellipse([x - 2, y - 2, x + 2, y + 2], fill=col)
                else:
                    dr.line(pts, fill=col, width=2)
            else:
                for _, (xa, ya, xb, yb) in pts:
                    dr.line([xa, ya, xb, yb], fill=col, width=2)
        _draw_marks(dr, sess, lambda tt: float(x_of(tt)), x0, x1, top + 1, bot - 1)
        lab = "%s  %s" % (name or "", spec) if name else str(spec)
        lab += "   [%s … %s]" % (fmt_num(round(float(np.nanmin(v)), 4)), fmt_num(round(float(np.nanmax(v)), 4)))
        tw = text_w(dr, lab, f_norm)
        dr.rectangle([x0 + 6, top + 5, x0 + 14 + tw, top + 9 + f_norm.size + 2], fill=(255, 255, 255))
        dr.text((x0 + 10, top + 6), lab, fill=col, font=f_norm)
    last_bot = panels[-1][1] if panels else py
    _time_axis(dr, sess, lambda tt: float(x_of(tt)), x0, x1, last_bot, t0, t1, f_small, grid_top=None)
    # отметки: подписи в полосе фаз
    for m in sess.marks:
        x = float(x_of(m["t"]))
        if x0 <= x <= x1:
            dr.polygon([(x - 5, title_h - 2), (x + 5, title_h - 2), (x, title_h + 6)], fill=MARK)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path)
    return out_path


def _viridis_lut():
    lut = np.zeros((256, 3), np.uint8)
    anchors = np.array(VIRIDIS, np.float64)
    pos = np.linspace(0, 255, len(anchors))
    for c in range(3):
        lut[:, c] = np.round(np.interp(np.arange(256), pos, anchors[:, c])).astype(np.uint8)
    return lut


def plot_id(sess, s, out_path, width=1400, codes=None, absolute=False):
    """Тепловая полоса байтов потока s. По умолчанию у каждой строки своя шкала (мин..макс байта),
    постоянные байты — серые; absolute=True — общая шкала 00..FF."""
    colors = phase_colors(sess)
    L = max(1, s.L)
    f_small, f_bold = font(12), font(15, True)
    left, right = 52, 118
    title_h, band_h, bottom = 34, 26, 90
    row_h = 20 if L <= 16 else (12 if L <= 32 else 9)
    strip_top = title_h + band_h + 8
    H = strip_top + L * row_h + bottom
    img = Image.new("RGB", (width, H), BG)
    dr = ImageDraw.Draw(img)
    x0, x1 = left, width - right
    W = x1 - x0
    t0, t1 = sess.t0, max(sess.t_end, sess.t0 + 1e-3)

    def x_of(t):
        return x0 + (np.asarray(t, dtype=np.float64) - t0) / (t1 - t0) * W
    rate = s.rate()
    dr.text((x0, 8), "%s — тепловая полоса байтов (%d кадров, %.1f Гц, %d байт)" % (s.name, s.n, rate, s.L),
            fill=FG, font=f_bold)
    _draw_phase_band(dr, sess, lambda t: float(x_of(t)), x0, x1, title_h, title_h + band_h, colors, f_small)
    # значения по столбцам: последний кадр в столбце, пустые — перенос предыдущего (если разрыв < 1 с)
    grid_t = t0 + (np.arange(W) + 1.0) / W * (t1 - t0)
    idx = np.searchsorted(s.t, grid_t, "right") - 1
    valid = idx >= 0
    idxc = np.clip(idx, 0, max(0, s.n - 1))
    per = s.period() or 0.1
    stale = (grid_t - s.t[idxc]) > max(1.0, 5 * per)
    valid &= ~stale
    lut = _viridis_lut()
    arr = np.full((L * row_h, W, 3), 235, np.uint8)
    ranges = []
    for b in range(L):
        vals = s.data[idxc, b] if s.n else np.zeros(W, np.uint8)
        okb = valid & (s.dlen[idxc] > b) if s.n else np.zeros(W, bool)
        colv = s.data[s.dlen > b, b] if s.n else np.zeros(0, np.uint8)
        vmin = int(colv.min()) if len(colv) else 0
        vmax = int(colv.max()) if len(colv) else 0
        ranges.append((vmin, vmax))
        if absolute:
            rgb = lut[vals]
        elif vmax > vmin:
            k = np.round((vals.astype(np.float64) - vmin) * 255.0 / (vmax - vmin)).astype(np.int64)
            rgb = lut[np.clip(k, 0, 255)]
        else:
            rgb = np.tile(np.array([[214, 217, 222]], np.uint8), (W, 1))
        rgb[~okb] = (240, 240, 240)
        arr[b * row_h:(b + 1) * row_h - (1 if row_h >= 12 else 0), :, :] = rgb[None, :, :]
    img.paste(Image.fromarray(arr, "RGB"), (x0, strip_top))
    dr.rectangle([x0 - 1, strip_top - 1, x1, strip_top + L * row_h], outline=AXIS)
    step_lab = 1 if row_h >= 12 else 2
    for b in range(L):
        y = strip_top + b * row_h + (row_h - f_small.size) / 2 - 1
        if b % step_lab == 0:
            lab = "Б%d" % b
            dr.text((x0 - 6 - text_w(dr, lab, f_small), y), lab, fill=FG, font=f_small)
        if b % step_lab == 0:
            vmin, vmax = ranges[b]
            code = codes[b] if codes and b < len(codes) else ""
            if vmin == vmax:
                txt = "=%02X" % vmin
            else:
                txt = "%s %02X..%02X" % (code, vmin, vmax)
            dr.text((x1 + 6, y), txt, fill=FG, font=f_small)
    _draw_marks(dr, sess, lambda tt: float(x_of(tt)), x0, x1, strip_top, strip_top + L * row_h, dashed=False)
    for m in sess.marks:
        x = float(x_of(m["t"]))
        if x0 <= x <= x1:
            dr.polygon([(x - 5, title_h - 2), (x + 5, title_h - 2), (x, title_h + 6)], fill=MARK)
    ybot = strip_top + L * row_h
    _time_axis(dr, sess, lambda tt: float(x_of(tt)), x0, x1, ybot, t0, t1, f_small)
    # шкала цвета
    cb_w, cb_h = 256, 12
    cbx, cby = x1 - cb_w, H - 26
    bar = np.repeat(lut[None, :, :], cb_h, axis=0)
    img.paste(Image.fromarray(bar, "RGB"), (cbx, cby))
    dr.rectangle([cbx - 1, cby - 1, cbx + cb_w, cby + cb_h], outline=AXIS)
    marks_ = [(0, "00"), (64, "40"), (128, "80"), (192, "C0"), (255, "FF")] if absolute else [(0, "мин"), (255, "макс")]
    for v, lab in marks_:
        dr.text((cbx + v - text_w(dr, lab, f_small) / 2, cby - f_small.size - 3), lab, fill=FG, font=f_small)
    lab = "значение байта:" if absolute else "цвет: от мин до макс своего байта (серый — постоянный):"
    dr.text((cbx - 10 - text_w(dr, lab, f_small), cby - 1), lab, fill=FG, font=f_small)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path)
    return out_path
