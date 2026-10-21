# -*- coding: utf-8 -*-
"""Строка для прошивки UniversalController (CanRxSignal_t) и строка DBC SG_.

CanRxSignal_t (config/can_types.h): { can_id, start_byte, start_bit, bit_len, is_signed, factor, offset, name }.
Прошивка извлекает поле как bits_extract() — это le (Intel). be длиннее байта прошивка не читает.
Значение в прошивке хранится как int: rx_values[i] = (int)(raw*factor + offset) — дробная часть отбрасывается.
"""
import re

from .fieldspec import fmt_id, fmt_num

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya"}


def dbc_name(name):
    out = []
    for ch in name:
        lo = ch.lower()
        if lo in _TRANSLIT:
            t = _TRANSLIT[lo]
            out.append(t.upper() if ch != lo else t)
        else:
            out.append(ch)
    s = re.sub(r"[^A-Za-z0-9_]", "_", "".join(out)).strip("_") or "SIGNAL"
    if s[0].isdigit():
        s = "S_" + s
    return s[:32]


def c_float(x):
    s = fmt_num(x)
    if "." not in s and "e" not in s:
        s += ".0"
    return s + "f"


def _dlc_for(nbytes):
    for L in (8, 12, 16, 20, 24, 32, 48, 64):
        if nbytes <= L:
            return L
    return 64


def export_field(spec, name, unit="", dlc=None):
    """Возвращает dict: firmware (строка или None), dbc_bo, dbc_sg, warnings."""
    warn = []
    fw = spec.firmware_equivalent()
    ids = "0x%03X" % spec.can_id if not spec.ext else "0x%08X" % spec.can_id
    fw_line = None
    if fw is None:
        warn.append("поле big-endian (be) длиннее одного байта — прошивка UniversalController читает только "
                    "le (Intel, bits_extract), это поле ею сейчас не декодируется. Строку для прошивки не даю; "
                    "нужна поддержка be в прошивке или другое поле.")
    else:
        if fw is not spec and spec.order == "be":
            warn.append("be-поле внутри одного байта записано как эквивалентное le: %s" % fw.raw_str())
        cname = name.replace("\\", "\\\\").replace('"', '\\"')
        fw_line = '{  %s,  %d,  %d,  %d,  %d,  %s,  %s,  "%s" },' % (
            ids, fw.byte, fw.bit, fw.length, 1 if fw.signed else 0, c_float(spec.factor), c_float(spec.offset), cname)
        if spec.factor != int(spec.factor) or spec.offset != int(spec.offset):
            warn.append("прошивка хранит значение как int ((int)(raw*factor+offset)) — дробная часть отбрасывается")
        if spec.length > 32:
            warn.append("длина больше 32 бит — прошивка не поддерживает")
        if spec.ext and spec.can_id is not None and spec.can_id <= 0x7FF:
            warn.append("ID %08X — 29-битный, но не больше 7FF: прошивка UniversalController определяет тип ID по "
                        "значению (> 7FF — 29 бит) и будет ждать 11-битный кадр 0x%03X; такой кадр ею не принимается"
                        % (spec.can_id, spec.can_id))
    lo, hi = spec.raw_range()
    a, b = lo * spec.factor + spec.offset, hi * spec.factor + spec.offset
    vmin, vmax = min(a, b), max(a, b)
    dbc_id = spec.can_id | (0x80000000 if spec.ext else 0)
    if dlc is None:
        dlc = _dlc_for(spec.need_bytes())
    sname = dbc_name(name)
    bo = "BO_ %d ID_%s: %d Vector__XXX" % (dbc_id, fmt_id(spec.can_id, spec.ext), dlc)
    sg = ' SG_ %s : %d|%d@%d%s (%s,%s) [%s|%s] "%s" Vector__XXX' % (
        sname, spec.start, spec.length, 1 if spec.order == "le" else 0, "-" if spec.signed else "+",
        fmt_num(spec.factor), fmt_num(spec.offset), fmt_num(vmin), fmt_num(vmax), unit)
    return {"spec": str(spec), "name": name, "channel": spec.ch,
            "firmware": fw_line,
            "firmware_where": "config/can%d_config.h → can%d_rx_signals[] (и CAN%d_RX_SIGNAL_COUNT_CFG)" % (
                spec.ch or 0, spec.ch or 0, spec.ch or 0),
            "dbc_bo": bo, "dbc_sg": sg, "dbc_name": sname, "warnings": warn}
