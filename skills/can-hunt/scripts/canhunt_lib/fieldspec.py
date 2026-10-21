# -*- coding: utf-8 -*-
"""Спецификация поля (SPEC §2) и векторный декодер.

    CH:ID:BYTE.BIT:LEN:ORDER[:s][*FACTOR][+OFFSET|-OFFSET]

le (Intel):     старт = младший бит значения, бит i значения — из позиции p+i.
be (Motorola):  старт = старший бит; дальше: если p%8==0 → p += 15, иначе p -= 1.
Позиция q → байт q//8, бит q%8 (0 — младший бит байта).
"""
import math

import numpy as np

from .specparse import SpecSyntaxError, parse_parts


class SpecError(ValueError):
    pass


def fmt_num(x):
    """Короткая запись числа без мусора: 0.25, 1, -40, 0.0078125."""
    if x is None:
        return ""
    x = float(x)
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    s = "%.10g" % x
    if "e" in s:
        s = ("%.12f" % x).rstrip("0").rstrip(".")
    return s


def fmt_id(can_id, ext=None):
    if ext is None:
        ext = can_id > 0x7FF
    return ("%08X" % can_id) if ext else ("%03X" % can_id)


class FieldSpec(object):
    """Поле кадра. ch/can_id могут быть None (для «голой» спецификации без канала и ID)."""
    __slots__ = ("ch", "can_id", "byte", "bit", "length", "order", "signed", "factor", "offset", "ext")

    def __init__(self, ch, can_id, byte, bit, length, order="le", signed=False, factor=1.0, offset=0.0, ext=None):
        self.ch = ch
        self.can_id = can_id
        self.byte = int(byte)
        self.bit = int(bit)
        self.length = int(length)
        self.order = order.lower()
        self.signed = bool(signed)
        self.factor = float(factor)
        self.offset = float(offset)
        self.ext = ext if ext is not None else (can_id is not None and can_id > 0x7FF)
        if not (0 <= self.bit <= 7):
            raise SpecError("бит должен быть 0..7: %d" % self.bit)
        if not (1 <= self.length <= 32):
            raise SpecError("длина поля должна быть 1..32 бит: %d" % self.length)
        if self.order not in ("le", "be"):
            raise SpecError("порядок должен быть le или be")
        if self.byte > 63:
            raise SpecError("байт должен быть 0..63")
        # поле должно помещаться в 64 байта
        pos = self.positions()
        if min(pos) < 0 or max(pos) > 511:
            raise SpecError("поле выходит за 64 байта данных")

    # --- геометрия ---------------------------------------------------------
    @property
    def start(self):
        return self.byte * 8 + self.bit

    def positions(self):
        """Позиции битов, от МЛАДШЕГО бита значения к старшему."""
        p = self.start
        n = self.length
        if self.order == "le":
            return [p + i for i in range(n)]
        seq = []
        q = p
        for _ in range(n):
            seq.append(q)
            if q % 8 == 0:
                q += 15
            else:
                q -= 1
        seq.reverse()
        return seq

    def bitset(self):
        return frozenset(self.positions())

    def need_bytes(self):
        """Минимальная длина данных кадра, при которой поле определено."""
        return max(self.positions()) // 8 + 1

    def firmware_equivalent(self):
        """Эквивалентная le-спецификация, которую понимает прошивка (или None).
        be-поле, целиком лежащее в одном байте, — это то же самое le-поле."""
        if self.order == "le":
            return self
        pos = self.positions()
        if len(set(q // 8 for q in pos)) == 1:
            lo = min(pos)
            return self.replace(byte=lo // 8, bit=lo % 8, order="le")
        return None

    def replace(self, **kw):
        d = dict(ch=self.ch, can_id=self.can_id, byte=self.byte, bit=self.bit, length=self.length,
                 order=self.order, signed=self.signed, factor=self.factor, offset=self.offset, ext=self.ext)
        d.update(kw)
        if "can_id" in kw and "ext" not in kw:
            d["ext"] = None
        return FieldSpec(**d)

    # --- строковое представление ----------------------------------------------
    def bits_str(self):
        s = "%d.%d:%d:%s" % (self.byte, self.bit, self.length, self.order)
        if self.signed:
            s += ":s"
        return s

    def scale_str(self):
        s = ""
        if self.factor != 1.0:
            s += "*" + fmt_num(self.factor)
        if self.offset != 0.0:
            s += ("+" if self.offset > 0 else "-") + fmt_num(abs(self.offset))
        return s

    def key_str(self):
        if self.can_id is None:
            return ""
        return "%d:%s" % (self.ch or 0, fmt_id(self.can_id, self.ext))

    def __str__(self):
        k = self.key_str()
        return (k + ":" if k else "") + self.bits_str() + self.scale_str()

    def raw_str(self):
        """Спецификация без масштаба."""
        k = self.key_str()
        return (k + ":" if k else "") + self.bits_str()

    __repr__ = __str__

    def raw_range(self):
        if self.signed:
            return -(1 << (self.length - 1)), (1 << (self.length - 1)) - 1
        return 0, (1 << self.length) - 1

    def phys(self, raw):
        return raw * self.factor + self.offset


def parse_spec(text, need_id=True):
    """Разбор строки спецификации (правила — canhunt_lib/specparse.py, общие с car.py и монитором).
    need_id=False разрешает «голую» форму BYTE.BIT:LEN:ORDER[:s][*F][+O]."""
    try:
        p = parse_parts(text, need_id)
    except SpecSyntaxError as e:
        raise SpecError(str(e))
    return FieldSpec(p["ch"], p["id"], p["byte"], p["bit"], p["length"], p["order"], p["signed"],
                     p["factor"], p["offset"], p["ext"])


def _runs(spec):
    """Разбить поле на куски «подряд идущие биты одного байта»:
    (байт, бит_в_байте, число_бит, сдвиг_в_значении)."""
    pos = spec.positions()
    runs = []
    i = 0
    n = len(pos)
    while i < n:
        q = pos[i]
        j = i + 1
        while j < n and pos[j] == pos[j - 1] + 1 and pos[j] // 8 == q // 8:
            j += 1
        runs.append((q // 8, q % 8, j - i, i))
        i = j
    return runs


def decode_raw(data, dlen, spec):
    """Векторный декод: data — uint8 N×L, dlen — длины кадров (N) или None.
    Возвращает (raw int64 N, valid bool N). Для невалидных кадров raw = 0."""
    data = np.asarray(data)
    n = data.shape[0]
    L = data.shape[1] if data.ndim == 2 else 0
    need = spec.need_bytes()
    if need > L:
        return np.zeros(n, np.int64), np.zeros(n, bool)
    raw = np.zeros(n, np.int64)
    for b, sb, k, sh in _runs(spec):
        col = data[:, b].astype(np.int64)
        if sb:
            col = col >> sb
        if k < 8:
            col = col & ((1 << k) - 1)
        if sh:
            col = col << sh
        raw |= col
    if spec.signed:
        half = 1 << (spec.length - 1)
        raw = np.where(raw >= half, raw - (1 << spec.length), raw)
    if dlen is None:
        valid = np.ones(n, bool)
    else:
        valid = np.asarray(dlen) >= need
        if not valid.all():
            raw = np.where(valid, raw, 0)
    return raw, valid


def decode_phys(data, dlen, spec):
    """Физическое значение (float64), NaN там, где поле не определено."""
    raw, valid = decode_raw(data, dlen, spec)
    v = raw.astype(np.float64) * spec.factor + spec.offset
    if not valid.all():
        v[~valid] = np.nan
    return v


def decode_bytes(payload, spec, phys=False):
    """Декод одного кадра (bytes или hex-строка). None — поле не определено."""
    if isinstance(payload, str):
        payload = bytes.fromhex(payload.replace(" ", ""))
    arr = np.frombuffer(bytes(payload), np.uint8).reshape(1, -1) if len(payload) else np.zeros((1, 0), np.uint8)
    raw, valid = decode_raw(arr, None, spec)
    if not valid[0]:
        return None
    r = int(raw[0])
    return spec.phys(r) if phys else r


def decode_naive(payload, spec):
    """Эталонная побитовая реализация прямо по тексту SPEC §2 (для тестов)."""
    data = bytes(payload)
    pos = spec.positions()  # от младшего к старшему
    if max(pos) // 8 >= len(data):
        return None
    v = 0
    for i, q in enumerate(pos):
        if (data[q // 8] >> (q % 8)) & 1:
            v |= 1 << i
    if spec.signed and v >= (1 << (spec.length - 1)):
        v -= 1 << spec.length
    return v


# ---------------------------------------------------------------------------
# «Красивые» числа для FACTOR/OFFSET
# ---------------------------------------------------------------------------
def nice_factors():
    s = set()
    for e in range(-6, 5):
        for m in (1, 2, 2.5, 4, 5):
            s.add(round(m * 10.0 ** e, 12))
    for n in range(0, 13):
        s.add(1.0 / (1 << n))
        s.add(float(1 << n))
    s.add(1.0 / 3.0)
    return sorted(s)


_NICE = nice_factors()


def nice_factor_candidates(a, tol=0.08):
    """Красивые множители в пределах ±tol от a (со знаком a), ближайшие первыми."""
    if a == 0 or not math.isfinite(a):
        return []
    sgn = 1.0 if a > 0 else -1.0
    aa = abs(a)
    c = [f for f in _NICE if abs(f / aa - 1.0) <= tol]
    c.sort(key=lambda f: abs(f / aa - 1.0))
    return [sgn * f for f in c]


def round_offset_candidates(b):
    """Смещения от «самых круглых» к точным: 0, ±1000, ±100, …"""
    out = [0.0]
    for step in (10000, 5000, 1000, 500, 250, 100, 50, 40, 25, 10, 5, 2, 1, 0.5, 0.25, 0.1, 0.05, 0.01):
        r = round(b / step) * step
        r = float(round(r, 6))
        if r not in out:
            out.append(r)
    return out
