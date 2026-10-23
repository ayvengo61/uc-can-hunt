# -*- coding: utf-8 -*-
"""Командная строка canhunt.py (SPEC §10). Вывод — по-русски, --json — машинный."""
import argparse
import json
import os
import sys
import time

import numpy as np

from . import analysis as A
from .fieldspec import SpecError, fmt_id, fmt_num, parse_spec
from .loader import LoadError, fmt_epoch, load_session

EPILOG = """Примеры:
  canhunt.py info latest
  canhunt.py overview latest --ch 0
  canhunt.py compare latest --a nobrake --b brake
  canhunt.py marks latest --label дверь --window 1.0
  canhunt.py marks latest --phase btn_volup              (только отметки внутри фазы btn_volup)
  canhunt.py track latest --ref off=0 --ref idle=800 --ref rpm1500=1500 --ref-inputs   (ввод уточняет свою фазу)
  canhunt.py track latest --ref idle=800 --ref rpm3000=3000 --ref rpm1500=1500 --ref rpm2500=2500
  canhunt.py track latest --ref-names --ref idle=800     (эталон из имён фаз rpmNNNN)
  canhunt.py track latest --ref-inputs                   (эталон из введённых пользователем чисел)
  canhunt.py corr latest --field 0:0C9:1.7:16:be*0.25
  canhunt.py plot latest --field 0:0C9:1.7:16:be*0.25 --field 0:2A0:1.4:1:le
  canhunt.py plot latest --id 0:0C9
  canhunt.py decode latest --field 0:0C9:1.7:16:be*0.25 --csv rpm.csv
  canhunt.py export --field 0:1F5:0.0:16:le*0.01 --name СКОРОСТЬ --unit км/ч
  canhunt.py export latest --field 0:1F5:0.0:16:le*0.01 --name СКОРОСТЬ   (длина и период кадра — из записи)
  canhunt.py uds DRL.trc                                 (разбор диагностики AUTEL: блоки, сервисы, DID, лента)
  canhunt.py uds latest --ecu 70 --service 22            (только блок 0x70 и сервис ReadDataByIdentifier)
  canhunt.py uds-did latest --ecu 70                     (дамп конфигурации блока 0x70)
  canhunt.py uds-diff есть.trc нет.trc --ecu 70          (что отличается: кандидат в искомый конфиг-бит)
Источник: папка сессии, frames.log (candump), .trc (PCAN 1.x/2.x или CAN-Hacker) или latest
(последняя сессия: --sessions DIR, иначе спросить монитор GET /session → last_dir)."""


class CliError(Exception):
    pass


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    return str(o)


def _clean(o):
    """NaN/inf → None для корректного JSON."""
    if isinstance(o, float) or isinstance(o, np.floating):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def emit_json(obj):
    sys.stdout.write(json.dumps(_clean(obj), ensure_ascii=False, indent=1, default=_json_default) + "\n")


# ---------------------------------------------------------------------------
def parse_ids(vals):
    """--id: «0C9», «0x0C9», «0:0C9», через запятую; несколько раз. ID из 8 цифр — только 29-битный
    (SPEC §2), короче — любой с этим числом. Возвращает [(канал|None, ID, 29-бит? True|None)]."""
    if not vals:
        return None
    out = []
    for v in vals:
        for tok in v.replace(";", ",").split(","):
            tok = tok.strip()
            if not tok:
                continue
            ch = None
            try:
                if ":" in tok:
                    a, b = tok.split(":", 1)
                    ch = int(a)
                    tok = b
                if tok.lower().startswith("0x"):
                    tok = tok[2:]
                out.append((ch, int(tok, 16), True if len(tok) == 8 else None))
            except ValueError:
                raise CliError("неверный ID: %s" % v)
    return out


def parse_chs(v):
    if v is None:
        return None
    try:
        return set(int(x) for x in str(v).replace(";", ",").split(",") if x.strip() != "")
    except ValueError:
        raise CliError("неверный --ch: %s" % v)


def parse_refs(vals):
    refs = {}
    for v in vals or []:
        for tok in v.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if "=" not in tok:
                raise CliError("эталон пишется как ФАЗА=ЗНАЧЕНИЕ: %s" % tok)
            k, x = tok.rsplit("=", 1)
            try:
                refs[k.strip()] = float(x.replace(",", "."))
            except ValueError:
                raise CliError("эталон не число: %s" % tok)
    return refs


def rel(sess, t):
    return t - sess.t0


def ftime(sess, t):
    return "%+.1f" % rel(sess, t)


def get_session(args):
    sess = load_session(args.src, sessions=args.sessions, port=args.port, use_cache=not args.no_cache)
    for w in sess.warnings:
        sys.stderr.write("Внимание: %s\n" % w)
    return sess


NO_FRAMES = ("в записи нет кадров CAN — сниффер был выключен (или выключился сам) или нет связи с шиной. "
             "Проверьте car.py status (сниффер ВКЛ, кадр/с > 0) и запишите заново")


def streams_of(sess, args, need=False):
    """Потоки по фильтрам --ch/--id. need=True — для анализа: пустая запись или пустой выбор — ошибка
    (иначе «не найдено» с кодом 0 легко принять за «сигнала нет в машине»)."""
    st = sess.select(parse_chs(args.ch), parse_ids(args.id))
    if need:
        if sess.n_frames == 0:
            raise CliError(NO_FRAMES)
        if not st:
            raise CliError("на выбранных каналах/ID (--ch %s, --id %s) кадров нет" % (
                args.ch or "все", ",".join(args.id) if args.id else "все"))
    return st


def frame_gaps(sess, streams, min_gap=1.0):
    """Паузы в кадрах выбранных потоков длиннее min_gap (в том числе в начале и в конце записи)."""
    ts = [s.t for s in streams if s.n]
    if not ts:
        return []
    t = np.sort(np.concatenate(ts))
    out = []
    if t[0] - sess.t0 > 2.0:
        out.append((sess.t0, float(t[0])))
    d = np.diff(t)
    for i in np.nonzero(d > min_gap)[0]:
        out.append((float(t[i]), float(t[i + 1])))
    if sess.t_end - t[-1] > min_gap:
        out.append((float(t[-1]), float(sess.t_end)))
    return out


def phase_frames(sess, streams, iv):
    """Число кадров выбранных потоков внутри отрезка фазы."""
    return int(sum(int(np.searchsorted(s.t, iv.t1, "left") - np.searchsorted(s.t, iv.t0, "left"))
                   for s in streams if s.n))


def ambiguous_note(sess):
    amb = sess.ambiguous_ids()
    if not amb:
        return None
    return ("ID %s есть и 11-битный, и 29-битный — это разные сообщения; в spec 29-битный пишите 8 цифрами "
            "(например %d:%08X)" % (", ".join("%d:%03X" % x for x in amb), amb[0][0], amb[0][1]))


