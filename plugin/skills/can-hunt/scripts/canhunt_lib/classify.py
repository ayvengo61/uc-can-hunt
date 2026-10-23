# -*- coding: utf-8 -*-
"""Классификация байтов потока: константа / счётчик / контрольная сумма / шум / сигнал / флаги.

Счётчик — значение, растущее (или убывающее) на постоянный шаг почти в каждом кадре по модулю
2^k (байт или тетрада) либо с переходом max→min (счётчики 0..14 и т. п.); допускаются пропуски кадров.
Контрольная сумма — проверяется гипотезами: XOR всех байт кадра = const, сумма, CRC-8 (набор полиномов;
init/xorout/DataID в начале сводятся к константе, потому что CRC линейна).
Непроверенный байт, который меняется почти в каждом кадре и имеет высокую энтропию, помечается
«шум/CRC?» — если это не младший байт плавного 16-битного сигнала (проверяется отдельно).
"""
import numpy as np

KIND_RU = {"const": "константа", "counter": "счётчик", "checksum": "контрольная сумма",
           "noise": "шум/CRC?", "signal": "сигнал", "flags": "флаги", "none": "нет данных"}


def _mode(a):
    if len(a) == 0:
        return None, 0.0
    v, c = np.unique(a, return_counts=True)
    i = int(np.argmax(c))
    return int(v[i]), float(c[i]) / len(a)


def counter_info(v, M):
    """Проверка «счётчик по модулю M» для последовательности v (подряд идущие кадры)."""
    n = len(v)
    if n < 12:
        return None
    v = v.astype(np.int64)
    d = (v[1:] - v[:-1]) % M
    nzm = d != 0
    if nzm.sum() < 0.6 * len(d):       # счётчик меняется почти в каждом кадре — быстрый отсев
        return None
    cnt = np.bincount(v, minlength=M)
    vals = np.nonzero(cnt)[0]
    if len(vals) < 4:
        return None
    nz = d[nzm]
    sc = np.bincount(nz, minlength=M)
    step = int(np.argmax(sc))
    vmin, vmax = int(vals[0]), int(vals[-1])
    if step <= M // 2:
        wrap = (v[:-1] == vmax) & (v[1:] == vmin)
    else:  # убывающий
        wrap = (v[:-1] == vmin) & (v[1:] == vmax)
    exact = (d == step) | wrap
    loose = exact | (d == (2 * step) % M) | (d == (3 * step) % M)
    fe = float(exact.mean())
    fl = float(loose.mean())
    if fe >= 0.6 and fl >= 0.9:
        s = step if step <= M // 2 else step - M
        return {"step": int(s), "min": vmin, "max": vmax, "frac": round(fl, 3), "mod": M}
    return None


# --- CRC-8 --------------------------------------------------------------------
def _crc_table(poly, reflected):
    tab = np.zeros(256, np.uint8)
    for i in range(256):
        c = i
        for _ in range(8):
            if reflected:
                c = (c >> 1) ^ poly if c & 1 else c >> 1
            else:
                c = ((c << 1) ^ poly) & 0xFF if c & 0x80 else (c << 1) & 0xFF
        tab[i] = c
    return tab


_CRC_DEFS = [(0x1D, False, "CRC-8 SAE J1850 (0x1D)"), (0x2F, False, "CRC-8H2F AUTOSAR (0x2F)"),
             (0x07, False, "CRC-8 (0x07)"), (0x31, False, "CRC-8 (0x31)"), (0x9B, False, "CRC-8 (0x9B)"),
             (0xD5, False, "CRC-8 (0xD5)"), (0x39, False, "CRC-8 (0x39)"), (0x49, False, "CRC-8 (0x49)"),
             (0x8C, True, "CRC-8/MAXIM (0x31 отр.)"), (0xE0, True, "CRC-8 (0x07 отр.)"),
             (0xB8, True, "CRC-8 (0x1D отр.)"), (0xD9, True, "CRC-8 (0x9B отр.)")]
_CRC_TABLES = None


