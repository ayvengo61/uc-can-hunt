# -*- coding: utf-8 -*-
"""Разбор строки спецификации поля (SPEC §2) — одно правило для canhunt.py и car.py.

Только стандартная библиотека: модуль импортирует и car.py (там нет numpy).
Монитор (C#, Field.Parse) реализует те же правила; общий список проверок — tests/spec_cases.txt.

    CH:ID:BYTE.BIT:LEN:ORDER[:s][*FACTOR][+OFFSET|-OFFSET]

Правила:
- пробелы по краям отбрасываются, внутри записи пробелов быть не должно;
- CH — десятичное 0..2; ID — hex (1..8 цифр, «0x» в начале допускается), не больше 1FFFFFFF;
  ID из 8 цифр или больше 7FF — 29-битный (иначе 11-битный);
- BYTE — 0..63, BIT — 0..7 (десятичные, без знака), LEN — 1..32;
- ORDER (le/be) обязателен, регистр любой; «:s» — тоже в любом регистре;
- FACTOR и OFFSET — конечные числа (десятичная запись, «.5», «5.», экспонента 1e-2);
- поле целиком помещается в 64 байта данных.
"""
import math
import re

_NUM = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
SPEC_RE = re.compile(
    r"^(?:(?P<ch>\d+):(?P<id>(?:0[xX])?[0-9A-Fa-f]+):)?"
    r"(?P<byte>\d+)\.(?P<bit>\d+):(?P<len>\d+):(?P<order>[lLbB][eE])"
    r"(?P<signed>:[sS])?"
    r"(?:\*(?P<factor>[-+]?" + _NUM + r"))?"
    r"(?P<offset>[-+]" + _NUM + r")?$")

FORMAT = "CH:ID:BYTE.BIT:LEN:le|be[:s][*FACTOR][+OFFSET]"


class SpecSyntaxError(ValueError):
    pass


def positions(byte, bit, length, order):
    """Позиции битов поля от МЛАДШЕГО бита значения к старшему (SPEC §2)."""
    p = byte * 8 + bit
    if order == "le":
        return [p + i for i in range(length)]
    seq = []
    q = p
    for _ in range(length):
        seq.append(q)
        q = q + 15 if q % 8 == 0 else q - 1
    seq.reverse()
    return seq


def _num(s, what):
    try:
        v = float(s)
    except ValueError:
        raise SpecSyntaxError("%s: не число «%s»" % (what, s))
    if not math.isfinite(v):
        raise SpecSyntaxError("%s: число вне диапазона «%s»" % (what, s))
    return v


def parse_parts(text, need_id=True):
    """Разобрать запись поля. Возвращает dict: ch, id, ext (None без CH:ID), byte, bit, length, order,
    signed, factor, offset. Ошибка — SpecSyntaxError с понятным текстом."""
    s = (text or "").strip()
    if not s:
        raise SpecSyntaxError("пустая спецификация поля (нужно %s)" % FORMAT)
    if re.search(r"\s", s):
        raise SpecSyntaxError("пробелы внутри спецификации поля: %r" % s)
    m = SPEC_RE.match(s)
    if not m:
        raise SpecSyntaxError("неверная спецификация поля: %r (нужно %s, например 0:0C9:1.7:16:be*0.25)" % (s, FORMAT))
    ch = cid = ext = None
    if m.group("id") is not None:
        ch = int(m.group("ch"))
        if ch > 2:
            raise SpecSyntaxError("канал должен быть 0..2: %s" % m.group("ch"))
        h = m.group("id")
        if h[:2].lower() == "0x":
            h = h[2:]
        if not 1 <= len(h) <= 8:
            raise SpecSyntaxError("ID: от 1 до 8 hex-цифр: %s" % m.group("id"))
        cid = int(h, 16)
        if cid > 0x1FFFFFFF:
            raise SpecSyntaxError("ID больше 29 бит: %s" % m.group("id"))
        ext = len(h) == 8 or cid > 0x7FF          # SPEC §2: 8 цифр или больше 7FF — 29-битный
    elif need_id:
        raise SpecSyntaxError("в спецификации нет CH:ID: %r" % s)
    byte, bit, ln = int(m.group("byte")), int(m.group("bit")), int(m.group("len"))
    if byte > 63:
        raise SpecSyntaxError("байт должен быть 0..63: %d" % byte)
    if bit > 7:
        raise SpecSyntaxError("бит должен быть 0..7: %d" % bit)
    if not 1 <= ln <= 32:
        raise SpecSyntaxError("длина поля должна быть 1..32 бит: %d" % ln)
    order = m.group("order").lower()
    pos = positions(byte, bit, ln, order)
    if min(pos) < 0 or max(pos) > 511:
        raise SpecSyntaxError("поле выходит за 64 байта данных: %r" % s)
    factor = _num(m.group("factor"), "FACTOR") if m.group("factor") else 1.0
    offset = _num(m.group("offset"), "OFFSET") if m.group("offset") else 0.0
    return {"ch": ch, "id": cid, "ext": ext, "byte": byte, "bit": bit, "length": ln, "order": order,
            "signed": bool(m.group("signed")), "factor": factor, "offset": offset}