def phase_problems(sess, streams, labels=None):
    """Предупреждения: фазы без кадров (связь с контроллером, сниффер) и паузы в кадрах внутри фаз."""
    out = []
    gaps = frame_gaps(sess, streams)
    for p in sess.phases:
        if labels is not None and p.label not in labels:
            continue
        n = phase_frames(sess, streams, p)
        if n == 0:
            out.append("в фазе «%s» (с %s с, %.1f с) нет ни одного кадра — связь с контроллером или сниффер?"
                       % (p.label, ftime(sess, p.t0), p.dur))
            continue
        for a, b in gaps:
            x, y = max(a, p.t0), min(b, p.t1)
            if y - x > 1.0:
                out.append("в фазе «%s» пауза в кадрах %.1f с (с %s до %s с) — связь с контроллером или сниффер?"
                           % (p.label, y - x, ftime(sess, x), ftime(sess, y)))
    return out


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------
def cmd_info(args):
    sess = get_session(args)
    streams = streams_of(sess, args)
    per_ch = {}
    ids_ch = {}
    rem = 0
    for s in sess.streams.values():
        rem += s.n_remote
    for s in streams:
        per_ch[s.ch] = per_ch.get(s.ch, 0) + s.n
        ids_ch[s.ch] = ids_ch.get(s.ch, 0) + 1
    ph = [{"label": p.label, "start": round(rel(sess, p.t0), 3), "duration": round(p.dur, 3), "by": p.by,
           "frames": phase_frames(sess, streams, p)}
          for p in sess.phases]
    gaps = frame_gaps(sess, streams)
    warns = []
    if sess.n_frames == 0:
        warns.append(NO_FRAMES)
    elif not streams:
        warns.append("на выбранных каналах/ID кадров нет")
    else:
        if gaps:
            warns.append("паузы в кадрах дольше 1 с: %s — связь с контроллером или сниффер? Отметки и фазы в эти "
                         "отрезки анализ не увидит" % ", ".join(
                             "с %s до %s с (%.1f с)" % (ftime(sess, a), ftime(sess, b), b - a) for a, b in gaps[:6]))
        warns += phase_problems(sess, streams)
    amb = ambiguous_note(sess)
    if amb:
        warns.append(amb)
    res = {"source": sess.frames_path, "dir": sess.dir, "format": sess.kind, "meta": sess.meta,
           "parse_seconds": round(sess.parse_time, 3), "from_cache": sess.from_cache,
           "start": sess.t0 if sess.epoch else None, "duration": round(sess.duration, 3),
           "frames": sum(per_ch.values()), "frames_by_channel": {str(k): v for k, v in sorted(per_ch.items())},
           "ids": len(streams), "ids_by_channel": {str(k): v for k, v in sorted(ids_ch.items())},
           "remote_frames": rem, "phases": ph,
           "marks": [{"t": round(rel(sess, m["t"]), 3), "label": m["label"], "by": m["by"]} for m in sess.marks],
           "inputs": [{"t": round(rel(sess, i["t"]), 3), "value": i["raw"], "number": i["value"], "phase": i["phase"],
                       "label": i["label"]} for i in sess.inputs],
           "notes": [{"t": round(rel(sess, n["t"]), 3), "text": n["text"], "by": n["by"]} for n in sess.notes],
           "events": len(sess.events), "stat": sess.stat, "live": sess.live,
           "frame_gaps": [{"from": round(rel(sess, a), 3), "to": round(rel(sess, b), 3), "duration": round(b - a, 3)}
                          for a, b in gaps],
           "warnings": warns}
    if args.json:
        return emit_json(res)
    m = sess.meta or {}
    print("Сессия: %s" % (sess.dir if sess.kind == "candump" else sess.frames_path))
    if m:
        print("        плата %s, имя «%s»" % (m.get("board", "?"), m.get("name", "")))
    fmt_name = {"candump": "candump", "pcan": "PCAN .trc %s" % (sess.stat.get("version") or ""),
                "canhacker": "CAN-Hacker .trc"}.get(sess.kind, sess.kind)
    print("Файл:   %s (%s), разобран за %.2f с%s" % (os.path.basename(sess.frames_path), fmt_name, sess.parse_time,
                                                  " [из кэша]" if sess.from_cache else ""))
    if sess.live:
        print("Запись ещё идёт (meta.stopped = null): данные — на момент чтения")
    if sess.epoch:
        print("Начало: %s, длительность %.1f с" % (fmt_epoch(sess.t0), sess.duration))
    else:
        print("Длительность: %.1f с (время .trc — смещение от начала записи)" % sess.duration)
    chs = ", ".join("канал %s: %d" % (k, v) for k, v in sorted(per_ch.items()))
    print("Кадров: %d (%s); ID: %d%s" % (sum(per_ch.values()), chs or "—", len(streams),
                                        ("; remote: %d" % rem) if rem else ""))
    if sess.stat.get("bad"):
        print("Непонятых строк: %d" % sess.stat["bad"])
    if sess.stat.get("header_frames") is not None:
        print("В заголовке CAN-Hacker указано кадров: %d" % sess.stat["header_frames"])
    if ph:
        print("Фазы (%d):" % len(ph))
        for p, x in zip(sess.phases, ph):
            print("   %-16s с %8s с   длит. %6.1f с   кадров %d%s" % (
                p.label, ftime(sess, p.t0), p.dur, x["frames"],
                "   (шла уже в момент старта записи)" if p.by == "session_start" else ""))
    else:
        print("Фаз нет.")
    if sess.marks:
        print("Отметки (%d): %s" % (len(sess.marks), ", ".join(
            "%s «%s»" % (ftime(sess, m["t"]), m["label"]) for m in sess.marks)))
    if sess.inputs:
        print("Ввод (%d):" % len(sess.inputs))
        for i in sess.inputs:
            print("   %8s с  «%s»  фаза: %s%s" % (ftime(sess, i["t"]), i["raw"], i["phase"] or "—",
                                               ("  — " + i["label"]) if i["label"] else ""))
    if sess.notes:
        print("Сообщения (%d):" % len(sess.notes))
        for n in sess.notes[-10:]:
            print("   %8s с  [%s] %s" % (ftime(sess, n["t"]), n["by"], n["text"]))
    for w in warns:
        print("ВНИМАНИЕ: %s" % w)


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------
LEGEND = ("Легенда: XX — константа (hex); сч — счётчик; с4 — счётчик в тетраде (с4+ — вторая тетрада меняется);\n"
          "         кс — контрольная сумма (проверена: XOR/сумма/CRC-8); ш? — шум или CRC (не проверено);\n"
          "         сг — сигнал; фл — флаги (мало бит, мало значений); -- — нет данных")


def _group_noise(details):
    out = []
    for b, kind, text in details:
        if out and kind == "noise" and out[-1][1] == "noise" and out[-1][3] == b - 1:
            out[-1][3] = b
            continue
        # подряд идущие байты с одинаковым описанием (64-байтный FD-кадр из счётчиков) — одной строкой
        if out and kind == out[-1][1] and text == out[-1][2] and out[-1][3] == b - 1:
            out[-1][3] = b
            continue
        out.append([b, kind, text, b])
    return out


