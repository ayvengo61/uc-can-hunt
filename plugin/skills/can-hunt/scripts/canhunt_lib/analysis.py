# -*- coding: utf-8 -*-
"""Анализ сессии: compare / marks / track / corr / decode (SPEC §10)."""
import math

import numpy as np

from .fieldspec import (FieldSpec, decode_phys, decode_raw, fmt_num, nice_factor_candidates,
                        round_offset_candidates)


class AnalysisError(Exception):
    pass


# ---------------------------------------------------------------------------
# общие помощники
# ---------------------------------------------------------------------------
SKIP_MAX_FRAC = 0.5          # начало фазы отбрасывается, но не больше половины фазы (фактическое — в выводе)


def interval_rows(t, a, b, skip=0.0):
    """Индексы кадров [i0, i1) внутри интервала (a + skip, b)."""
    i0 = int(np.searchsorted(t, a + skip, "left"))
    i1 = int(np.searchsorted(t, b, "left"))
    return i0, max(i0, i1)


def eff_skip(iv, skip):
    """Сколько секунд начала фазы отбросить: skip, но не больше половины фазы."""
    return min(max(0.0, skip), SKIP_MAX_FRAC * max(0.0, iv.t1 - iv.t0))


def skip_report(groups, skip):
    """Фактически отброшенное начало по каждому отрезку фаз: [{"phase","duration","skipped","capped"}]."""
    out = []
    for g in groups:
        for iv in g[2]:
            d = max(0.0, iv.t1 - iv.t0)
            se = eff_skip(iv, skip)
            out.append({"phase": g[0], "duration": round(d, 3), "skipped": round(se, 3),
                        "capped": bool(se < skip - 1e-9)})
    return out


def subsample(i0, i1, k):
    n = i1 - i0
    if n <= k:
        return np.arange(i0, i1)
    return np.round(np.linspace(i0, i1 - 1, k)).astype(np.int64)


def field_series(sess, spec):
    s = sess.stream(spec.ch, spec.can_id, spec.ext)
    if s is None or s.n == 0:
        other = sess.stream(spec.ch, spec.can_id, not spec.ext)
        if other is not None and other.n:
            raise AnalysisError("в сессии нет кадров %s, но есть %s — %s-битный ID с тем же числом; в spec пишите ID %s"
                                % (spec.key_str(), other.name, "29" if other.ext else "11",
                                   "8 цифрами (29 бит)" if other.ext else "3 цифрами (11 бит)"))
        raise AnalysisError("в сессии нет кадров %s" % spec.key_str())
    v = decode_phys(s.data, s.dlen, spec)
    return s, s.t, v


def excluded_bits(sess, s, noise=True):
    c = sess.classification(s)
    ex = set(c["hard_excl"])
    if noise:
        ex |= c["noise_bits"]
    return ex


def const_bits(sess, s):
    """Позиции бит, которые не меняются во всей сессии."""
    c = sess.classification(s)
    out = set()
    for b, m in enumerate(c["mask"]):
        for k in range(8):
            if not (m >> k) & 1:
                out.add(b * 8 + k)
    return out


def _trimmed_sd(x, axis=0):
    """Разброс внутри фазы: СКО после усечения по 10-му и 90-му процентилям, ×1.3 (для нормального
    распределения ≈ σ). В отличие от MAD не обнуляется, когда больше половины кадров квантованного
    поля имеют одно значение (иначе грубое поле выглядело бы «чище» настоящего)."""
    lo, hi = np.percentile(x, [10, 90], axis=axis)
    return np.clip(x, lo, hi).std(axis=axis) * 1.3


def _binom_sf(k, n, p):
    """P(X ≥ k), X ~ Bin(n, p)."""
    if k <= 0:
        return 1.0
    if k > n or p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    if n > 300:                                    # нормальное приближение с поправкой на непрерывность
        mu = n * p
        sd = math.sqrt(n * p * (1.0 - p))
        return 0.5 * math.erfc((k - 0.5 - mu) / (sd * math.sqrt(2.0)))
    lp, lq = math.log(p), math.log1p(-p)
    lc = math.lgamma(n + 1)
    terms = [lc - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq for i in range(k, n + 1)]
    m = max(terms)
    return min(1.0, math.exp(m) * sum(math.exp(x - m) for x in terms))


# ---------------------------------------------------------------------------
# decode — статистика поля по фазам
# ---------------------------------------------------------------------------
def decode_stats(sess, spec):
    s, t, v = field_series(sess, spec)
    ok = ~np.isnan(v)
    groups = []
    for lab in sess.phase_labels():
        m = np.zeros(len(t), bool)
        for iv in sess.intervals_for([lab]):
            i0, i1 = interval_rows(t, iv.t0, iv.t1)
            m[i0:i1] = True
        groups.append((lab, m & ok))
    inph = np.zeros(len(t), bool)
    for iv in sess.phases:
        i0, i1 = interval_rows(t, iv.t0, iv.t1)
        inph[i0:i1] = True
    if sess.phases:
        groups.append(("(вне фаз)", ~inph & ok))
    groups.append(("(вся запись)", ok))
    rows = []
    for lab, m in groups:
        x = v[m]
        if len(x) == 0:
            rows.append({"phase": lab, "n": 0})
            continue
        rows.append({"phase": lab, "n": int(len(x)), "min": float(x.min()), "median": float(np.median(x)),
                     "mean": float(x.mean()), "max": float(x.max()), "std": float(x.std()),
                     "last": float(x[-1])})
    return {"field": str(spec), "stream": s.name, "frames": int(s.n), "valid": int(ok.sum()), "phases": rows,
            "t": t, "v": v, "stream_obj": s}


# ---------------------------------------------------------------------------
# compare A vs B
# ---------------------------------------------------------------------------
def _group_rows(t, intervals, skip, tail=0.0):
    idx = []
    spans = []
    dur = 0.0
    for iv in intervals:
        tl = min(tail, 0.1 * max(0.0, iv.t1 - iv.t0))
        i0, i1 = interval_rows(t, iv.t0, iv.t1 - tl, eff_skip(iv, skip))
        if i1 > i0:
            idx.append(np.arange(i0, i1))
            spans.append((i0, i1))
            dur += t[i1 - 1] - t[i0]
    if not idx:
        return np.zeros(0, np.int64), spans, 0.0
    return np.concatenate(idx), spans, dur


def _toggles(bits_full, spans):
    """Число переключений каждого бита внутри интервалов."""
    tot = None
    for i0, i1 in spans:
        if i1 - i0 < 2:
            continue
        b = bits_full[i0:i1]
        c = (b[1:] != b[:-1]).sum(axis=0)
        tot = c if tot is None else tot + c
    return tot


def _regular_toggles(bits_full, spans, t, q):
    """Интервалы между переключениями бита q регулярны (мигание)?"""
    gaps = []
    for i0, i1 in spans:
        b = bits_full[i0:i1, q]
        w = np.nonzero(b[1:] != b[:-1])[0]
        if len(w) >= 3:
            gaps.extend(np.diff(t[i0 + 1 + w]).tolist())
    if len(gaps) < 2:
        return False
    g = np.array(gaps)
    return float(g.std() / g.mean()) < 0.35 if g.mean() > 0 else False