def _tables():
    global _CRC_TABLES
    if _CRC_TABLES is None:
        _CRC_TABLES = [(_crc_table(p, r), name) for p, r, name in _CRC_DEFS]
    return _CRC_TABLES


def crc8(rows, poly=0x1D, init=0xFF, xorout=0xFF):
    """CRC-8 (MSB-first) по строкам uint8 N×K — для генераторов тестов."""
    tab = _crc_table(poly, False)
    crc = np.full(rows.shape[0], init, np.uint8)
    for j in range(rows.shape[1]):
        crc = tab[crc ^ rows[:, j]]
    return crc ^ np.uint8(xorout)


def _ranges(idx):
    if not idx:
        return ""
    out = []
    a = p = idx[0]
    for i in idx[1:]:
        if i == p + 1:
            p = i
            continue
        out.append("%d" % a if a == p else "%d..%d" % (a, p))
        a = p = i
    out.append("%d" % a if a == p else "%d..%d" % (a, p))
    return ",".join(out)


def checksum_info(D, b):
    """Сумма и CRC-8 для байта b (XOR проверяется на уровне кадра отдельно).
    Области: все остальные байты; байты до b (так в CAN FD Stellantis: CRC-8 J1850 по 0..b-1);
    байты после b."""
    L = D.shape[1]
    if L < 2 or D.shape[0] < 20:
        return None
    D = D[:2000]
    v = D[:, b]
    areas = [[j for j in range(L) if j != b]]
    if b >= 2 and b < L - 1:
        areas.append(list(range(0, b)))
    if L - b - 1 >= 2 and b > 0:
        areas.append(list(range(b + 1, L)))
    for cols in areas:
        s = (D[:, cols].astype(np.int64).sum(axis=1)) & 0xFF
        k, f = _mode((v.astype(np.int64) - s) & 0xFF)
        if f >= 0.97:
            return {"type": "sum", "const": k, "bytes": cols,
                    "text": "контр. сумма: сумма байт %s +0x%02X (mod 256)" % (_ranges(cols), k)}
    for tab, name in _tables():
        for cols in areas:
            crc = np.zeros(D.shape[0], np.uint8)
            for j in cols:
                crc = tab[crc ^ D[:, j]]
            k, f = _mode(v ^ crc)
            if f >= 0.97:
                return {"type": "crc8", "const": k, "bytes": cols,
                        "text": "контр. сумма: %s по байтам %s, ^0x%02X" % (name, _ranges(cols), k)}
    return None


def _entropy_norm(v, n, cnt=None):
    if cnt is None:
        cnt = np.bincount(v.astype(np.int64), minlength=256)
    p = cnt[cnt > 0] / float(len(v))
    h = float(-(p * np.log2(p)).sum())
    hmax = np.log2(min(256, max(2, n)))
    return h / hmax


def _smooth_lsb(D, b, kinds):
    """Байт b — младший байт плавного 16-битного сигнала? Возвращает ('be'|'le', байт-пара) или None."""
    L = D.shape[1]
    lo = D[:, b].astype(np.int64)
    best = None
    for order, hb in (("be", b - 1), ("le", b + 1)):
        if hb < 0 or hb >= L or kinds.get(hb) in ("counter", "checksum", "const"):
            continue
        V = D[:, hb].astype(np.int64) * 256 + lo
        md = float(np.median(np.abs(np.diff(V))))
        if md < 20:
            if best is None or md < best[2]:
                best = (order, hb, md)
    return best


def _popcount8(x):
    return bin(int(x) & 0xFF).count("1")