def cmd_overview(args):
    sess = get_session(args)
    streams = streams_of(sess, args)
    res = []
    for s in streams:
        c = sess.classification(s)
        res.append({"id": s.name, "ch": s.ch, "can_id": s.can_id, "ext": s.ext, "fd": s.fd, "frames": s.n,
                     "rate_hz": round(s.rate(), 2), "len": s.len_mode(), "len_min": int(s.dlen.min()) if s.n else 0,
                     "len_max": int(s.dlen.max()) if s.n else 0, "remote": s.n_remote,
                     "mask": " ".join("%02X" % m for m in c["mask"]),
                     "bytes": [{"i": b["i"], "kind": b["kind"], "code": b["code"], "text": b["text"],
                                "value": b["value"], "change_rate": b["chg"], "distinct": b["distinct"],
                                "entropy": b["entropy"]} for b in c["bytes"]]})
    if args.json:
        return emit_json({"streams": res})
    print("%-12s %7s %4s %8s  %s" % ("ID", "Гц", "дл", "кадров", "байты 0.. (коды)  |  маска меняющихся бит (hex)"))
    for s, r in zip(streams, res):
        c = sess.classification(s)
        codes = [b["code"] for b in c["bytes"]]
        mask = ["%02X" % m for m in c["mask"]]
        L = len(codes)
        lens = str(r["len"]) if r["len_min"] == r["len_max"] else "%d-%d" % (r["len_min"], r["len_max"])
        flag = (" FD" if s.fd else "") + ((" R%d" % s.n_remote) if s.n_remote else "")
        head = "%-12s %7.1f %4s %8d  " % (s.name + flag, r["rate_hz"], lens, s.n)
        if L == 0:
            print(head + "(нет данных)")
            continue
        for off in range(0, L, 16):
            cs = " ".join("%-3s" % x for x in codes[off:off + 16])
            ms = " ".join(mask[off:off + 16])
            pre = head if off == 0 else " " * len(head)
            tag = "" if L <= 16 else "[%2d] " % off
            print(pre + tag + cs.ljust(16 * 4) + "| " + ms)
        det = [(b["i"], b["kind"], b["text"]) for b in c["bytes"]
               if b["kind"] in ("counter", "checksum", "noise") or "16-бит" in b["text"]]
        for b0, kind, text, b1 in _group_noise(det):
            nm = ("б%d" % b0) if b0 == b1 else ("б%d-%d" % (b0, b1))
            if kind == "noise" and b0 != b1:
                text = "шум/CRC? (высокая энтропия, меняются почти в каждом кадре)"
            print("      %s: %s" % (nm, text))
    print(LEGEND)
    amb = ambiguous_note(sess)
    if amb:
        print("ВНИМАНИЕ: %s" % amb)
    if sess.n_frames == 0:
        print("ВНИМАНИЕ: %s" % NO_FRAMES)


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------
def _labels(v):
    out = []
    for x in v or []:
        out += [t.strip() for t in x.split(",") if t.strip()]
    return out


def cmd_compare(args):
    sess = get_session(args)
    a, b = _labels(args.a), _labels(args.b)
    if not a or not b:
        raise CliError("нужно --a ФАЗА[,ФАЗА] и --b ФАЗА[,ФАЗА]")
    streams = streams_of(sess, args, need=True)
    r = A.compare(sess, a, b, streams, skip=args.skip, thr=args.thr, top=args.top)
    r["warnings"] = phase_problems(sess, streams, set(a) | set(b))
    if args.json:
        return emit_json(r)
    sk = "начало фаз (%.1f с) отброшено" % args.skip
    if r["skip_min"] < args.skip - 1e-9:
        sk += " (в коротких фазах — половина фазы, от %.1f с)" % r["skip_min"]
    print("Сравнение: A = %s (%d интерв.), B = %s (%d интерв.); %s; счётчики и CRC исключены"
          % (",".join(a), r["a_intervals"], ",".join(b), r["b_intervals"], sk))
    for w in r["warnings"]:
        print("ВНИМАНИЕ: %s" % w)
    print("Биты, стабильные внутри A и B, но разные между ними (порог %.0f%%):" % (args.thr * 100))
    if not r["bits"]:
        print("   — не найдено")
    for x in r["bits"]:
        print("   %-22s A=%d (%5.1f%%)  B=%d (%5.1f%%)   переключений A/B: %d/%d%s" % (
            x["spec"], x["a_value"], x["a_pct"], x["b_value"], x["b_pct"], x["a_toggles"], x["b_toggles"],
            "   (в «шумном» байте)" if x.get("noisy_byte") else ""))
    if r["bits_total"] > len(r["bits"]):
        print("   … всего %d" % r["bits_total"])
    if r["blink"]:
        print("Мигающие биты (регулярно переключаются в одной группе, стоят в другой):")
        for x in r["blink"]:
            print("   %-22s мигает в %s ≈ %.2f Гц, в другой группе = %d" % (x["spec"], x["group"], x["hz"], x["other_value"]))
    print("Поля 8/16 бит с разделёнными распределениями (5..95 %):")
    if not r["fields"]:
        print("   — не найдено")
    for x in r["fields"]:
        print("   %-22s A: медиана %-8s [%s..%s]   B: медиана %-8s [%s..%s]   %s d=%.1f" % (
            x["spec"], fmt_num(x["a_median"]), fmt_num(x["a_range"][0]), fmt_num(x["a_range"][1]),
            fmt_num(x["b_median"]), fmt_num(x["b_range"][0]), fmt_num(x["b_range"][1]),
            "разделены," if x["separated"] else "пересекаются,", x["d"]))


# ---------------------------------------------------------------------------
# marks
# ---------------------------------------------------------------------------
def _lag_text(lag):
    if abs(lag) < 0.25:
        return None
    if lag < 0:
        return "изменения в среднем за %.1f с ДО отметки — отметку нажимали после действия" % -lag
    return "изменения в среднем через %.1f с после отметки — действие выполняли с задержкой" % lag