def compare(sess, a_labels, b_labels, streams, skip=0.5, thr=0.8, top=20, tail=0.2):
    A = sess.intervals_for(a_labels)
    B = sess.intervals_for(b_labels)
    if not A:
        raise AnalysisError("нет фаз A: %s (есть: %s)" % (",".join(a_labels), ", ".join(sess.phase_labels()) or "—"))
    if not B:
        raise AnalysisError("нет фаз B: %s (есть: %s)" % (",".join(b_labels), ", ".join(sess.phase_labels()) or "—"))
    bits_out, blink_out, fields_out = [], [], []
    for s in streams:
        Lm = s.len_mode()
        if Lm == 0:
            continue
        ia, spa, durA = _group_rows(s.t, A, skip, tail)
        ib, spb, durB = _group_rows(s.t, B, skip, tail)
        if len(ia) < 3 or len(ib) < 3:
            continue
        # Биты: исключаются только счётчики и CRC (SPEC §10). Байт, помеченный «шум?», может содержать чистый
        # бит (выключатель тормоза рядом с быстро меняющимися битами): случайные биты и так отсекает порог
        # разницы долей (у них доли в A и B ≈ 0.5). Поля 8/16 бит и мигание — без «шумных» байтов.
        ex_hard = excluded_bits(sess, s, noise=False)
        ex = excluded_bits(sess, s)
        full = s.data[:, :Lm]
        okA = s.dlen[ia] >= Lm
        okB = s.dlen[ib] >= Lm
        ia, ib = ia[okA], ib[okB]
        if len(ia) < 3 or len(ib) < 3:
            continue
        bits_full = np.unpackbits(full, axis=1, bitorder="little")
        bA = bits_full[ia]
        bB = bits_full[ib]
        pA = bA.mean(axis=0)
        pB = bB.mean(axis=0)
        diff = np.abs(pA - pB)
        tgA = _toggles(bits_full, spa)
        tgB = _toggles(bits_full, spb)
        reported = set()
        for q in np.nonzero(diff >= thr)[0]:
            q = int(q)
            if q in ex_hard:
                continue
            purity = min(max(pA[q], 1 - pA[q]), max(pB[q], 1 - pB[q]))
            sp = FieldSpec(s.ch, s.can_id, q // 8, q % 8, 1, "le", ext=s.ext)
            bits_out.append({"spec": str(sp), "stream": s.name, "byte": q // 8, "bit": q % 8,
                             "a_value": int(round(pA[q])), "b_value": int(round(pB[q])),
                             "a_pct": round(100 * (pA[q] if pA[q] >= 0.5 else 1 - pA[q]), 1),
                             "b_pct": round(100 * (pB[q] if pB[q] >= 0.5 else 1 - pB[q]), 1),
                             "diff": round(float(diff[q]), 3), "purity": round(float(purity), 3),
                             "a_toggles": int(tgA[q]) if tgA is not None else 0,
                             "b_toggles": int(tgB[q]) if tgB is not None else 0,
                             "noisy_byte": bool(q in ex)})
            reported.add(q)
        # поля 8/16 бит (16-бит — только если не перекрывают найденные биты и 8-битные поля)
        D = full.astype(np.int64)
        d8 = {}
        kept8 = set()
        cands = []
        for i in range(Lm):
            cands.append((FieldSpec(s.ch, s.can_id, i, 0, 8, "le", ext=s.ext), D[:, i]))
        for i in range(Lm - 1):
            cands.append((FieldSpec(s.ch, s.can_id, i, 0, 16, "le", ext=s.ext), D[:, i] + 256 * D[:, i + 1]))
            cands.append((FieldSpec(s.ch, s.can_id, i, 7, 16, "be", ext=s.ext), 256 * D[:, i] + D[:, i + 1]))
        for sp, v in cands:
            bs = sp.bitset()
            if bs & ex:
                continue
            xa = v[ia].astype(np.float64)
            xb = v[ib].astype(np.float64)
            if xa.min() == xa.max() == xb.min() == xb.max():
                continue
            mA, mB = float(np.median(xa)), float(np.median(xb))
            if mA == mB:
                continue
            sA, sB = float(xa.std()), float(xb.std())
            lA, hA = np.percentile(xa, [5, 95])
            lB, hB = np.percentile(xb, [5, 95])
            sep = bool(hA < lB or hB < lA)
            d = abs(mA - mB) / (sA + sB + 1.0)
            if sp.length == 8:
                d8[sp.byte] = d
                # изменение целиком объясняется уже найденными битами — не дублировать
                x = int(mA) ^ int(mB)
                if x and all(((x >> k) & 1) == 0 or (sp.byte * 8 + k) in reported for k in range(8)):
                    continue
            else:
                b0 = sp.byte
                if b0 in kept8 or (b0 + 1) in kept8 or (bs & reported):
                    continue
                both_vary = (s.data[:, b0].min() != s.data[:, b0].max()) and (s.data[:, b0 + 1].min() != s.data[:, b0 + 1].max())
                if not both_vary or d <= 1.2 * max(d8.get(b0, 0), d8.get(b0 + 1, 0)):
                    continue
            if not sep and d < 3:
                continue
            if sp.length == 8:
                kept8.add(sp.byte)
            fields_out.append({"spec": str(sp), "stream": s.name, "a_median": mA, "b_median": mB,
                               "a_range": [float(lA), float(hA)], "b_range": [float(lB), float(hB)],
                               "separated": sep, "d": round(d, 2), "_bits": bs, "_key": s.key})
        # мигание: бит переключается в одной группе и стоит в другой
        if tgA is not None and tgB is not None and durA > 0 and durB > 0:
            rA = tgA / durA
            rB = tgB / durB
            for q in range(len(rA)):
                if q in ex or q in reported or (q // 8) in kept8:
                    continue
                for (r1, r2, g, gdur, gsp) in ((rB[q], tgA[q], "B", durB, spb), (rA[q], tgB[q], "A", durA, spa)):
                    if 0.3 <= r1 <= 20 and r2 == 0 and r1 * gdur >= 3 and _regular_toggles(bits_full, gsp, s.t, q):
                        sp = FieldSpec(s.ch, s.can_id, q // 8, q % 8, 1, "le", ext=s.ext)
                        blink_out.append({"spec": str(sp), "stream": s.name, "group": g,
                                          "hz": round(float(r1) / 2.0, 2),
                                          "other_value": int(round(pA[q] if g == "B" else pB[q]))})
    bits_out.sort(key=lambda r: (-r["diff"], -r["purity"], r["a_toggles"] + r["b_toggles"]))
    fields_out.sort(key=lambda r: (not r["separated"], -r["d"]))
    kept = []
    for f in fields_out:
        if any(k["_key"] == f["_key"] and (k["_bits"] & f["_bits"]) for k in kept):
            continue
        kept.append(f)
    for f in kept:
        del f["_bits"], f["_key"]
    sk = skip_report([("A", 0, A), ("B", 0, B)], skip)
    return {"a": list(a_labels), "b": list(b_labels), "a_intervals": len(A), "b_intervals": len(B),
            "bits": bits_out[:top], "bits_total": len(bits_out), "blink": blink_out[:top],
            "fields": kept[:top], "fields_total": len(kept), "skip": skip,
            "skip_min": min([x["skipped"] for x in sk] or [skip])}


# ---------------------------------------------------------------------------
# marks
# ---------------------------------------------------------------------------
def _merge(iv):
    """Объединение пересекающихся отрезков [(a, b)] (по возрастанию a)."""
    out = []
    for a, b in sorted(iv):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _clip_to(iv, dom):
    out = []
    for a, b in iv:
        for c, d in dom:
            x, y = max(a, c), min(b, d)
            if y > x:
                out.append((x, y))
    return _merge(out)


def marks_analysis(sess, streams, label=None, window=1.0, before=0.3, top=20, phase=None, lag=3.0, lag_step=0.1,
                   noisy_rate=0.3):
    """Что меняется у отметок. Для каждого бита — лучший сдвиг окна относительно отметок (−lag…+lag):
    отметку жмут то до действия, то после. Оценка — с поправкой на случайность: окна покрывают долю
    coverage записи, и случайно переключающийся бит попадал бы в них с той же долей.

    score = полнота × (точность − coverage) / (1 − coverage); p — биномиальная вероятность получить
    столько попаданий случайно (с поправкой на перебор сдвигов)."""
    marks = [m for m in sess.marks if label is None or m["label"] == label]
    if phase:
        ivs = sess.intervals_for([phase])
        if not ivs:
            raise AnalysisError("фазы «%s» нет в сессии (есть: %s)" % (phase, ", ".join(sess.phase_labels()) or "—"))
        dom = _merge([(iv.t0, iv.t1) for iv in ivs])
        marks = [m for m in marks if any(a <= m["t"] <= b for a, b in dom)]
    else:
        dom = [[sess.t_first, sess.t_last]]
    what = ((" с меткой «%s»" % label) if label else "") + ((" в фазе «%s»" % phase) if phase else "")
    if len(marks) < 2:
        raise AnalysisError("нужно хотя бы 2 отметки%s, найдено %d" % (what, len(marks)))
    dom_len = sum(b - a for a, b in dom)
    if dom_len <= 0:
        raise AnalysisError("в записи нет кадров — анализировать нечего")
    mt = np.array(sorted(m["t"] for m in marks))
    n = len(mt)
    lags = [0.0] if lag <= 0 else [round(x, 3) for x in np.arange(-lag, lag + lag_step / 2, lag_step)]
    wins = []                          # по сдвигам: объединение окон ∩ область, доля покрытия
    for dl in lags:
        u = _clip_to([(m + dl - before, m + dl + window) for m in mt], dom)
        cov = sum(b - a for a, b in u) / dom_len
        wins.append((dl, u, min(max(cov, 1e-6), 0.999)))
    cov0 = [w[2] for w in wins if abs(w[0]) < 1e-9][0] if any(abs(w[0]) < 1e-9 for w in wins) else wins[0][2]
    n_eff = 1.0 + (2.0 * lag / (window + before) if lag > 0 else 0.0)   # сколько «независимых» сдвигов перебрано
    out = []
    for s in streams:
        Lm = s.len_mode()
        if Lm == 0 or s.n < 3:
            continue
        hard = excluded_bits(sess, s, noise=False)
        bits = np.unpackbits(s.data[:, :Lm], axis=1, bitorder="little")
        tc = s.t[1:]
        in_dom = np.zeros(len(tc), bool)
        for a, b in dom:
            in_dom[int(np.searchsorted(tc, a, "left")):int(np.searchsorted(tc, b, "right"))] = True
        ch = (bits[1:] != bits[:-1]) & in_dom[:, None]
        if not ch.any():
            continue
        # «шумный» бит — по нему самому, а не по классу байта: меняется больше чем в noisy_rate кадров
        rate = ch.sum(axis=0) / max(1, int(in_dom.sum()))
        cum = np.vstack([np.zeros((1, ch.shape[1]), np.int64), np.cumsum(ch, axis=0, dtype=np.int64)])
        total = cum[-1]
        cand = [q for q in range(ch.shape[1]) if total[q] >= 2 and q not in hard and rate[q] <= noisy_rate]
        if not cand:
            continue
        cq = np.array(cand)
        best = None                                      # по битам: (score, dl, inside, hits, cov)
        for dl, u, cov in wins:
            ins = np.zeros(len(cq), np.int64)
            for a, b in u:
                i0 = int(np.searchsorted(tc, a, "left"))
                i1 = int(np.searchsorted(tc, b, "right"))
                ins += cum[i1, cq] - cum[i0, cq]
            i0 = np.searchsorted(tc, mt + dl - before, "left")
            i1 = np.searchsorted(tc, mt + dl + window, "right")
            hits = ((cum[i1][:, cq] - cum[i0][:, cq]) > 0).sum(axis=0)
            prec = ins / total[cq]
            inf = np.clip((prec - cov) / (1.0 - cov), 0.0, 1.0)
            sc = hits / float(n) * inf - 0.002 * abs(dl)          # при равенстве — сдвиг ближе к нулю
            if best is None:
                best = [sc.copy(), np.full(len(cq), dl), ins.copy(), hits.copy(), np.full(len(cq), cov)]
            else:
                w = sc > best[0]
                best[0][w] = sc[w]
                best[1][w] = dl
                best[2][w] = ins[w]
                best[3][w] = hits[w]
                best[4][w] = cov
        for j, q in enumerate(cand):
            dl = float(best[1][j])
            ins, hits, cov = int(best[2][j]), int(best[3][j]), float(best[4][j])
            tot = int(total[q])
            prec = ins / float(tot)
            rec = hits / float(n)
            inf = max(0.0, (prec - cov) / (1.0 - cov))
            score = rec * inf
            f1 = 2 * prec * rec / (prec + rec) if prec + rec > 0 else 0.0
            pattern = []
            for m in mt:
                j0 = max(0, int(np.searchsorted(s.t, m + dl - before, "left")) - 1)
                j1 = min(s.n - 1, int(np.searchsorted(s.t, m + dl + window, "right")) - 1)
                pattern.append("%d→%d" % (bits[j0, q], bits[j1, q]))
            sp = FieldSpec(s.ch, s.can_id, q // 8, q % 8, 1, "le", ext=s.ext)
            out.append({"spec": str(sp), "stream": s.name, "hits": hits, "marks": n, "inside": ins,
                        "outside": tot - ins, "precision": round(prec, 3), "recall": round(rec, 3),
                        "coverage": round(cov, 3), "lift": round(prec / cov, 2) if cov > 0 else None,
                        "score": round(score, 3), "f1": round(f1, 3), "lag": round(dl, 2), "pattern": pattern})
    # Место в списке: оценка × значимость. «Все 3 переключения в окнах» при окнах на ползаписи — слабое
    # свидетельство (p ≈ 0.1), а 30 переключений кнопки только в окнах — сильное (p < 0.001).
    for r in out:
        p = _binom_sf(r["inside"], r["inside"] + r["outside"], r["coverage"])
        r["p"] = p
        r["p_adj"] = min(1.0, p * n_eff)
        sig = min(1.0, max(0.0, -math.log10(max(r["p_adj"], 1e-300))) / 2.0)
        r["rank"] = round(r["score"] * sig, 4)
    out.sort(key=lambda r: (-r["rank"], -r["score"], -r["hits"], abs(r["lag"])))
    found = [r for r in out[:top] if r["score"] >= 0.3 and r["p_adj"] < 0.05]
    return {"marks": n, "label": label, "phase": phase, "window": window, "before": before, "lag": lag,
            "coverage": round(cov0, 3), "bits": out[:top], "total": len(out), "found": bool(found),
            "mark_times": [float(x) for x in mt]}


# ---------------------------------------------------------------------------
# track
# ---------------------------------------------------------------------------
class Candidate(object):
    __slots__ = ("stream", "byte", "bit", "length", "order", "signed", "score", "r2", "ratio", "mono", "rank", "nice",
                 "a", "b", "medians", "spreads", "ns", "bits", "time_mono", "timer", "unsettled", "variants",
                 "phases_used", "factor", "offset", "rms", "rms_fit", "rmsf", "res", "alt", "col", "why")

    def spec(self, scaled=True):
        s = self.stream
        sp = FieldSpec(s.ch, s.can_id, self.byte, self.bit, self.length, self.order, self.signed, ext=s.ext)
        if scaled and self.factor is not None:
            sp = sp.replace(factor=self.factor, offset=self.offset)
        return sp


def _gen_candidates(D, L):
    """Перебор полей: (byte, bit, len, order, signed, values int64)."""
    out = []
    D = D.astype(np.int64)

    def add(byte, bit, ln, order, u):
        out.append((byte, bit, ln, order, False, u))
        msb = (u >> (ln - 1)) & 1
        if msb.min() != msb.max():
            out.append((byte, bit, ln, order, True, np.where(msb == 1, u - (1 << ln), u)))

    for i in range(L):
        add(i, 0, 8, "le", D[:, i])
        add(i, 0, 4, "le", D[:, i] & 0xF)
        add(i, 4, 4, "le", D[:, i] >> 4)
    for i in range(L - 1):
        a, b = D[:, i], D[:, i + 1]
        add(i, 0, 16, "le", a | (b << 8))
        add(i, 7, 16, "be", (a << 8) | b)
        add(i, 0, 12, "le", a | ((b & 0xF) << 8))
        add(i, 4, 12, "le", (a >> 4) | (b << 4))
        add(i, 7, 12, "be", (a << 4) | (b >> 4))
        add(i, 3, 12, "be", ((a & 0xF) << 8) | b)
    for i in range(L - 2):
        a, b, c = D[:, i], D[:, i + 1], D[:, i + 2]
        add(i, 0, 24, "le", a | (b << 8) | (c << 16))
        add(i, 7, 24, "be", (a << 16) | (b << 8) | c)
    for i in range(L - 3):
        a, b, c, d = D[:, i], D[:, i + 1], D[:, i + 2], D[:, i + 3]
        add(i, 0, 32, "le", a | (b << 8) | (c << 16) | (d << 24))
        add(i, 7, 32, "be", (a << 24) | (b << 16) | (c << 8) | d)
    return out


_BITSET_CACHE = {}


def _bitset(byte, bit, ln, order):
    k = (byte, bit, ln, order)
    r = _BITSET_CACHE.get(k)
    if r is None:
        r = _BITSET_CACHE[k] = FieldSpec(None, None, byte, bit, ln, order).bitset()
    return r


def _positions(byte, bit, ln, order):
    return FieldSpec(None, None, byte, bit, ln, order).positions()


def build_ref_groups(sess, refs=None, ref_inputs=False, input_window=2.0):
    """Список групп: (label, ref, [интервалы])."""
    groups = []
    warn = []

    class _Iv(object):
        __slots__ = ("label", "t0", "t1")

        def __init__(self, label, t0, t1):
            self.label, self.t0, self.t1 = label, t0, t1

    if ref_inputs:
        by_label = {}
        pseudo = []
        for inp in sess.inputs:
            if inp["value"] is None:
                warn.append("ввод «%s» — не число, пропущен" % inp["raw"])
                continue
            if inp["phase"]:
                by_label.setdefault(inp["phase"], []).append(inp["value"])
            else:
                pseudo.append(inp)
        for lab, vals in by_label.items():
            groups.append((lab, float(np.median(vals)), sess.intervals_for([lab])))
        for inp in pseudo:
            lab = "ввод@%.1fс" % (inp["t"] - sess.t0)
            groups.append((lab, inp["value"], [_Iv(lab, inp["t"] - input_window, inp["t"] + input_window)]))
        if not groups:
            raise AnalysisError("в событиях нет числовых вводов (input)")
    for lab, val in (refs or {}).items():
        ivs = sess.intervals_for([lab])
        if not ivs:
            warn.append("фазы «%s» нет в сессии (есть: %s)" % (lab, ", ".join(sess.phase_labels()) or "—"))
            continue
        groups = [g for g in groups if g[0] != lab]
        groups.append((lab, float(val), ivs))
    groups.sort(key=lambda g: min([iv.t0 for iv in g[2]] or [0.0]))   # в порядке времени
    return groups, warn


def _nice_scale(x, y, w, a, b, max_dev=0.03, loose=False):
    """Округлить FACTOR/OFFSET до «красивых», если подгонка заметно не хуже
    (и множитель отличается от подогнанного не больше чем на max_dev).
    loose — в фазах значение ещё не установилось: подгонка смещена, допуск шире (6 %, 2 % размаха).
    Возвращает (factor, offset, rms, rms_fit, nice)."""
    W = w.sum()
    if loose:
        max_dev = max(max_dev, 0.06)

    def rms(f, o):
        r = y - (f * x + o)
        return math.sqrt(float((w * r * r).sum() / W))
    rms0 = rms(a, b)
    yr = float(y.max() - y.min()) if len(y) else 1.0
    # эталон обычно снят «на глаз» (тахометр ±1-2 %), поэтому допускаем ухудшение на 1 % размаха
    tol = max(rms0 * 1.25, rms0 + (0.02 if loose else 0.01) * yr)
    for f in nice_factor_candidates(a, max_dev):
        bo = float((w * (y - f * x)).sum() / W)
        for o in round_offset_candidates(bo):
            r = rms(f, o)
            if r <= tol:
                return f, o, r, rms0, True
    # не округляется — 4 значащие цифры
    f = float("%.4g" % a)
    o = float("%.4g" % b) if abs(b) > 1e-9 else 0.0
    return f, o, rms(f, o), rms0, False


def _carry_stats(s, small, big):
    """big = small, продолженный младшими битами. Статистика «переноса»: (число изменений старшей части,
    доля кадров с её изменением, корреляция ΔU и ΔL). У настоящего продолжения при изменении старшей
    части младшая «переносится» (ΔL против ΔU — отрицательная корреляция), если шум меньше шага старшей."""
    k = big.length - small.length
    sp_s = FieldSpec(s.ch, s.can_id, small.byte, small.bit, small.length, small.order)
    sp_b = FieldSpec(s.ch, s.can_id, big.byte, big.bit, big.length, big.order)
    U, ok1 = decode_raw(s.data, s.dlen, sp_s)
    V, ok2 = decode_raw(s.data, s.dlen, sp_b)
    ok = ok1 & ok2
    U, V = U[ok], V[ok]
    Lo = V - (U << k)
    dU = np.diff(U)
    dL = np.diff(Lo)
    m = dU != 0
    n = int(m.sum())
    rate = n / float(max(1, len(dU)))
    if n < 8:
        return n, rate, 0.0
    x, y = dU[m].astype(np.float64), dL[m].astype(np.float64)
    if x.std() == 0 or y.std() == 0:
        return n, rate, 0.0
    return n, rate, float(np.corrcoef(x, y)[0, 1])


def _carry_continuous(s, small, big):
    n, rate, r = _carry_stats(s, small, big)
    return n >= 8 and r < -0.1


# --- граница поля: какие из вложенных кандидатов — одно и то же значение ------------------------------
def _is_standard(c):
    """Стандартное выравнивание (как почти всегда в DBC): le от бита 0 байта, be от бита 7; 8/16/24/32 бит."""
    return c.length in (8, 16, 24, 32) and ((c.order == "le" and c.bit == 0) or (c.order == "be" and c.bit == 7))


class _FieldInfo(object):
    """Кэш на время одного track: постоянные биты потоков, биты, меняющиеся в строках фаз, и отношения
    «то же значение». «Живой» бит — тот, что меняется в анализируемых строках фаз: бит, который меняется
    только вне фаз, в подгонке ничего не даёт (а в поле с ним попадает чужой сигнал)."""

    def __init__(self, sess, pinfo):
        self.sess = sess
        self.pinfo = pinfo          # id(поток) -> (множество бит, меняющихся в строках фаз, строка-образец)
        self.cb = {}
        self.rel = {}
        self.foreign = set()        # (id(small), id(big)): младшие биты big доказанно чужие (нет переноса)

    def const(self, s):
        k = id(s)
        if k not in self.cb:
            self.cb[k] = const_bits(self.sess, s)
        return self.cb[k]

    def pvar(self, s):
        return self.pinfo[id(s)][0]

    def n_live(self, c):
        return len(c.bits & self.pvar(c.stream))

    def high_const_nonzero(self, c):
        """Над старшим «живым» битом поля стоят неизменные в фазах НЕнулевые биты (поле захватывает чужой
        сигнал или флаги: поглощение их даёт смещение и скачки вне фаз)."""
        pv = self.pvar(c.stream)
        row = self.pinfo[id(c.stream)][1]
        hi = []
        for q in reversed(_positions(c.byte, c.bit, c.length, c.order)):
            if q in pv:
                break
            hi.append(q)
        if not hi:
            return False
        if any(q not in self.const(c.stream) for q in hi):
            # вне фаз эти биты меняются: продолжение того же числа (перенос из младших бит) или чужой сигнал
            f = self._hi_foreign(c, len(hi))
            if f is not None:
                return f
        return any((int(row[q // 8]) >> (q % 8)) & 1 for q in hi if q // 8 < len(row))

    def _hi_foreign(self, c, h):
        """Старшие h бит поля меняются независимо от остальных (не перенос) — чужой сигнал: True;
        меняются переносом из младших бит (то же число) — False; изменений мало, судить нельзя — None."""
        s = c.stream
        if h >= c.length:
            return None
        if c.order == "le":
            p = c.byte * 8 + c.bit + (c.length - h)
            up = FieldSpec(None, None, p // 8, p % 8, h, "le")
        else:
            up = FieldSpec(None, None, c.byte, c.bit, h, "be")
        U, ok = decode_raw(s.data, s.dlen, up)
        if int((np.diff(U[ok]) != 0).sum()) < 8:
            return None
        return not _carry_continuous(s, up, c)

    def relation(self, small, big):
        """small ⊂ big по битам. 'const' — лишние биты big в строках фаз не меняются (то же значение);
        'cont' — снизу к small примыкают «живые» младшие биты — продолжение того же значения (big точнее);
        None — в лишних битах другой сигнал (или small — младшая часть большего числа)."""
        key = (id(small), id(big))
        if key in self.rel:
            return self.rel[key]
        r = self._relation(small, big)
        self.rel[key] = r
        return r

    def _relation(self, small, big):
        if not small.bits < big.bits:
            return None
        s = big.stream
        pos = _positions(big.byte, big.bit, big.length, big.order)             # от младшего к старшему
        spos = _positions(small.byte, small.bit, small.length, small.order)
        try:
            i0 = pos.index(spos[0])
        except ValueError:
            return None
        if pos[i0:i0 + len(spos)] != spos:
            return None                                  # small не лежит в big тем же порядком бит
        pv = self.pvar(s)
        above = pos[i0 + len(spos):]
        below = pos[:i0]
        if any(q in pv for q in above):
            return None
        live = [i for i, q in enumerate(below) if q in pv]
        if not live:
            return "const"
        if live != list(range(len(below) - len(live), len(below))):
            return None                                  # живые биты не примыкают к small снизу
        k = len(live)
        if not above:
            # Перенос виден, если шум меньше шага старшей части: тогда он решает. Если старшая часть «чистая»
            # (меняется редко), а переноса нет — снизу чужой сигнал (педаль + младший байт оборотов)
            n, rate, r = _carry_stats(s, small, big)
            if n >= 8 and r < -0.1:
                return "cont"
            if n >= 8 and rate < 0.1:
                self.foreign.add((id(small), id(big)))
                return None
        sp_b = FieldSpec(s.ch, s.can_id, big.byte, big.bit, big.length, big.order)
        V, ok = decode_raw(s.data, s.dlen, sp_b)
        lo = (V[ok] >> (len(below) - k)) & ((1 << k) - 1)
        # Шумный сигнал: старшая часть меняется почти в каждом кадре, перенос не виден. Продолжение до 16 бит
        # (12→16, 8→16, 4→8…) — обычное дело: достаточно, что младшие биты принимают почти все значения
        # (не флаги). Поле длиннее 16 бит из шумного соседнего байта и настоящих 16 бит так не отличить
        if big.length - (len(below) - k) <= 16 and len(lo) and len(np.unique(lo)) >= 0.75 * min(1 << k, 16):
            return "cont"
        return None

    def canon_key(self, c):
        return (self.n_live(c), not self.high_const_nonzero(c), _is_standard(c),
                -len(c.bits - self.pvar(c.stream)), c.rank, not c.signed)


def _dedupe(fi, cands, top):
    """Лучшие кандидаты без повторов. Вложенные поля, кодирующие ТО ЖЕ значение (отличаются постоянными
    битами или продолжением младшими битами), сводятся к одной границе: больше «живых» бит, без постоянных
    ненулевых старших, стандартное выравнивание, меньше постоянных бит. Кандидат, лишь частично
    перекрывающий выбранный и подогнанный заметно лучше (СКО подгонки втрое меньше), остаётся отдельным."""
    rem = sorted(cands, key=lambda c: -c.rank)
    out = []

    def same_as(x):
        """Кандидаты того же потока, кодирующие то же значение, что x (включая x)."""
        r = [x]
        for c in rem:
            if c is x or c.stream is not x.stream:
                continue
            if c.bits == x.bits or (c.bits < x.bits and fi.relation(c, x)) or (x.bits < c.bits and fi.relation(x, c)):
                r.append(c)
        return r

    while rem and len(out) < top:
        best = rem[0]
        s = best.stream
        same = same_as(best)
        rep = max(same, key=fi.canon_key)
        twins = [c for c in same if c.bits == rep.bits]
        if len(twins) > 1:                                # со знаком и без: без знака, если не заметно хуже
            uns = [c for c in twins if not c.signed]
            sig = [c for c in twins if c.signed]
            if uns and sig:
                rep = sig[0] if sig[0].rank > uns[0].rank + 1e-9 else uns[0]
        union = best.bits | rep.bits
        cluster = [c for c in rem if c.stream is s and (c.bits & union)]
        same_ids = set(id(c) for c in same)
        keep_ids = set()
        for c in cluster:
            if id(c) in same_ids:
                continue
            nested = c.bits <= rep.bits or rep.bits <= c.bits
            if not nested and c.rmsf * 3.0 < rep.rmsf and c.score >= 0.3 and not _same_signal(s, c, rep):
                keep_ids.add(id(c))
        var = [c for c in same if c is not rep and c.bits != rep.bits]
        var += [c for c in cluster if id(c) not in same_ids and id(c) not in keep_ids]
        seen = set()
        rep.variants = []
        for c in sorted(var, key=lambda c: (id(c) not in same_ids, -c.rank)):
            if c.bits in seen:
                continue
            seen.add(c.bits)
            rep.variants.append(c)
            if len(rep.variants) >= 4:
                break
        nl = fi.n_live(rep)
        rep.alt = [c for c in same if c.bits != rep.bits and fi.n_live(c) == nl and not c.signed]
        out.append(rep)
        drop = set(id(c) for c in cluster) - keep_ids
        rem = [c for c in rem if id(c) not in drop]
    return out


def _same_signal(s, c1, c2, thr=0.995):
    """Два кандидата одного потока — по сути одно и то же число (например, грубая копия того же сигнала
    в перекрывающихся байтах): их значения по всей записи почти строго линейно связаны."""
    v = []
    for c in (c1, c2):
        sp = FieldSpec(s.ch, s.can_id, c.byte, c.bit, c.length, c.order, c.signed)
        r, ok = decode_raw(s.data, s.dlen, sp)
        v.append((r.astype(np.float64), ok))
    ok = v[0][1] & v[1][1]
    a, b = v[0][0][ok], v[1][0][ok]
    if len(a) < 10 or a.std() == 0 or b.std() == 0:
        return False
    return abs(float(np.corrcoef(a, b)[0, 1])) >= thr


def _slope(x, y):
    """Наклон МНК y по x для каждой колонки y (x — вектор)."""
    xm = x.mean()
    dx = x - xm
    sxx = float((dx * dx).sum())
    if sxx <= 0:
        return np.zeros(y.shape[1])
    return (dx[:, None] * (y - y.mean(axis=0))).sum(axis=0) / sxx


def track(sess, streams, groups, skip=0.7, top=5, per_phase=400, min_rows=5, min_score=0.02):
    """Поиск числового поля, линейно связанного с эталоном.

    Уровень фазы — медиана второй половины отрезка (после отброшенного начала): так меньше смещение от
    переходного процесса, если кнопку нажали до того, как значение установилось. Первая половина служит
    для проверки тренда внутри фазы («не установилось», таймеры).

    Оценка = (1 − 2·СКО_подгонки/размах) × (1 − 2·|a|·разброс/размах) × (1 − шаг/размах) × монотонность
    × доля фаз; разброс — усечённое СКО внутри фазы; шаг — цена младшего бита поля (грубые копии ниже)."""
    warn = []
    groups = [g for g in groups if g[2]]
    refs_distinct = sorted(set(g[1] for g in groups))
    if len(groups) < 2:
        raise AnalysisError("для track нужно хотя бы 2 фазы с эталоном (лучше ≥3 с разными значениями)")
    if len(refs_distinct) < 3:
        warn.append("эталонов с разными значениями меньше 3 — линейная подгонка ненадёжна, "
                    "сделайте ещё фазы с другими значениями")
    elif len(refs_distinct) == 3:
        warn.append("эталонов ровно 3 — у прямой одна степень свободы, случайное совпадение вероятнее; "
                    "подтвердите живым значением или добавьте 4-й уровень")
    # порядок фаз во времени
    tl = sorted([(iv.t0, g[1]) for g in groups for iv in g[2]])
    ref_seq = np.array([r for _, r in tl])
    ref_time_mono = len(ref_seq) >= 3 and (np.all(np.diff(ref_seq) >= 0) or np.all(np.diff(ref_seq) <= 0))
    if ref_time_mono:
        warn.append("фазы шли по возрастанию/убыванию эталона — счётчики и таймеры могут давать ложные "
                    "совпадения; для надёжности повторите фазы в другом порядке")
    all_c = []
    n_eval = 0
    seen_groups = set()
    pinfo = {}
    for s in streams:
        Lm = s.len_mode()
        if Lm == 0 or s.n < 10:
            continue
        ex = excluded_bits(sess, s, noise=False)
        sel = []
        ginfo = []   # (группа, строки первой трети, строки второй половины, строки последней трети)
        ib = []      # (t0 интервала, ref, a, b)
        pos = 0
        for gi, (lab, ref, ivs) in enumerate(groups):
            nint = max(1, len(ivs))
            hp, tp, lp = [], [], []
            for iv in ivs:
                i0, i1 = interval_rows(s.t, iv.t0, iv.t1, eff_skip(iv, skip))
                rr = subsample(i0, i1, max(20, per_phase // nint))
                rr = rr[s.dlen[rr] >= Lm] if len(rr) else rr
                if len(rr) == 0:
                    continue
                n_ = len(rr)
                h, t3 = n_ // 2, n_ // 3
                sel.append(rr)
                hp.append(np.arange(pos, pos + t3))
                tp.append(np.arange(pos + h, pos + n_))
                lp.append(np.arange(pos + n_ - t3, pos + n_))
                ib.append((iv.t0, ref, pos, pos + n_))
                pos += n_
            if not tp:
                continue
            head, tail, last = np.concatenate(hp), np.concatenate(tp), np.concatenate(lp)
            if len(tail) + len(head) < min_rows:
                continue
            if len(tail) < 3:                              # кадров мало — уровень по всему отрезку
                tail = np.concatenate([head, tail])
                head = last = np.zeros(0, np.int64)
            ginfo.append((gi, head, tail, last))
            seen_groups.add(gi)
        if len(ginfo) < 2:
            continue
        rows = np.concatenate(sel)
        tr = s.t[rows]
        D = s.data[rows, :Lm]
        pm = np.bitwise_or.reduce(D ^ D[0], axis=0)
        pinfo[id(s)] = (frozenset(b * 8 + k for b in range(Lm) for k in range(8) if (int(pm[b]) >> k) & 1),
                        D[0].copy())
        cands = _gen_candidates(D, Lm)
        keep = []
        cols = []
        resl = []
        for (byte, bit, ln, order, sg, u) in cands:
            bs = _bitset(byte, bit, ln, order)
            if bs & ex:
                continue
            if u.min() == u.max():
                continue
            vm = int(np.bitwise_or.reduce(u ^ u[0]))
            res_ = float(vm & -vm) if vm > 0 else 1.0   # младшие неизменные биты → шаг квантования
            keep.append((byte, bit, ln, order, sg, bs))
            cols.append(u)
            resl.append(res_)
        if not keep:
            continue
        V = np.stack(cols, axis=1).astype(np.float64)   # rows × C
        C = V.shape[1]
        n_eval += C
        P = len(ginfo)
        M = np.empty((P, C))
        S = np.empty((P, C))
        Mh = np.zeros((P, C))                              # медиана первой трети
        Ml = np.zeros((P, C))                              # медиана последней трети
        ns = np.empty(P)
        nh = np.zeros(P)
        dtau = np.zeros(P)
        y = np.empty(P)
        for k, (gi, head, tail, last) in enumerate(ginfo):
            blk = V[tail]
            M[k] = np.median(blk, axis=0)
            S[k] = _trimmed_sd(blk)
            ns[k] = len(tail)
            y[k] = groups[gi][1]
            if len(head) >= 3 and len(last) >= 3:
                Mh[k] = np.median(V[head], axis=0)
                Ml[k] = np.median(V[last], axis=0)
                nh[k] = min(len(head), len(last))
                dtau[k] = float(tr[last].mean() - tr[head].mean())
        w = np.sqrt(ns)
        W = w.sum()
        xm = (w[:, None] * M).sum(0) / W
        ym = float((w * y).sum() / W)
        dx = M - xm
        sxx = (w[:, None] * dx * dx).sum(0)
        sxy = (w[:, None] * dx * (y - ym)[:, None]).sum(0)
        good = sxx > 0
        a_ = np.where(good, sxy / np.where(good, sxx, 1), 0.0)
        b_ = ym - a_ * xm
        res = y[:, None] - (a_ * M + b_)
        sst = float((w * (y - ym) ** 2).sum())
        ssr = (w[:, None] * res * res).sum(0)
        r2 = 1 - ssr / sst if sst > 0 else np.zeros(C)
        rmsf = np.sqrt(ssr / W)
        yr = float(y.max() - y.min())
        resv = np.array(resl)
        seff = np.sqrt((S * S).mean(0) + resv * resv / 12.0)
        if yr > 0:
            ratio = 2 * np.abs(a_) * seff / yr
            fit_ratio = 2 * rmsf / yr
            res_ratio = np.abs(a_) * resv / yr
        else:
            ratio = fit_ratio = res_ratio = np.ones(C)
        # монотонность: согласованность знаков по парам фаз с разными эталонами
        num = np.zeros(C)
        npairs = 0
        for i in range(P):
            for j in range(i + 1, P):
                dy = y[j] - y[i]
                if dy == 0:
                    continue
                npairs += 1
                num += np.sign(M[j] - M[i]) * np.sign(dy)
        mono = np.abs(num) / npairs if npairs else np.zeros(C)
        score = (np.clip(1 - fit_ratio, 0, 1) * np.clip(1 - ratio, 0, 1) * np.clip(1 - res_ratio, 0, 1)
                 * mono * (float(P) / len(groups)))
        score[~good] = 0
        # тренд внутри фаз: последняя треть отрезка против первой
        hk = nh >= 3
        dmed = Ml - Mh
        se = 1.2533 * S * np.sqrt(2.0 / np.maximum(nh, 1)[:, None])
        sig = (np.abs(dmed) > np.maximum(3 * se, 0.5 * resv[None, :])) & hk[:, None]
        # таймер/пробег: значимый тренд одного знака и похожей скорости во ВСЕХ фазах, и этот же наклон
        # объясняет разницу уровней между фазами (у настоящего сигнала хоть в одной фазе тренда нет)
        timer = np.zeros(C, bool)
        tmono = np.zeros(C, bool)
        Mi = ti = None
        if len(ib) >= 2:
            ibs = sorted(ib)
            Mi = np.stack([np.median(V[a:b], axis=0) for _, _, a, b in ibs])
            ti = np.array([float(tr[a:b].mean()) for _, _, a, b in ibs])
        if hk.sum() >= 2 and Mi is not None:
            dd = dmed[hk]
            beta = dd / np.where(dtau[hk] > 0, dtau[hk], 1.0)[:, None]
            allsig = sig[hk].all(axis=0)
            same = np.all(np.sign(dd) == np.sign(dd[0]), axis=0)
            ab = np.abs(beta)
            similar = ab.max(axis=0) <= 3.0 * np.maximum(ab.min(axis=0), 1e-12)
            bw = np.median(beta, axis=0)
            bb = _slope(ti, Mi)
            cons = (np.sign(bb) == np.sign(bw)) & (np.abs(bb) >= np.abs(bw) / 2.5) & (np.abs(bb) <= np.abs(bw) * 2.5)
            timer = allsig & same & similar & cons
            score[timer] *= 0.1
        # монотонные по ВРЕМЕНИ поля (счётчики, таймеры) — при немонотонном порядке эталонов отбросить
        if Mi is not None and len(ib) >= 3:
            di = np.diff(Mi, axis=0)
            tmono = np.all(di > 0, axis=0) | np.all(di < 0, axis=0)
            rs = np.array([r for _, r, _, _ in sorted(ib)])
            if not (np.all(np.diff(rs) >= 0) or np.all(np.diff(rs) <= 0)):
                score[tmono] = 0
        # «не установилось»: тренд внутри фазы больше 2 % размаха
        unset = sig & (np.abs(a_[None, :] * dmed) > 0.02 * max(yr, 1e-12))
        order_idx = np.argsort(-score)[:60]
        for ci in order_idx:
            if score[ci] < min_score:
                break
            byte, bit, ln, order, sg, bs = keep[ci]
            c = Candidate()
            c.stream, c.byte, c.bit, c.length, c.order, c.signed = s, byte, bit, ln, order, sg
            c.score, c.r2, c.ratio, c.mono = float(score[ci]), float(r2[ci]), float(ratio[ci]), float(mono[ci])
            c.a, c.b = float(a_[ci]), float(b_[ci])
            c.rmsf = float(rmsf[ci])
            c.res = float(abs(a_[ci]) * resv[ci])
            c.medians = M[:, ci].copy()
            c.spreads = S[:, ci].copy()
            c.ns = ns.copy()
            c.bits = bs
            c.time_mono = bool(tmono[ci])
            c.timer = bool(timer[ci])
            c.phases_used = [g_[0] for g_ in [groups[x[0]] for x in ginfo]]
            c.unsettled = [groups[ginfo[k][0]][0] for k in range(P) if unset[k, ci]]
            c.factor = c.offset = None
            c.variants = []
            c.alt = []
            all_c.append(c)
    missing = [groups[gi][0] for gi in range(len(groups)) if gi not in seen_groups]
    if missing and streams:
        warn.append("в фазах %s нет кадров (или слишком мало) ни в одном сообщении — связь с контроллером "
                    "или сниффер? Эти фазы в подгонке не участвуют" % ", ".join("«%s»" % x for x in missing))
    # априорное знание: множители в CAN почти всегда «красивые» (0.25, 0.125, 1, 2 …) — при почти равной
    # оценке поле с красивым множителем идёт выше (иначе гладкий «родственник» вроде педали обгоняет
    # шумные, но настоящие обороты)
    refmap = dict((g[0], g[1]) for g in groups)
    all_c.sort(key=lambda c: -c.score)
    # Бритва Оккама. Поле, в котором кроме подходящего вложенного поля есть чужие биты (не продолжение и не
    # постоянные: соседний шумный байт, педаль + младший байт оборотов), убираем, если вложенное поле почти
    # так же хорошо или чужие биты доказаны (нет переноса): лишние биты подгонке ничего не дали, а шаг
    # квантования прячут и завышают оценку.
    fi = _FieldInfo(sess, pinfo)
    by_s = {}
    for c in all_c:
        by_s.setdefault(id(c.stream), []).append(c)
    bad = set()
    for cs in by_s.values():
        for c in cs:
            for u in cs:
                if u.bits < c.bits and fi.relation(u, c) is None and                         (u.score >= c.score - 0.005 or (id(u), id(c)) in fi.foreign):
                    bad.add(id(c))
                    break
    if bad:
        all_c = [c for c in all_c if id(c) not in bad]
    # Фаза «не установилась», если тренд внутри неё виден у лучших кандидатов (у поля с редкими кадрами
    # тренд может не пройти порог значимости, а смещение уровня у него то же) — для всех кандидатов
    unsettled = []
    for c in all_c[:20]:
        if c.score >= 0.5 and not c.timer:
            for lab in c.unsettled:
                if lab not in unsettled:
                    unsettled.append(lab)
    if unsettled:
        warn.append("в фазах %s в начале отрезка значение ещё менялось (кнопку нажали раньше, чем оно "
                    "установилось): уровень взят по второй половине фазы, множитель округляется с допуском шире; "
                    "для точности держите состояние дольше перед кнопкой или увеличьте --skip"
                    % ", ".join("«%s»" % x for x in unsettled))
    for i, c in enumerate(all_c):
        c.rank = c.score
        c.nice = False
        if i < 200 and c.score >= 0.5:
            yv = np.array([refmap[lab] for lab in c.phases_used])
            loose = any(lab in unsettled for lab in c.phases_used)
            f, o, r, r0, nice = _nice_scale(c.medians, yv, np.sqrt(c.ns), c.a, c.b, loose=loose)
            c.nice = nice
            if nice:
                c.rank = c.score + 0.01
    best = _dedupe(fi, all_c, top)
    results = []
    for c in best:
        yv = np.array([refmap[lab] for lab in c.phases_used])
        w = np.sqrt(c.ns)
        c.unsettled = [lab for lab in c.phases_used if lab in unsettled or lab in c.unsettled]
        f, o, r, r0, nice = _nice_scale(c.medians, yv, w, c.a, c.b, loose=bool(c.unsettled))
        c.factor, c.offset, c.rms, c.rms_fit = f, o, r, r0
        sp = c.spec()
        fw = sp.firmware_equivalent()
        resid = []
        for lab, ref, med, sd, nn in zip(c.phases_used, yv, c.medians, c.spreads, c.ns):
            val = med * f + o
            resid.append({"phase": lab, "ref": float(ref), "raw_median": float(med), "value": float(val),
                          "diff": float(val - ref), "spread": float(abs(f) * sd), "n": int(nn),
                          "unsettled": lab in c.unsettled})
        results.append({"spec": str(sp), "stream": c.stream.name, "score": round(c.score, 4),
                        "rank": round(c.rank, 4),     # по нему упорядочен список: score (+0.01 за «красивый» множитель)
                        "r2": round(c.r2, 5), "spread_ratio": round(c.ratio, 4), "monotonic": round(c.mono, 3),
                        "factor": f, "offset": o, "factor_fit": c.a, "offset_fit": c.b,
                        "rms": round(r, 3), "rms_fit": round(r0, 3), "nice_factor": bool(nice),
                        "resolution": c.res, "time_mono": c.time_mono, "timer": c.timer,
                        "unsettled": list(c.unsettled),
                        "firmware": str(fw) if fw is not None else None,
                        "residuals": resid,
                        "variants": [{"spec": str(v.spec(False)), "score": round(v.score, 4),
                                      "factor_fit": v.a, "offset_fit": v.b, "rms_fit": round(v.rmsf, 3)}
                                     for v in c.variants],
                        "alt_bounds": [str(v.spec(False)) for v in c.alt]})
    return {"groups": [{"phase": g[0], "ref": g[1], "intervals": len(g[2])} for g in groups],
            "results": results, "warnings": warn, "candidates": n_eval,
            "ref_time_monotonic": bool(ref_time_mono), "skip": skip, "skip_report": skip_report(groups, skip)}


# ---------------------------------------------------------------------------
# corr
# ---------------------------------------------------------------------------
def _grid_idx(t, grid, max_age):
    idx = np.searchsorted(t, grid, "right") - 1
    ok = idx >= 0
    idxc = np.clip(idx, 0, max(0, len(t) - 1))
    if len(t):
        ok &= (grid - t[idxc]) <= max_age
    return idxc, ok


def corr(sess, spec, streams, step=0.05, top=15, max_age=1.0):
    s0, t0, v0 = field_series(sess, spec)
    a = max(sess.t_first, t0[0])
    b = min(sess.t_last, t0[-1])
    if b - a < 5 * step:
        raise AnalysisError("слишком короткая запись для корреляции")
    grid = np.arange(a, b, step)
    i0, ok0 = _grid_idx(t0, grid, max_age)
    y = v0[i0]
    ok0 &= ~np.isnan(y)
    out = []
    own = spec.bitset()
    for s in streams:
        Lm = s.len_mode()
        if Lm == 0 or s.n < 5:
            continue
        ex = excluded_bits(sess, s)
        idx, ok = _grid_idx(s.t, grid, max(max_age, 3 * (s.period() or 0)))
        m = ok & ok0
        if m.sum() < 20:
            continue
        D = s.data[idx[m], :Lm].astype(np.int64)
        yy = y[m]
        ys = yy - yy.mean()
        ysd = math.sqrt(float((ys * ys).sum()))
        if ysd == 0:
            raise AnalysisError("поле %s постоянно — коррелировать нечего" % spec)
        cands = []
        for i in range(Lm):
            cands.append((i, 0, 8, "le", D[:, i]))
        for i in range(Lm - 1):
            cands.append((i, 0, 16, "le", D[:, i] | (D[:, i + 1] << 8)))
            cands.append((i, 7, 16, "be", (D[:, i] << 8) | D[:, i + 1]))
        bits = np.unpackbits(D.astype(np.uint8), axis=1, bitorder="little")
        for q in range(Lm * 8):
            cands.append((q // 8, q % 8, 1, "le", bits[:, q].astype(np.int64)))
        for byte, bit, ln, order, u in cands:
            bs = _bitset(byte, bit, ln, order)
            if bs & ex:
                continue
            if s.key == s0.key and (bs & own):
                continue
            x = u.astype(np.float64)
            xs = x - x.mean()
            xsd = math.sqrt(float((xs * xs).sum()))
            if xsd == 0:
                continue
            r = float((xs * ys).sum() / (xsd * ysd))
            out.append({"spec": str(FieldSpec(s.ch, s.can_id, byte, bit, ln, order, ext=s.ext)),
                        "stream": s.name, "r": round(r, 4), "len": ln, "_bits": bs, "_key": s.key})
    out.sort(key=lambda r: -abs(r["r"]))
    kept = []
    for r in out:
        if any(k["_key"] == r["_key"] and (k["_bits"] & r["_bits"]) for k in kept):
            continue
        kept.append(r)
        if len(kept) >= top:
            break
    for r in kept:
        del r["_bits"], r["_key"]
    return {"field": str(spec), "step": step, "points": int(ok0.sum()), "results": kept}