def classify_stream(s, max_rows=30000):
    """Классификация всех байтов потока. Возвращает dict:
    bytes — описания по байтам; hard_excl — позиции бит счётчиков и контрольных сумм;
    noise_bits — позиции бит «шумных» байтов; mask — маски меняющихся бит; xor — XOR-сумма кадра."""
    L = s.L
    res = {"bytes": [], "hard_excl": set(), "noise_bits": set(), "mask": [0] * L}
    if s.n == 0 or L == 0:
        return res
    Lm = s.len_mode()
    full = s.dlen == Lm
    D_all = s.data[full, :Lm] if not full.all() else s.data[:, :Lm]
    n_all = D_all.shape[0]
    if n_all > max_rows:
        a = (n_all - max_rows) // 2
        D = D_all[a:a + max_rows]
    else:
        D = D_all
    n = D.shape[0]
    chk_rows = D[:min(n, 6000)]
    cnt_a = max(0, (n - 5000) // 2)
    first = s.data[0]
    mask_all = np.bitwise_or.reduce(s.data ^ first, axis=0) if s.n > 1 else np.zeros(L, np.uint8)
    infos = []
    kinds = {}
    # --- проход 1: константы, счётчики, статистика
    for b in range(L):
        info = {"i": b, "kind": "none", "code": "--", "text": "", "value": None,
                "chg": 0.0, "distinct": 0, "entropy": 0.0, "mask": int(mask_all[b]), "excl_mask": 0}
        res["mask"][b] = int(mask_all[b])
        infos.append(info)
        if b >= Lm or n < 2:
            vv = s.data[s.dlen > b, b]
            if len(vv) and mask_all[b] == 0:
                info.update(kind="const", code="%02X" % int(vv[0]), value=int(vv[0]), text="=%02X" % int(vv[0]))
            kinds[b] = info["kind"]
            continue
        v = D[:, b]
        if mask_all[b] == 0:
            info.update(kind="const", code="%02X" % int(v[0]), value=int(v[0]), text="=%02X" % int(v[0]))
            kinds[b] = "const"
            continue
        chg = float(np.mean(v[1:] != v[:-1]))
        cnt = np.bincount(v, minlength=256)
        distinct = int(np.count_nonzero(cnt))
        ent = _entropy_norm(v, n, cnt)
        info.update(chg=round(chg, 3), distinct=distinct, entropy=round(ent, 3))
        lo = v & 0x0F
        hi = v >> 4
        lo_const = bool(np.all(lo == lo[0]))
        hi_const = bool(np.all(hi == hi[0]))
        vc = v[cnt_a:cnt_a + 5000]            # счётчик виден и на 5000 подряд идущих кадрах
        c8 = counter_info(vc, 256)
        clo = None if lo_const else counter_info(vc & 0x0F, 16)
        chi = None if hi_const else counter_info(vc >> 4, 16)
        # тетрада-счётчик важнее «байтового» счётчика, если байт на участке проверки гуляет в пределах 16
        # значений (иначе флаг во второй тетраде пропал бы вместе со счётчиком)
        lo_nib = clo is not None and (c8 is None or c8["max"] - c8["min"] < 16)
        hi_nib = chi is not None and (c8 is None or lo_const or abs(c8["step"]) % 16 == 0)
        if lo_nib and hi_const:
            info.update(kind="counter", code="с4",
                        text="мл. тетрада — счётчик %d..%d шаг %+d; ст. тетрада =%X" % (clo["min"], clo["max"], clo["step"], int(hi[0])),
                        excl_mask=0x0F, counter=clo, nibble="lo")
        elif hi_nib and lo_const:
            info.update(kind="counter", code="с4",
                        text="ст. тетрада — счётчик %d..%d шаг %+d; мл. тетрада =%X" % (chi["min"], chi["max"], chi["step"], int(lo[0])),
                        excl_mask=0xF0, counter=chi, nibble="hi")
        elif lo_nib or hi_nib:
            c = clo if lo_nib else chi
            info.update(kind="counter", code="с4+",
                        text="%s тетрада — счётчик %d..%d шаг %+d; %s тетрада меняется (биты %s не исключаются)" % (
                            "мл." if lo_nib else "ст.", c["min"], c["max"], c["step"], "ст." if lo_nib else "мл.",
                            "4-7" if lo_nib else "0-3"),
                        excl_mask=0x0F if lo_nib else 0xF0, counter=c, nibble="lo" if lo_nib else "hi")
        elif c8:
            info.update(kind="counter", code="сч",
                        text="счётчик %d..%d шаг %+d" % (c8["min"], c8["max"], c8["step"]),
                        excl_mask=0xFF, counter=c8)
        kinds[b] = info["kind"]
    # --- проход 2: XOR всего кадра = const → один байт — XOR-сумма
    if n >= 20 and Lm >= 2:
        X = np.bitwise_xor.reduce(chk_rows, axis=1)
        k, f = _mode(X)
        if f >= 0.97:
            # соотношение XOR симметрично (любой байт = XOR остальных), поэтому выбираем самый
            # «суммоподобный»: не чистый счётчик, чаще всех меняется, лучше последний или первый
            cand = [b for b in range(Lm) if kinds.get(b) != "const" and infos[b]["chg"] >= 0.3]
            if cand:
                def cnt_rank(b):
                    c = infos[b].get("counter")
                    if c is None:
                        return 2
                    return 0 if (infos[b]["code"] in ("сч", "с4") and c["frac"] >= 0.95) else 1

                def pref(b):
                    # меняется почти всегда → дальше решает место: последний байт, затем первый
                    return (cnt_rank(b), min(infos[b]["chg"], 0.9), 2 if b == Lm - 1 else (1 if b == 0 else 0),
                            infos[b]["chg"])
                xb = max(cand, key=pref)
                others = [j for j in range(Lm) if j != xb]
                infos[xb].update(kind="checksum", code="кс", excl_mask=0xFF,
                                 text="контр. сумма: XOR байт %s ^0x%02X" % (_ranges(others), k),
                                 checksum={"type": "xor", "const": k})
                kinds[xb] = "checksum"
    # --- проход 3: сумма / CRC-8, шум, сигнал, флаги
    for b in range(min(L, Lm)):
        info = infos[b]
        if info["kind"] != "none":
            continue
        chg, distinct, ent = info["chg"], info["distinct"], info["entropy"]
        if chg >= 0.5 and distinct >= 16:
            ck = checksum_info(chk_rows, b)
            if ck:
                info.update(kind="checksum", code="кс", text=ck["text"], excl_mask=0xFF, checksum=ck)
                kinds[b] = "checksum"
                continue
        if chg >= 0.85 and distinct >= 32 and ent >= 0.75:
            sm = _smooth_lsb(D, b, kinds)
            if sm:
                info.update(kind="signal", code="сг", lsb_of=sm[1],
                            text="младший байт плавного 16-бит сигнала (%s, байты %d и %d)" % (sm[0], min(b, sm[1]), max(b, sm[1])))
            else:
                info.update(kind="noise", code="ш?",
                            text="меняется в %d%% кадров, энтропия %.2f — CRC/шум или младший байт шумного сигнала"
                            % (round(chg * 100), ent))
            kinds[b] = info["kind"]
            continue
        nb = _popcount8(mask_all[b])
        v = D[:, b]
        vals = np.nonzero(np.bincount(v, minlength=256))[0]
        contiguous = distinct >= 3 and int(vals[-1]) - int(vals[0]) + 1 == distinct
        if nb <= 3 and distinct <= 8 and not contiguous:
            info.update(kind="flags", code="фл", text="флаги: меняются биты %s" % ",".join(
                str(k) for k in range(8) if (int(mask_all[b]) >> k) & 1))
        else:
            info.update(kind="signal", code="сг", text="сигнал: %d значений, %d..%d" % (distinct, int(v.min()), int(v.max())))
        kinds[b] = info["kind"]
    # старший байт при найденном «младшем байте плавного сигнала»
    for info in infos:
        sm = info.get("lsb_of")
        if sm is not None and infos[sm]["kind"] in ("flags", "signal"):
            infos[sm].update(kind="signal", code="сг", text="старший байт 16-бит сигнала (младший — б%d)" % info["i"])
    for info in infos:
        b = info["i"]
        for k in range(8):
            if (info["excl_mask"] >> k) & 1:
                res["hard_excl"].add(b * 8 + k)
            if info["kind"] == "noise":
                res["noise_bits"].add(b * 8 + k)
    res["bytes"] = infos
    return res