def cmd_marks(args):
    sess = get_session(args)
    streams = streams_of(sess, args, need=True)
    r = A.marks_analysis(sess, streams, label=args.label, window=args.window,
                         before=args.before, top=args.top, phase=args.phase, lag=args.lag)
    r["warnings"] = phase_problems(sess, streams, {args.phase} if args.phase else None)
    if args.json:
        return emit_json(r)
    what = ((" с меткой «%s»" % args.label) if args.label else "") + ((" в фазе «%s»" % args.phase) if args.phase else "")
    print("Отметок: %d%s; окно [−%.1f; +%.1f] с%s; счётчики/CRC и часто меняющиеся биты исключены" % (
        r["marks"], what, args.before, args.window,
        (", сдвиг окна подбирается в пределах ±%.1f с" % args.lag) if args.lag > 0 else ""))
    print("Окна покрывают %d%% %s: случайно меняющийся бит попадал бы в окна примерно в %d%% случаев "
          "(оценка учитывает это; p — вероятность случайного совпадения)" % (
              round(100 * r["coverage"]), "фазы" if args.phase else "записи", round(100 * r["coverage"])))
    for w in r["warnings"]:
        print("ВНИМАНИЕ: %s" % w)
    if not r["bits"]:
        print("   — ни один бит не меняется у отметок")
        return
    if not r["found"]:
        b0 = r["bits"][0]
        if b0["score"] >= 0.8:
            print("Устойчивой реакции на отметки не найдено: лучший бит %s совпал с отметками, но окна покрывают "
                  "%d%% записи, и такое совпадение возможно случайно (p=%.2f). Повторите действие 5+ раз или "
                  "уменьшите --window." % (b0["spec"], round(100 * r["coverage"]), b0.get("p_adj") or 1.0))
        else:
            print("Устойчивой реакции на отметки не найдено: биты ниже могли совпасть с отметками случайно "
                  "(оценка < 0.3 или p ≥ 0.05). Проверьте шину/сниффер или повторите: больше отметок, пауза 3–5 с.")
    print("   %-22s %9s %8s %8s %8s %7s %6s %8s  %s" % ("бит", "попаданий", "в окнах", "вне окон", "точность",
                                                     "оценка", "сдвиг", "p", "значение до→после у отметок"))
    for x in r["bits"]:
        pat = " ".join(x["pattern"][:12]) + (" …" if len(x["pattern"]) > 12 else "")
        p = x.get("p_adj")
        ps = "—" if p is None else ("<0.001" if p < 0.001 else "%.3f" % p)
        print("   %-22s %5d/%-3d %8d %8d %8.2f %7.2f %+6.1f %8s  %s" % (
            x["spec"], x["hits"], x["marks"], x["inside"], x["outside"], x["precision"], x["score"], x["lag"],
            ps, pat))
    top = r["bits"][0]
    lt = _lag_text(top["lag"])
    if r["found"] and lt:
        print("   %s: %s" % (top["spec"], lt))


# ---------------------------------------------------------------------------
# track
# ---------------------------------------------------------------------------
def _refs_from_names(sess):
    import re
    out = {}
    for lab in sess.phase_labels():
        m = re.search(r"(-?\d+(?:[.,]\d+)?)\s*$", lab)
        if m:
            out[lab] = float(m.group(1).replace(",", "."))
    return out


def cmd_track(args):
    # «track --ref a=1 b=2 ПАПКА»: источник, записанный после списка эталонов, попадает в --ref — вернуть его на место
    if args.ref:
        loose = [v for v in args.ref if "=" not in v]
        if loose:
            if len(loose) > 1 or args.src != "latest":
                raise CliError("эталон пишется как ФАЗА=ЗНАЧЕНИЕ: %s" % ", ".join(loose))
            args.src = loose[0]
            args.ref = [v for v in args.ref if "=" in v]
    sess = get_session(args)
    refs = {}
    if args.ref_names:
        refs.update(_refs_from_names(sess))
    refs.update(parse_refs(args.ref))
    if not refs and not args.ref_inputs:
        raise CliError("нужен эталон: --ref ФАЗА=ЗНАЧЕНИЕ (несколько), --ref-names или --ref-inputs")
    groups, warn = A.build_ref_groups(sess, refs, args.ref_inputs)
    if len([g for g in groups if g[2]]) < 2:
        raise CliError("; ".join(warn + ["для track нужно хотя бы 2 фазы с эталоном (лучше ≥3 с разными значениями)"]))
    streams = streams_of(sess, args, need=True)
    t = time.time()
    r = A.track(sess, streams, groups, skip=args.skip, top=args.top)
    labels = set(g[0] for g in groups)
    r["warnings"] = warn + phase_problems(sess, streams, labels) + r["warnings"]
    r["seconds"] = round(time.time() - t, 3)
    if args.json:
        return emit_json(r)
    print("Эталон: %s" % ", ".join("%s=%s" % (g["phase"], fmt_num(g["ref"])) for g in r["groups"]))
    print("Проверено полей: %d за %.1f с; начало каждой фазы (%.1f с) отброшено, уровень фазы — медиана второй "
          "половины остатка" % (r["candidates"], r["seconds"], args.skip))
    capped = [x for x in r.get("skip_report", []) if x["capped"]]
    if capped:
        print("  фазы короче %.1f с — отброшена только половина: %s" % (2 * args.skip, ", ".join(
            "%s %.1f с → %.1f с" % (x["phase"], x["duration"], x["skipped"]) for x in capped[:8])))
    for w in r["warnings"]:
        print("ВНИМАНИЕ: %s" % w)
    if not r["results"]:
        print("Ничего похожего не найдено.")
        return
    for k, x in enumerate(r["results"], 1):
        print("")
        # место в списке — по rank: при почти равной оценке выше поле с «красивым» множителем (+0.01)
        bonus = x.get("rank", x["score"]) - x["score"]
        print("#%d  %-30s оценка %.3f%s   R²=%.5f   разброс %.1f%%   монотонность %.2f%s" % (
            k, x["spec"], x["score"], (" (+%.2f за «красивый» множитель)" % bonus) if bonus > 1e-6 else "",
            x["r2"], x["spread_ratio"] * 100, x["monotonic"],
            "   (слабое совпадение)" if x["score"] < 0.5 else ""))
        if x.get("timer"):
            print("    ВНИМАНИЕ: растёт со временем и внутри фаз — похоже на таймер/счётчик, а не на сигнал")
        elif x.get("time_mono") and r.get("ref_time_monotonic"):
            print("    монотонно по времени, как и эталон: таймер не исключён — проверьте фазами в другом порядке")
        print("    подгонка: ×%s %+.4g (СКО %.1f);  %s: ×%s %s (СКО %.1f);  шаг %s" % (
            "%.6g" % x["factor_fit"], x["offset_fit"], x["rms_fit"],
            "округлено" if x["nice_factor"] else "не округляется", fmt_num(x["factor"]),
            ("%+g" % x["offset"]) if x["offset"] else "+0", x["rms"], "%.4g" % x.get("resolution", 0)))
        print("    %-14s %9s %12s %10s %8s %9s" % ("фаза", "эталон", "сырое(мед)", "значение", "откл.", "разброс"))
        for p in x["residuals"]:
            print("    %-14s %9s %12s %10.1f %+8.1f %9s%s" % (
                p["phase"][:14], fmt_num(p["ref"]), fmt_num(round(p["raw_median"], 2)), p["value"], p["diff"],
                "±" + fmt_num(round(p["spread"], 1)), "  *" if p.get("unsettled") else ""))
        if any(p.get("unsettled") for p in x["residuals"]):
            print("    * — в начале фазы значение ещё менялось")
        if x.get("alt_bounds"):
            print("    граница поля неоднозначна — то же значение дают и %s (лишние биты в фазах не менялись)"
                  % ", ".join(x["alt_bounds"][:3]))
        if x["variants"]:
            print("    варианты той же области: %s" % ", ".join(
                "%s ×%.4g%+.4g (СКО %s, оценка %.3f)" % (v["spec"], v["factor_fit"], v["offset_fit"],
                                                          fmt_num(round(v["rms_fit"], 1)), v["score"])
                for v in x["variants"]))
        if x["firmware"] is None:
            print("    прошивка: это be-поле длиннее байта — прошивка UniversalController его не читает (только le)")
        elif x["firmware"] != x["spec"]:
            print("    для прошивки: %s" % x["firmware"])
        try:
            sp = parse_spec(x["spec"])
            if sp.ext and sp.can_id <= 0x7FF:
                print("    прошивка: 29-битный ID не больше 7FF прошивка UniversalController примет за 11-битный")
        except SpecError:
            pass


# ---------------------------------------------------------------------------
# corr
# ---------------------------------------------------------------------------
def cmd_corr(args):
    sess = get_session(args)
    spec = parse_spec(args.field)
    r = A.corr(sess, spec, streams_of(sess, args, need=True), step=args.step, top=args.top)
    if args.json:
        return emit_json(r)
    print("Корреляция с %s (сетка %.0f мс, %d точек); счётчики/CRC/шум исключены:" % (r["field"], args.step * 1000, r["points"]))
    for x in r["results"]:
        print("   %-24s r = %+.3f" % (x["spec"], x["r"]))


# ---------------------------------------------------------------------------
# decode
# ---------------------------------------------------------------------------
def cmd_decode(args):
    sess = get_session(args)
    spec = parse_spec(args.field)
    r = A.decode_stats(sess, spec)
    t, v, s = r.pop("t"), r.pop("v"), r.pop("stream_obj")
    if args.csv:
        from .fieldspec import decode_raw
        raw, ok = decode_raw(s.data, s.dlen, spec)
        ph = np.array([""] * len(t), dtype=object)
        for iv in sess.phases:
            i0, i1 = A.interval_rows(t, iv.t0, iv.t1)
            ph[i0:i1] = iv.label
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
            f.write("t_rel;t;raw;value;phase\n")
            for i in range(len(t)):
                if not ok[i]:
                    continue
                f.write("%.6f;%.6f;%d;%s;%s\n" % (t[i] - sess.t0, t[i], raw[i], repr(float(v[i])), ph[i]))
        r["csv"] = os.path.abspath(args.csv)
    if args.json:
        return emit_json(r)
    print("Поле %s: кадров %d, определено %d" % (r["field"], r["frames"], r["valid"]))
    print("   %-16s %7s %10s %10s %10s %10s %9s" % ("фаза", "кадров", "мин", "медиана", "среднее", "макс", "СКО"))
    for p in r["phases"]:
        if not p["n"]:
            print("   %-16s %7d" % (p["phase"][:16], 0))
            continue
        print("   %-16s %7d %10s %10s %10s %10s %9s" % (
            p["phase"][:16], p["n"], fmt_num(round(p["min"], 4)), fmt_num(round(p["median"], 4)),
            fmt_num(round(p["mean"], 3)), fmt_num(round(p["max"], 4)), fmt_num(round(p["std"], 3))))
    if args.csv:
        print("CSV: %s" % r["csv"])


# ---------------------------------------------------------------------------
# plot
# ---------------------------------------------------------------------------
def cmd_plot(args):
    from . import plotting as P
    sess = get_session(args)
    outs = []
    if args.field:
        items = []
        for f in args.field:
            name = ""
            if "=" in f and not f.split("=", 1)[0].count(":") >= 3:
                name, f = f.split("=", 1)
            items.append((parse_spec(f), name))
        out = args.out or P.default_out(sess, "field_" + P._safe("+".join(sp.raw_str().replace(":", "-") for sp, _ in items)))
        outs.append(P.plot_fields(sess, items, out, width=args.width))
    elif args.id:
        ids = parse_ids(args.id)
        streams = sess.select(parse_chs(args.ch), ids)
        if not streams:
            raise CliError("в сессии нет таких ID: %s" % ", ".join(args.id))
        for k, s in enumerate(streams):
            codes = [b["code"] for b in sess.classification(s)["bytes"]]
            if args.out and len(streams) == 1:
                out = args.out
            elif args.out:
                root, ext = os.path.splitext(args.out)
                out = "%s_%d-%s%s" % (root, s.ch, fmt_id(s.can_id, s.ext), ext or ".png")
            else:
                out = P.default_out(sess, "id_%d-%s" % (s.ch, fmt_id(s.can_id, s.ext)))
            outs.append(P.plot_id(sess, s, out, width=args.width, codes=codes, absolute=args.abs))
    else:
        raise CliError("нужно --field SPEC (можно несколько) или --id CH:ID")
    outs = [os.path.abspath(o) for o in outs]
    if args.json:
        return emit_json({"png": outs})
    for o in outs:
        print(o)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def _frame_info(args, spec):
    """export с источником (canhunt.py export latest --field …): длина и период кадра из записи.
    Возвращает (dlc|None, период_мс|None, предупреждение|None)."""
    sess = get_session(args)
    st = None
    for s in sess.streams.values():
        if s.ch == spec.ch and s.can_id == spec.can_id and s.ext == bool(spec.ext):
            st = s
            break
    if st is None or st.t is None or len(st.t) == 0:
        return None, None, "ID %d:%s в записи нет — длина кадра взята по полю" % (spec.ch, fmt_id(spec.can_id, spec.ext))
    dlc = int(np.max(st.dlen)) if st.dlen is not None and len(st.dlen) else None
    per = None
    if len(st.t) > 2:
        per = float(np.median(np.diff(st.t))) * 1000.0
    return dlc, per, None


def cmd_export(args):
    from .export import export_field, _dlc_for
    spec = parse_spec(args.field)
    dlc, per, fwarn = args.dlc, None, None
    if args.src is not None:
        fdlc, per, fwarn = _frame_info(args, spec)
        if dlc is None and fdlc:
            dlc = _dlc_for(fdlc) if fdlc > 8 else fdlc
    r = export_field(spec, args.name, unit=args.unit or "", dlc=dlc)
    if per is not None:
        r["period_ms"] = round(per, 1)
    if fwarn:
        r["warnings"].append(fwarn)
    if args.json:
        return emit_json(r)
    print("Поле: %s  «%s»" % (r["spec"], r["name"]))
    if per is not None:
        print("Кадр в записи: длина %d байт, период ≈%s мс" % (dlc, fmt_num(round(per, 1))))
    if r["firmware"]:
        print("Прошивка (%s):" % r["firmware_where"])
        print("    " + r["firmware"])
    else:
        print("Прошивка: —")
    print("DBC:")
    print(r["dbc_bo"])
    print(r["dbc_sg"])
    for w in r["warnings"]:
        print("ВНИМАНИЕ: %s" % w)


# ---------------------------------------------------------------------------
# uds / uds-did / uds-diff (SPEC §12) — разбор записанного диагностического обмена (только чтение)
# ---------------------------------------------------------------------------
def _parse_hex(v, what):
    if v is None:
        return None
    s = str(v).strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    try:
        return int(s, 16)
    except ValueError:
        raise CliError("%s: нужно hex-число, получено %r" % (what, v))


def _msg_line(sess, U, m):
    """Одна строка ленты ISO-TP: время, направление, ID, разбор."""
    arrow = {"request": "→", "response": "←", "negative": "←", "flow": "FC", "empty": "·",
             "stray": "·"}.get(m.uds().get("role"), "?")
    if m.kind == "flow":
        fc = m.fc or {}
        body = "flow-control: %s (BS=%s STmin=%s)" % (fc.get("flag_name"), fc.get("bs"), fc.get("stmin"))
        tag = "FC"
    elif m.kind == "incomplete":
        body = "НЕПОЛНОЕ: %s  [%s]" % (U.hexb(m.data), m.note)
        tag = "!!"
    elif m.kind == "stray":
        body = "посторонний CF: %s  [%s]" % (U.hexb(m.data), m.note)
        tag = "cf"
    else:
        u = m.uds()
        body = u.get("summary") or U.hexb(m.data)
        tag = {"single": "SF", "multi": "MF"}.get(m.kind, "??")
        if not m.complete:
            body += "  [%s]" % (m.note or "неполное")
        if not m.sn_ok:
            body += "  [порядок CF нарушен]"
    return "  %+8.3f  %s %-3s %-12s %s" % (rel(sess, m.t0), arrow, tag, m.name, body)


def cmd_uds(args):
    from . import uds as U
    sess = get_session(args)
    chs = parse_chs(args.ch)
    ecu = _parse_hex(args.ecu, "--ecu")
    svc_filter = _parse_hex(args.service, "--service")
    msgs = U.collect_messages(sess, chs)
    blocks = U.build_blocks(msgs)
    blocks = [b for b in blocks if U.block_matches_ecu(b, ecu)]

    def block_json(b):
        s = U.summarize_block(b)
        return {
            "label": U.block_label(b), "ecu_addr": b["ecu_addr"], "scheme": b["scheme"],
            "role_hint": U.role_hint_text(b),
            "req_ids": [U.name_of(k) for k in b["req_ids"]],
            "resp_ids": [U.name_of(k) for k in b["resp_ids"]],
            "services": s["services"], "sessions": s["sessions"],
            "reads": {"0x%04X" % d: {"name": U.did_hint(d), "bytes": U.hexb(e["bytes"]),
                                     "ascii": U.ascii_hint(e["bytes"]), "count": e["count"]}
                      for d, e in sorted(s["reads"].items())},
            "writes": {"0x%04X" % d: {"name": U.did_hint(d), "bytes": U.hexb(e["bytes"]), "count": e["count"]}
                       for d, e in sorted(s["writes"].items())},
            "io": {"0x%04X" % d: {"param": e["param"], "state": U.hexb(e["state"]), "count": e["count"]}
                   for d, e in sorted(s["io"].items())},
            "routines": {"0x%04X" % r: {"subs": sorted(e["subs"]), "count": e["count"],
                                        "last_data": U.hexb(e["last_data"])}
                         for r, e in sorted(s["routines"].items())},
            "negatives": [{"t": round(rel(sess, x["t"]), 3), "service": x["service_name"],
                           "nrc": x["nrc_name"], "id": x["id"]} for x in s["negatives"]],
            "requested_only": ["0x%04X" % d for d in sorted(s["requested"])
                               if d not in s["reads"] and d not in s["writes"]],
        }

    timeline = [m for m in msgs if svc_filter is None or (m.uds().get("service") == svc_filter)]
    if ecu is not None:
        want = set()
        for b in blocks:
            want |= set(b["keys"])
        timeline = [m for m in timeline if m.key in want]

    if args.json:
        return emit_json({
            "source": sess.frames_path, "format": sess.kind,
            "iso_tp_messages": len(msgs), "blocks": [block_json(b) for b in blocks],
            "timeline": [{"t": round(rel(sess, m.t0), 3), "id": m.name, "role": m.uds().get("role"),
                          "kind": m.kind, "data": U.hexb(m.data), "summary": m.uds().get("summary"),
                          "complete": m.complete, "sn_ok": m.sn_ok} for m in timeline]})

    print("Разбор UDS: %s (%s)" % (sess.frames_path, sess.kind))
    print("ISO-TP сообщений: %d; блоков: %d%s" % (
        len(msgs), len(blocks), (" (фильтр --ecu 0x%02X)" % ecu) if ecu is not None else ""))
    if sess.n_frames == 0:
        print("ВНИМАНИЕ: %s" % NO_FRAMES)
    if not blocks:
        print("Диагностического обмена (ISO-TP/UDS) в записи не найдено — либо AUTEL ещё не опрашивал шину, "
              "либо сниффер смотрел не тот канал. Проверьте car.py status и запишите заново.")
        return
    for b in blocks:
        s = U.summarize_block(b)
        req = ", ".join(U.name_of(k) for k in b["req_ids"]) or "—"
        resp = ", ".join(U.name_of(k) for k in b["resp_ids"]) or "—"
        print("")
        print("%s   запрос %s → ответ %s" % (U.block_label(b), req, resp))
        print("   подсказка: %s" % U.role_hint_text(b))
        if s["services"]:
            print("   сервисы: %s" % ", ".join("%s ×%d" % (k, v) for k, v in sorted(s["services"].items())))
        if s["sessions"]:
            print("   сессии:  %s" % ", ".join("%s ×%d" % (k, v) for k, v in sorted(s["sessions"].items())))
        if s["reads"]:
            print("   прочитано (0x22→0x62):")
            for d, e in sorted(s["reads"].items()):
                a = U.ascii_hint(e["bytes"])
                print("      0x%04X %-28s = %s%s  ×%d" % (
                    d, U.did_hint(d), U.hexb(e["bytes"]), (" «%s»" % a) if a else "", e["count"]))
        ronly = [d for d in sorted(s["requested"]) if d not in s["reads"] and d not in s["writes"]]
        if ronly:
            print("   запрошены (0x22, ответ не пойман): %s" % ", ".join("0x%04X" % d for d in ronly))
        if s["writes"]:
            print("   записано (0x2E):")
            for d, e in sorted(s["writes"].items()):
                print("      0x%04X %-28s ← %s  ×%d" % (d, U.did_hint(d), U.hexb(e["bytes"]), e["count"]))
        if s["io"]:
            print("   IO-control (0x2F):")
            for d, e in sorted(s["io"].items()):
                print("      0x%04X %-28s %s состояние %s  ×%d" % (
                    d, U.did_hint(d), e["param"] or "", U.hexb(e["state"]), e["count"]))
        if s["routines"]:
            print("   routine (0x31):")
            for r, e in sorted(s["routines"].items()):
                print("      RID 0x%04X  %s  ×%d%s" % (
                    r, ", ".join(sorted(e["subs"])) or "?", e["count"],
                    ("  данные %s" % U.hexb(e["last_data"])) if e["last_data"] else ""))
        if s["negatives"]:
            print("   негативные ответы (0x7F):")
            for x in s["negatives"]:
                print("      %+8.3f с  %s: %s (%s)" % (
                    rel(sess, x["t"]), x["service_name"], x["nrc_name"], x["id"]))
    incomplete = [m for m in msgs if m.kind == "incomplete"]
    if incomplete:
        print("\nНезавершённых цепочек ISO-TP: %d (помечены в ленте)" % len(incomplete))
    print("\nЛента ISO-TP (время от начала записи, →запрос ←ответ FC=flow-control):")
    LIMIT = 400
    show = timeline if len(timeline) <= LIMIT else timeline[:LIMIT]
    for m in show:
        print(_msg_line(sess, U, m))
    if len(timeline) > LIMIT:
        print("  … показано %d из %d сообщений (полный список — с ключом --json)" % (LIMIT, len(timeline)))


def cmd_uds_did(args):
    from . import uds as U
    sess = get_session(args)
    ecu = _parse_hex(args.ecu, "--ecu")
    msgs = U.collect_messages(sess, parse_chs(args.ch))
    blocks = U.build_blocks(msgs)
    sel = [b for b in blocks if U.block_matches_ecu(b, ecu)]
    if not sel:
        raise CliError("блок --ecu %s в записи не найден. Блоки: %s" % (
            args.ecu, ", ".join(U.block_label(b) for b in blocks) or "—"))
    dumps = []
    for b in sel:
        dump = U.did_dump(b)
        dumps.append({"label": U.block_label(b), "ecu_addr": b["ecu_addr"],
                      "dids": {"0x%04X" % d: {"name": e["name"], "bytes": U.hexb(e["bytes"]),
                                              "ascii": U.ascii_hint(e["bytes"]), "source": e["source"],
                                              "count": e["count"]}
                               for d, e in sorted(dump.items())}})
    if args.json:
        return emit_json({"source": sess.frames_path, "blocks": dumps})
    src_names = {"read": "прочитан (0x62)", "write": "записан (0x2E)", "requested": "только запрос (0x22)"}
    for b in sel:
        dump = U.did_dump(b)
        print("Дамп конфигурации: %s  (%s)" % (U.block_label(b), U.role_hint_text(b)))
        if not dump:
            print("   DID не найдено (в записи нет 0x22/0x2E для этого блока)")
            continue
        print("   %-8s %-30s %-24s %-8s %s" % ("DID", "подсказка", "байты", "источник", "ASCII"))
        for d, e in sorted(dump.items()):
            print("   0x%04X   %-30s %-24s %-8s %s" % (
                d, e["name"][:30], U.hexb(e["bytes"])[:24], src_names.get(e["source"], e["source"])[:8],
                U.ascii_hint(e["bytes"])))


def cmd_uds_diff(args):
    from . import uds as U
    ecu = _parse_hex(args.ecu, "--ecu")

    def load(src):
        return load_session(src, sessions=args.sessions, port=args.port, use_cache=not args.no_cache)

    sa, sb = load(args.a), load(args.b)
    for w in sa.warnings + sb.warnings:
        sys.stderr.write("Внимание: %s\n" % w)

    def dumps_by_ecu(sess):
        out = {}
        for b in U.build_blocks(U.collect_messages(sess)):
            if not U.block_matches_ecu(b, ecu):
                continue
            key = b["ecu_addr"] if b["ecu_addr"] is not None else b["keys"][0]
            out[key] = (U.block_label(b), U.did_dump(b))
        return out

    da, db = dumps_by_ecu(sa), dumps_by_ecu(sb)
    ecus = sorted(set(da) | set(db), key=lambda x: (isinstance(x, tuple), x))
    result = {"a": sa.frames_path, "b": sb.frames_path, "ecus": []}
    for e in ecus:
        in_a, in_b = e in da, e in db
        la, dA = da.get(e, (None, {}))
        lb, dB = db.get(e, (None, {}))
        label = la or lb
        only_a, only_b, changed = [], [], []
        for d in sorted(set(dA) | set(dB)):
            ba = dA.get(d, {}).get("bytes")
            bb = dB.get(d, {}).get("bytes")
            if d in dA and d not in dB:
                only_a.append(d)
            elif d in dB and d not in dA:
                only_b.append(d)
            elif ba != bb:
                # какие байты и биты различаются
                n = max(len(ba), len(bb))
                pa = ba + b"\x00" * (n - len(ba))
                pb = bb + b"\x00" * (n - len(bb))
                bytediff = []
                for i in range(n):
                    if pa[i] != pb[i]:
                        bits = [str(k) for k in range(8) if ((pa[i] ^ pb[i]) >> k) & 1]
                        bytediff.append({"byte": i, "a": pa[i], "b": pb[i], "bits": bits})
                changed.append({"did": d, "name": U.did_hint(d), "a": U.hexb(ba), "b": U.hexb(bb),
                                "bytediff": bytediff})
        result["ecus"].append({"ecu": ("0x%02X" % e) if isinstance(e, int) else U.name_of(e),
                               "label": label, "in_a": in_a, "in_b": in_b,
                               "only_a": ["0x%04X" % d for d in only_a],
                               "only_b": ["0x%04X" % d for d in only_b], "changed": changed})
    if args.json:
        return emit_json(result)
    print("Сравнение дампов DID (сужение области):")
    print("   A = %s" % sa.frames_path)
    print("   B = %s" % sb.frames_path)
    any_change = False
    for eb in result["ecus"]:
        print("\n%s  (%s)%s" % (eb["label"] or eb["ecu"], eb["ecu"],
                                "" if eb["in_a"] and eb["in_b"] else
                                ("  — только в A" if eb["in_a"] else "  — только в B")))
        if eb["only_a"]:
            print("   есть только в A: %s" % ", ".join(eb["only_a"]))
        if eb["only_b"]:
            print("   есть только в B: %s" % ", ".join(eb["only_b"]))
        if not eb["changed"] and not eb["only_a"] and not eb["only_b"]:
            print("   различий в DID нет")
            continue
        for c in eb["changed"]:
            any_change = True
            print("   0x%04X %s:" % (c["did"], c["name"]))
            print("        A: %s" % c["a"])
            print("        B: %s" % c["b"])
            for bd in c["bytediff"]:
                print("        байт %d: %02X→%02X  различаются биты %s  ← кандидат в искомый конфиг-бит"
                      % (bd["byte"], bd["a"], bd["b"], ",".join(bd["bits"])))
    if any_change:
        print("\nБайты/биты выше — то, что изменилось между состояниями: это и есть суженная область поиска.")
    elif not any(eb["changed"] or eb["only_a"] or eb["only_b"] for eb in result["ecus"]):
        print("\nДампы DID совпадают — либо опция кодируется не через DID, либо AUTEL её не читал в обеих записях.")


# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="canhunt.py", description="Анализ записанных сессий CAN (CAN_HUNT)",
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", metavar="КОМАНДА")

    def common(sp, src=True):
        if src:
            sp.add_argument("src", nargs="?", default="latest",
                            help="папка сессии, frames.log, .trc или latest (по умолчанию)")
            sp.add_argument("--sessions", help="каталог сессий (для latest)")
            sp.add_argument("--port", type=int, help="порт монитора (для latest)")
            sp.add_argument("--ch", help="только каналы (0 или 0,1)")
            sp.add_argument("--id", action="append", help="только ID: 0C9, 0:0C9, через запятую; можно несколько раз")
            sp.add_argument("--no-cache", action="store_true", help="не использовать кэш разбора")
        sp.add_argument("--json", action="store_true", help="вывод в JSON")

    sp = sub.add_parser("info", help="длительность, кадры, фазы, отметки, ввод")
    common(sp)
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("overview", help="по каждому ID: частота, длина, классы байтов")
    common(sp)
    sp.set_defaults(func=cmd_overview)

    sp = sub.add_parser("compare", help="что отличает фазы A и B")
    common(sp)
    sp.add_argument("--a", action="append", required=True, help="фазы A через запятую")
    sp.add_argument("--b", action="append", required=True, help="фазы B через запятую")
    sp.add_argument("--skip", type=float, default=0.5, help="отбросить начало каждой фазы, с (0.5)")
    sp.add_argument("--thr", type=float, default=0.8, help="порог разницы долей для бит (0.8)")
    sp.add_argument("--top", type=int, default=20)
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("marks", help="что меняется у отметок")
    common(sp)
    sp.add_argument("--label", help="только отметки с этой меткой")
    sp.add_argument("--phase", help="только отметки внутри этой фазы; «вне окон» тоже считается только в ней "
                                    "(несколько кнопок в одной записи: у каждой своя фаза)")
    sp.add_argument("--window", type=float, default=1.0, help="окно после отметки, с (1.0)")
    sp.add_argument("--before", type=float, default=0.3, help="окно до отметки, с (0.3)")
    sp.add_argument("--lag", type=float, default=3.0,
                    help="искать сдвиг окна относительно отметок в пределах ±LAG с (3.0; 0 — без сдвига)")
    sp.add_argument("--top", type=int, default=20)
    sp.set_defaults(func=cmd_marks)

    sp = sub.add_parser("track", help="поиск числового поля, линейно связанного с эталоном")
    common(sp)
    # --ref a=1 --ref b=2, --ref a=1,b=2 и --ref a=1 b=2 (как в SPEC §10) — всё равнозначно
    sp.add_argument("--ref", action="extend", nargs="+",
                    help="ФАЗА=ЗНАЧЕНИЕ (можно несколько: через пробел, через запятую или повторяя --ref)")
    sp.add_argument("--ref-names", action="store_true", help="эталон из чисел в конце имён фаз (rpm2000 → 2000)")
    sp.add_argument("--ref-inputs", action="store_true", help="эталон из введённых пользователем чисел (события input)")
    sp.add_argument("--skip", type=float, default=0.7, help="отбросить начало каждой фазы, с (0.7)")
    sp.add_argument("--top", type=int, default=5)
    sp.set_defaults(func=cmd_track)

    sp = sub.add_parser("corr", help="поля, коррелирующие с данным")
    common(sp)
    sp.add_argument("--field", required=True)
    sp.add_argument("--step", type=float, default=0.05, help="шаг сетки, с (0.05)")
    sp.add_argument("--top", type=int, default=15)
    sp.set_defaults(func=cmd_corr)

    sp = sub.add_parser("plot", help="PNG: поля по времени или тепловая полоса байтов ID")
    common(sp)
    sp.add_argument("--field", action="append", help="SPEC или ИМЯ=SPEC; можно несколько")
    sp.add_argument("--out", help="путь PNG (по умолчанию <сессия>\\plots\\)")
    sp.add_argument("--width", type=int, default=1400)
    sp.add_argument("--abs", action="store_true", help="для --id: общая шкала 00..FF вместо своей у каждого байта")
    sp.set_defaults(func=cmd_plot)

    sp = sub.add_parser("decode", help="статистика поля по фазам")
    common(sp)
    sp.add_argument("--field", required=True)
    sp.add_argument("--csv", help="сохранить значения в CSV (разделитель ;)")
    sp.set_defaults(func=cmd_decode)

    sp = sub.add_parser("uds", help="разбор диагностики (ISO-TP+UDS) из записи: блоки, сервисы, DID, лента")
    common(sp)
    sp.add_argument("--ecu", help="только блок с этим адресом ЭБУ (hex: 70, 0x70 или полный ID)")
    sp.add_argument("--service", help="только этот сервис в ленте (hex: 22, 2E, 31 …)")
    sp.set_defaults(func=cmd_uds)

    sp = sub.add_parser("uds-did", help="таблица всех DID блока и последних байт (дамп конфигурации)")
    common(sp)
    sp.add_argument("--ecu", help="адрес ЭБУ (hex); без него — все блоки записи")
    sp.set_defaults(func=cmd_uds_did)

    sp = sub.add_parser("uds-diff", help="различия дампов DID двух записей (сужение области поиска)")
    sp.add_argument("a", help="запись A: папка сессии, frames.log, .trc или latest")
    sp.add_argument("b", help="запись B: папка сессии, frames.log, .trc или latest")
    sp.add_argument("--ecu", help="только этот адрес ЭБУ (hex)")
    sp.add_argument("--sessions", help="каталог сессий (для latest)")
    sp.add_argument("--port", type=int, help="порт монитора (для latest)")
    sp.add_argument("--no-cache", action="store_true", help="не использовать кэш разбора")
    sp.add_argument("--json", action="store_true", help="вывод в JSON")
    sp.set_defaults(func=cmd_uds_diff)

    sp = sub.add_parser("export", help="строка для прошивки и строка DBC")
    common(sp, src=False)
    # источник необязателен: если указан (export latest --field …), длина и период кадра берутся из записи
    sp.add_argument("src", nargs="?", default=None,
                    help="папка сессии, frames.log, .trc или latest (необязательно: длина и период кадра из записи)")
    sp.add_argument("--sessions", help="каталог сессий (для latest)")
    sp.add_argument("--port", type=int, help="порт монитора (для latest)")
    sp.add_argument("--no-cache", action="store_true", help="не использовать кэш разбора")
    sp.add_argument("--field", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--unit", default="")
    sp.add_argument("--dlc", type=int, help="длина кадра для BO_ (по умолчанию по полю)")
    sp.set_defaults(func=cmd_export)
    return p


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    try:
        args.func(args)
        return 0
    except (LoadError, A.AnalysisError, SpecError, CliError) as e:
        if getattr(args, "json", False):
            emit_json({"ok": False, "error": str(e)})
        else:
            sys.stderr.write("Ошибка: %s\n" % e)
        return 1
