# -*- coding: utf-8 -*-
"""Загрузка сессий: candump (frames.log), PCAN .trc (1.x, 2.x), CAN-Hacker .trc,
events.jsonl → фазы / отметки / введённые значения, meta.json.

Хранение: по (канал, 29-бит?, ID) — Stream: t (float64, N), data (uint8 N×L), dlen (N).
11-битный и 29-битный кадры с одинаковым числом ID — разные сообщения и разные потоки.
"""
import binascii
import datetime
import glob
import hashlib
import json
import operator
import os
import re
import time
import urllib.request

import numpy as np

from .fieldspec import fmt_id

PARSER_VERSION = "3"

# Где лежат записи мониторов (папка can_sessions рядом с exe). Пакет плагина: <корень>/monitor/<плата>/can_sessions;
# переменная CANHUNT_SESSIONS (список папок через os.pathsep) — любое другое место; старая раскладка CUBE — на всякий случай.
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))  # корень пакета (над plugin/)
MONITOR_SESSION_DIRS = [p for p in os.environ.get("CANHUNT_SESSIONS", "").split(os.pathsep) if p] + [
    os.path.join(ROOT, "monitor", b, "can_sessions") for b in ("G4", "H723", "H503")] + [
    os.path.join(ROOT, "UnivrsalController-G4", "МОНИТОР", "can_sessions"),
    os.path.join(ROOT, "H_723", "МОНИТОР", "can_sessions"),
    os.path.join(ROOT, "H_503", "UC_H503_web", "can_sessions"),
]
MONITOR_PORTS = (8473, 8723, 8503)


class LoadError(Exception):
    pass


# ---------------------------------------------------------------------------
# Поток кадров одного (канал, ID)
# ---------------------------------------------------------------------------
class Stream(object):
    __slots__ = ("ch", "can_id", "ext", "t", "data", "dlen", "fd", "n_remote", "_cache")

    def __init__(self, ch, can_id, ext, t, data, dlen, fd=False, n_remote=0):
        self.ch = int(ch)
        self.can_id = int(can_id)
        self.ext = bool(ext)
        self.t = t
        self.data = data
        self.dlen = dlen
        self.fd = bool(fd)
        self.n_remote = int(n_remote)
        self._cache = {}

    @property
    def key(self):
        return (self.ch, self.ext, self.can_id)

    @property
    def name(self):
        return "%d:%s" % (self.ch, fmt_id(self.can_id, self.ext))

    @property
    def n(self):
        return len(self.t)

    @property
    def L(self):
        return self.data.shape[1]

    def rate(self):
        if self.n < 2:
            return 0.0
        dt = self.t[-1] - self.t[0]
        return (self.n - 1) / dt if dt > 0 else 0.0

    def period(self):
        if self.n < 3:
            return None
        return float(np.median(np.diff(self.t)))

    def len_mode(self):
        if self.n == 0:
            return 0
        v, c = np.unique(self.dlen, return_counts=True)
        return int(v[np.argmax(c)])

    def __repr__(self):
        return "<Stream %s n=%d L=%d>" % (self.name, self.n, self.L)


class _Builder(object):
    """Накопитель кусков для одного потока."""
    __slots__ = ("ch", "can_id", "ext", "ts", "mats", "fd", "n_remote")

    def __init__(self, ch, can_id, ext):
        self.ch, self.can_id, self.ext = ch, can_id, ext
        self.ts, self.mats, self.fd, self.n_remote = [], [], False, 0

    def add(self, t, mat):
        self.ts.append(t)
        self.mats.append(mat)

    def build(self):
        if not self.ts:
            t = np.zeros(0)
            return Stream(self.ch, self.can_id, self.ext, t, np.zeros((0, 0), np.uint8),
                          np.zeros(0, np.uint8), self.fd, self.n_remote)
        t = np.concatenate(self.ts)
        L = max(m.shape[1] for m in self.mats)
        if all(m.shape[1] == L for m in self.mats):
            data = np.concatenate(self.mats) if len(self.mats) > 1 else self.mats[0]
            dlen = np.full(len(t), L, np.uint8)
        else:
            data = np.zeros((len(t), L), np.uint8)
            dlen = np.zeros(len(t), np.uint8)
            i = 0
            for m in self.mats:
                k = m.shape[0]
                data[i:i + k, :m.shape[1]] = m
                dlen[i:i + k] = m.shape[1]
                i += k
        if len(t) > 1 and np.any(np.diff(t) < 0):
            o = np.argsort(t, kind="stable")
            t, data, dlen = t[o], data[o], dlen[o]
        return Stream(self.ch, self.can_id, self.ext, t, np.ascontiguousarray(data), dlen, self.fd, self.n_remote)


class _Acc(object):
    def __init__(self):
        self.b = {}

    def get(self, ch, can_id, ext):
        k = (ch, bool(ext), can_id)
        bl = self.b.get(k)
        if bl is None:
            bl = self.b[k] = _Builder(ch, can_id, ext)
        return bl

    def streams(self):
        out = {}
        for k, bl in self.b.items():
            s = bl.build()
            if s.n > 0 or s.n_remote > 0:
                out[k] = s
        return out


# ---------------------------------------------------------------------------
# candump
# ---------------------------------------------------------------------------
# (время) canN ID#данные | ID##<флаги FD><данные> | ID#R<dlc>; группы: время, «N ID», флаги FD, данные
_CANDUMP_RE = re.compile(
    rb"\((\d+\.?\d*)\)[ \t]+\D+(\d+[ \t]+[0-9A-Fa-f]+)#(#[0-9A-Fa-f]|)(R?[0-9A-Fa-f]*)")


def _hexrows(rows):
    """Список hex-строк (bytes) одинаковой длины → uint8 N×L."""
    n = len(rows)
    L2 = len(rows[0])
    if L2 == 0:
        return np.zeros((n, 0), np.uint8)
    buf = binascii.unhexlify(b"".join(rows))
    return np.frombuffer(buf, np.uint8).reshape(n, L2 // 2)


def _is_remote(d):
    return d[:1] == b"R"


def _candump_block(block, acc, keymap, stat):
    m = _CANDUMP_RE.findall(block)
    if not m:
        return
    stat["lines"] += len(m)
    ts, keys, fds, datas = zip(*m)
    t = np.fromiter(map(float, ts), np.float64, len(ts))
    uniq = {}
    inv = np.array([uniq.setdefault(k, len(uniq)) for k in keys], np.int32)
    order = np.argsort(inv, kind="stable")
    counts = np.bincount(inv, minlength=len(uniq))
    if len(order) == 1:
        dsorted = (datas[order[0]],)
        fsorted = (fds[order[0]],)
    else:
        get = operator.itemgetter(*order)
        dsorted = get(datas)
        fsorted = get(fds)
    starts = np.concatenate([[0], np.cumsum(counts)])
    for kb, gi in uniq.items():
        a, b = int(starts[gi]), int(starts[gi + 1])
        idx = order[a:b]
        rows = dsorted[a:b]
        info = keymap.get(kb)
        if info is None:
            parts = kb.split()
            ch = int(parts[0])
            idh = parts[1]
            cid = int(idh, 16)
            ext = len(idh) > 3 or cid > 0x7FF
            info = keymap[kb] = (ch, cid, ext)
        bl = acc.get(*info)
        if any(fsorted[a:b]):
            bl.fd = True
        if any(map(_is_remote, rows)):
            keep = np.array([not _is_remote(r) for r in rows], bool)
            bl.n_remote += int((~keep).sum())
            idx = idx[keep]
            rows = [r for r, k in zip(rows, keep) if k]
            if not rows:
                continue
        tt = t[idx]
        lens = np.fromiter(map(len, rows), np.int64, len(rows))
        if (lens & 1).any():
            ok = (lens & 1) == 0
            stat["bad"] += int((~ok).sum())
            tt = tt[ok]
            rows = [r for r, k in zip(rows, ok) if k]
            lens = lens[ok]
            if not rows:
                continue
        if lens.min() == lens.max():
            bl.add(tt, _hexrows(rows))
        else:
            for Lx in np.unique(lens):
                sel = np.nonzero(lens == Lx)[0]
                bl.add(tt[sel], _hexrows([rows[i] for i in sel]))


def parse_candump(path, chunk=1 << 24, drop_partial_tail=False):
    """drop_partial_tail — для идущей записи: последняя строка без перевода строки может быть недописана."""
    acc = _Acc()
    keymap = {}
    stat = {"lines": 0, "bad": 0}
    with open(path, "rb") as f:
        tail = b""
        while True:
            blk = f.read(chunk)
            if not blk:
                if tail.strip():
                    if drop_partial_tail:
                        stat["partial_tail"] = True
                    else:
                        _candump_block(tail, acc, keymap, stat)
                break
            blk = tail + blk
            cut = blk.rfind(b"\n")
            if cut < 0:
                tail = blk
                continue
            _candump_block(blk[:cut + 1], acc, keymap, stat)
            tail = blk[cut + 1:]
    return acc.streams(), stat


# ---------------------------------------------------------------------------
# PCAN .trc (1.0 … 1.3, 2.0, 2.1)
# ---------------------------------------------------------------------------
_FD_DLC = {9: 12, 10: 16, 11: 20, 12: 24, 13: 32, 14: 48, 15: 64}
_PCAN_DATA_TYPES = {"DT": (False, False), "FD": (True, False), "FB": (True, False),
                    "FE": (True, False), "BI": (True, False), "RR": (False, True)}


def _pcan_starttime(v):
    try:
        days = float(v)
        return (days - 25569.0) * 86400.0  # OLE-дата (локальное время) → «эпоха»
    except ValueError:
        return None


def parse_pcan_trc(path):
    acc = _Acc()
    stat = {"lines": 0, "bad": 0, "format": "PCAN", "version": None, "starttime": None}
    rows = {}  # (ch, id, ext) -> ([t], [bytes], fd)
    version = None
    columns = None
    with open(path, "r", encoding="latin-1", errors="replace") as f:
        lines = f.read().splitlines()
    for line in lines:
        if not line.strip():
            continue
        if line.startswith(";"):
            s = line[1:].strip()
            if s.startswith("$FILEVERSION="):
                version = s.split("=", 1)[1].strip()
            elif s.startswith("$STARTTIME="):
                stat["starttime"] = _pcan_starttime(s.split("=", 1)[1].strip())
            elif s.startswith("$COLUMNS="):
                columns = [c.strip() for c in s.split("=", 1)[1].split(",")]
            continue
        tok = line.split()
        if len(tok) < 3:
            continue
        stat["lines"] += 1
        try:
            if version and version.startswith("2"):
                cols = columns
                if not cols:
                    cols = ["N", "O", "T", "I", "d", "l", "D"] if version == "2.0" else \
                        ["N", "O", "T", "B", "I", "d", "R", "L", "D"]
                ci = {c: i for i, c in enumerate(cols)}
                typ = tok[ci["T"]] if "T" in ci else "DT"
                if typ not in _PCAN_DATA_TYPES:
                    continue
                fd, remote = _PCAN_DATA_TYPES[typ]
                t = float(tok[ci["O"]]) / 1000.0
                bus = int(tok[ci["B"]]) if "B" in ci and tok[ci["B"]].isdigit() else 1
                idh = tok[ci["I"]]
                cid = int(idh, 16)
                ext = len(idh) > 4 or cid > 0x7FF
                if remote:
                    acc.get(bus - 1, cid, ext).n_remote += 1
                    continue
                di = ci.get("D", len(cols) - 1)
                if "L" in ci:
                    n = int(tok[ci["L"]])
                elif "l" in ci:
                    dlc = int(tok[ci["l"]])
                    n = _FD_DLC.get(dlc, dlc) if fd else min(dlc, 8)
                else:
                    n = len(tok) - di
                data = bytes(int(x, 16) for x in tok[di:di + n])
                if len(data) != n:
                    stat["bad"] += 1
                    continue
            else:
                # 1.x: "N)" время [шина] Rx|Tx ID [-] DLC данные|RTR
                if not tok[0].endswith(")"):
                    continue
                t = float(tok[1]) / 1000.0
                i = 2
                bus = 1
                if tok[i].isdigit() and i + 1 < len(tok) and tok[i + 1] in ("Rx", "Tx"):
                    bus = int(tok[i])
                    i += 1
                if tok[i] in ("Rx", "Tx"):
                    i += 1
                elif tok[i] in ("Error", "Warng", "Warning", "Status"):
                    continue
                idh = tok[i]
                i += 1
                if tok[i] == "-":
                    i += 1
                dlc = int(tok[i])
                i += 1
                cid = int(idh, 16)
                ext = len(idh) > 4 or cid > 0x7FF
                if i < len(tok) and tok[i] == "RTR":
                    acc.get(bus - 1, cid, ext).n_remote += 1
                    continue
                n = _FD_DLC.get(dlc, dlc) if dlc > 8 else dlc
                data = bytes(int(x, 16) for x in tok[i:i + n])
                fd = dlc > 8 or len(data) > 8
                if len(data) != n:
                    stat["bad"] += 1
                    continue
        except (ValueError, IndexError, KeyError):
            stat["bad"] += 1
            continue
        key = (bus - 1, cid, ext)
        r = rows.get(key)
        if r is None:
            r = rows[key] = ([], [], [False])
        r[0].append(t)
        r[1].append(data)
        if fd:
            r[2][0] = True
    stat["version"] = version or "1.x"
    _rows_to_acc(rows, acc)
    return acc.streams(), stat


def _rows_to_acc(rows, acc):
    for (ch, cid, ext), (ts, datas, fdl) in rows.items():
        bl = acc.get(ch, cid, ext)
        bl.fd = bl.fd or fdl[0]
        t = np.array(ts, np.float64)
        lens = np.fromiter(map(len, datas), np.int64, len(datas))
        for Lx in np.unique(lens):
            sel = np.nonzero(lens == Lx)[0]
            if Lx == 0:
                mat = np.zeros((len(sel), 0), np.uint8)
            else:
                mat = np.frombuffer(b"".join(datas[i] for i in sel), np.uint8).reshape(len(sel), int(Lx))
            bl.add(t[sel], mat)


# ---------------------------------------------------------------------------
# CAN-Hacker .trc ("@ TEXT @ 3 @ 64 @ ...", поля через TAB, строки через CR)
# время "сек,мкс" \t канал \t флаги(hex) \t ID \t длина \t байты \t ...
# Флаги (битовое слово кадра): 0x01 — 29-битный ID (IDE), 0x10 — CAN FD (FDF),
# 0x20 — BRS, 0x02 — remote (RTR). ВАЖНО: FD определяется битом FDF, а НЕ длиной —
# FD-кадр может нести и 8, и 4 байта. В однотипной записи флаги у всех кадров одинаковы
# (напр. 0x31 = IDE|FDF|... у 29-битных FD), это не «постоянный дескриптор», а именно флаги.
# 29-бит берём из флага или по значению ID (>0x7FF, SPEC §2); длина >8 — доп. признак FD.
# ---------------------------------------------------------------------------
def parse_canhacker_trc(path):
    acc = _Acc()
    stat = {"lines": 0, "bad": 0, "format": "CAN-Hacker", "header_frames": None, "local_time": None}
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("latin-1")
    rows = {}
    chan_vals = set()
    parsed = []
    for line in re.split(r"\r\n|\r|\n", text):
        if not line.strip():
            continue
        if line.startswith("@"):
            parts = [p.strip() for p in line.split("@")]
            parts = [p for p in parts if p]
            # @ TEXT @ 3 @ 64 @ 0 @ <кадров> @ <мс> @ <чч:мм:сс.мс> @
            if len(parts) >= 6 and parts[4].isdigit():
                stat["header_frames"] = int(parts[4])
            continue
        if line.startswith("#"):
            m = re.search(r"\$LocalTime=([\d:.]+)", line)
            if m:
                stat["local_time"] = m.group(1)
            continue
        f = line.split("\t")
        if len(f) < 5:
            stat["bad"] += 1
            continue
        stat["lines"] += 1
        try:
            t = float(f[0].replace(",", "."))
            chv = int(f[1]) if f[1].strip() else 0
            flags = int(f[2], 16) if f[2].strip() else 0
            idh = f[3].strip()
            cid = int(idh, 16)
            n = int(f[4])
            ext = bool(flags & 0x01) or cid > 0x7FF   # 29-бит: бит IDE или значение ID (SPEC §2)
            fd = bool(flags & 0x10) or n > 8           # FD: бит FDF (не длина!); >8 — доп. признак
            remote = bool(flags & 0x02)
            hx = f[5].split() if len(f) > 5 else []
            data = bytes(int(x, 16) for x in hx[:n])
        except (ValueError, IndexError):
            stat["bad"] += 1
            continue
        chan_vals.add(chv)
        parsed.append((t, chv, cid, ext, fd, remote, data, n))
    # каналы: одно значение → канал 0; несколько → по порядку 0, 1, 2
    cmap = {v: i for i, v in enumerate(sorted(chan_vals))}
    for t, chv, cid, ext, fd, remote, data, n in parsed:
        ch = cmap[chv]
        if remote:
            acc.get(ch, cid, ext).n_remote += 1
            continue
        if len(data) != n:
            stat["bad"] += 1
            continue
        key = (ch, cid, ext)
        r = rows.get(key)
        if r is None:
            r = rows[key] = ([], [], [False])
        r[0].append(t)
        r[1].append(data)
        if fd:
            r[2][0] = True
    _rows_to_acc(rows, acc)
    return acc.streams(), stat


def detect_format(path):
    with open(path, "rb") as f:
        head = f.read(8192)
    if head.startswith(b"@ TEXT @") or head.startswith(b"@TEXT@"):
        return "canhacker"
    if b";$FILEVERSION" in head or head.lstrip().startswith(b";"):
        return "pcan"
    if _CANDUMP_RE.search(head):
        return "candump"
    if path.lower().endswith(".trc"):
        return "pcan"
    return "candump"


# ---------------------------------------------------------------------------
# События
# ---------------------------------------------------------------------------
def read_events(path):
    out = []
    if not path or not os.path.isfile(path):
        return out
    with open(path, "rb") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln.decode("utf-8", "replace"))
            except ValueError:
                continue  # недописанная последняя строка идущей сессии
            if isinstance(e, dict) and "t" in e and "type" in e:
                try:
                    e["t"] = float(e["t"])
                except (TypeError, ValueError):
                    continue
                out.append(e)
    out.sort(key=lambda e: (e.get("seq") if isinstance(e.get("seq"), (int, float)) else 0, e["t"]))
    # seq может начинаться заново после перезапуска монитора — порядок по времени надёжнее
    out.sort(key=lambda e: e["t"])
    return out


class Interval(object):
    __slots__ = ("label", "t0", "t1", "by")

    def __init__(self, label, t0, t1, by=None):
        self.label, self.t0, self.t1, self.by = label, float(t0), float(t1), by

    @property
    def dur(self):
        return self.t1 - self.t0

    def __repr__(self):
        return "<Phase %s %.2f..%.2f>" % (self.label, self.t0, self.t1)


def build_phases(events, t_end):
    """Фазы по событиям phase. session_start несёт фазу, которая уже шла в момент старта записи
    (поле phase, SPEC §6): с неё запись и начинается."""
    out = []
    cur = None
    for e in events:
        typ = e.get("type")
        if typ == "phase" or (typ == "session_start" and "phase" in e):
            lab = e.get("label") if typ == "phase" else e.get("phase")
            lab = "" if lab is None else str(lab)
            if cur is not None:
                if e["t"] > cur.t0:
                    cur.t1 = e["t"]
                    out.append(cur)
                cur = None
            if lab:
                cur = Interval(lab, e["t"], e["t"], e.get("by") if typ == "phase" else "session_start")
        elif typ == "session_stop" and cur is not None:
            cur.t1 = e["t"]
            if cur.t1 > cur.t0:
                out.append(cur)
            cur = None
    if cur is not None:
        cur.t1 = max(t_end, cur.t0)
        if cur.t1 > cur.t0:
            out.append(cur)
    return out


def _parse_number(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace(",", ".").replace(" ", "")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


# ---------------------------------------------------------------------------
# Сессия
# ---------------------------------------------------------------------------
class Session(object):
    def __init__(self):
        self.src = None           # что передал пользователь
        self.frames_path = None
        self.dir = None           # папка сессии (для plots\)
        self.kind = None          # candump / pcan / canhacker
        self.streams = {}
        self.events = []
        self.meta = {}
        self.stat = {}
        self.phases = []
        self.marks = []
        self.inputs = []
        self.notes = []
        self.t0 = 0.0
        self.t_first = 0.0
        self.t_last = 0.0
        self.t_end = 0.0
        self.epoch = True         # время — эпоха (candump) или смещение (trc)
        self.parse_time = 0.0
        self.from_cache = False
        self.live = False         # запись ещё идёт (meta.stopped = null)
        self.warnings = []
        self._cls = {}

    # --- удобства ---------------------------------------------------------
    def rel(self, t):
        return t - self.t0

    @property
    def duration(self):
        return self.t_end - self.t0

    @property
    def n_frames(self):
        return sum(s.n for s in self.streams.values())

    def stream(self, ch, can_id, ext=None):
        """Поток (канал, ID). ext=None — тип ID по значению (> 7FF — 29 бит), а если такого потока нет —
        поток с тем же числом ID другого типа; ext=True/False — строго этот тип (так ищет поле по spec)."""
        if ext is not None:
            return self.streams.get((ch, bool(ext), can_id))
        s = self.streams.get((ch, can_id > 0x7FF, can_id))
        return s if s is not None else self.streams.get((ch, can_id <= 0x7FF, can_id))

    def ambiguous_ids(self):
        """(канал, ID), у которых в записи есть и 11-битный, и 29-битный кадр."""
        out = []
        for (ch, ext, cid) in self.streams:
            if not ext and (ch, True, cid) in self.streams:
                out.append((ch, cid))
        return sorted(out)

    def select(self, chs=None, ids=None):
        """ids — список (канал|None, ID[, 29-бит? True/False/None])."""
        out = []
        for (ch, ext, cid), s in sorted(self.streams.items()):
            if s.n == 0:
                continue
            if chs is not None and ch not in chs:
                continue
            if ids is not None:
                ok = False
                for it in ids:
                    fch, fid = it[0], it[1]
                    fext = it[2] if len(it) > 2 else None
                    if fid == cid and (fch is None or fch == ch) and (fext is None or bool(fext) == ext):
                        ok = True
                        break
                if not ok:
                    continue
            out.append(s)
        return out

    def phase_labels(self):
        seen = []
        for p in self.phases:
            if p.label not in seen:
                seen.append(p.label)
        return seen

    def phase_at(self, t):
        for p in self.phases:
            if p.t0 <= t <= p.t1:
                return p
        return None

    def intervals_for(self, labels):
        labels = set(labels)
        return [p for p in self.phases if p.label in labels]

    def time_str(self, t):
        return "%.2f" % (t - self.t0)

    def classification(self, s):
        """Классификация байтов потока (кэшируется)."""
        from .classify import classify_stream
        c = self._cls.get(s.key)
        if c is None:
            c = self._cls[s.key] = classify_stream(s)
        return c


def _finish(sess):
    ts_first = [s.t[0] for s in sess.streams.values() if s.n]
    ts_last = [s.t[-1] for s in sess.streams.values() if s.n]
    sess.t_first = min(ts_first) if ts_first else 0.0
    sess.t_last = max(ts_last) if ts_last else 0.0
    ev_t = [e["t"] for e in sess.events]
    t0 = sess.t_first
    if sess.epoch:
        st = sess.meta.get("started") if isinstance(sess.meta, dict) else None
        cands = [x for x in [st, (min(ev_t) if ev_t else None)] if isinstance(x, (int, float))]
        if cands:
            t0 = min([t0] + cands) if ts_first else min(cands)
    else:
        t0 = sess.t_first   # .trc: время — смещение от старта логгера; отсчёт — от первого кадра
    sess.t0 = t0
    t_end = sess.t_last
    if ev_t:
        t_end = max(t_end, max(ev_t))
    stp = sess.meta.get("stopped") if isinstance(sess.meta, dict) else None
    if isinstance(stp, (int, float)):
        t_end = max(t_end, stp)
    sess.t_end = t_end
    sess.phases = build_phases(sess.events, t_end)
    says = {}
    for e in sess.events:
        typ = e.get("type")
        if typ == "say":
            says[e.get("id", e.get("seq"))] = e
        elif typ == "mark":
            sess.marks.append({"t": e["t"], "label": str(e.get("label") or ""), "by": e.get("by"), "seq": e.get("seq")})
        elif typ == "note":
            sess.notes.append({"t": e["t"], "text": e.get("text", ""), "by": e.get("by")})
    for e in sess.events:
        if e.get("type") == "input":
            sy = says.get(e.get("say"))
            ph = sess.phase_at(e["t"])
            sess.inputs.append({
                "t": e["t"], "raw": e.get("value"), "value": _parse_number(e.get("value")),
                "say": e.get("say"), "label": (sy or {}).get("input_label") or (sy or {}).get("text") or "",
                "phase": ph.label if ph else None, "by": e.get("by")})


# --- кэш разобранных кадров -------------------------------------------------
def _cache_dir():
    d = os.environ.get("CANHUNT_CACHE")
    if not d:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "canhunt", "cache")
    return d


def _cache_path(path):
    st = os.stat(path)
    key = "%s|%d|%d|%s" % (os.path.abspath(path).lower(), st.st_size, st.st_mtime_ns, PARSER_VERSION)
    return os.path.join(_cache_dir(), hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".npz")


def _cache_load(path):
    try:
        cp = _cache_path(path)
        if not os.path.isfile(cp):
            return None
        z = np.load(cp, allow_pickle=False)
        idx = json.loads(bytes(z["index"]).decode("utf-8"))
        streams = {}
        for i, it in enumerate(idx["streams"]):
            s = Stream(it["ch"], it["id"], it["ext"], z["t%d" % i], z["d%d" % i], z["l%d" % i],
                       it["fd"], it["rem"])
            streams[s.key] = s
        return streams, idx["stat"]
    except Exception:
        return None


def _cache_save(path, streams, stat):
    try:
        d = _cache_dir()
        os.makedirs(d, exist_ok=True)
        arrs = {}
        idx = {"streams": [], "stat": stat}
        for i, s in enumerate(streams.values()):
            idx["streams"].append({"ch": s.ch, "id": s.can_id, "ext": s.ext, "fd": s.fd, "rem": s.n_remote})
            arrs["t%d" % i] = s.t
            arrs["d%d" % i] = s.data
            arrs["l%d" % i] = s.dlen
        arrs["index"] = np.frombuffer(json.dumps(idx).encode("utf-8"), np.uint8)
        cp = _cache_path(path)
        tmp = cp + ".tmp.npz"
        np.savez(tmp, **arrs)
        os.replace(tmp, cp)
        # держим не больше 12 файлов кэша
        files = sorted(glob.glob(os.path.join(d, "*.npz")), key=os.path.getmtime)
        for fp in files[:-12]:
            try:
                os.remove(fp)
            except OSError:
                pass
    except Exception:
        pass


def parse_frames_file(path, use_cache=True, live=False):
    """(streams, stat, kind, from_cache). live — запись ещё идёт (без кэша, недописанная строка отбрасывается)."""
    kind = detect_format(path)
    size = os.path.getsize(path)
    cacheable = use_cache and not live and size > (4 << 20) and not os.environ.get("CANHUNT_NO_CACHE")
    if cacheable:
        r = _cache_load(path)
        if r is not None:
            return r[0], r[1], kind, True
    if kind == "canhacker":
        streams, stat = parse_canhacker_trc(path)
    elif kind == "pcan":
        streams, stat = parse_pcan_trc(path)
    else:
        streams, stat = parse_candump(path, drop_partial_tail=live)
    if cacheable and streams:
        _cache_save(path, streams, stat)
    return streams, stat, kind, False


# --- поиск «latest» -------------------------------------------------------------
def _car_state_port():
    """Порт монитора, который запомнил car.py (state.json, SPEC §9): latest берётся у того же монитора,
    с которым Claude сейчас работает (в том числе демо на 18473)."""
    p = os.environ.get("CANHUNT_STATE", "").strip()
    if not p:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        p = os.path.join(base, "canhunt", "state.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            v = json.load(f).get("port")
        return int(v) if v else None
    except Exception:
        return None


def _ask_monitor(port=None, timeout=1.5):
    ports = []
    if port:
        ports.append(int(port))
    elif os.environ.get("UC_PORT"):
        try:
            ports.append(int(os.environ["UC_PORT"]))
        except ValueError:
            pass
    if not ports:
        sp = _car_state_port()
        ports = ([sp] if sp else []) + [x for x in MONITOR_PORTS if x != sp]
    for p in ports:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/session" % p, timeout=timeout) as r:
                j = json.loads(r.read().decode("utf-8"))
            return p, j
        except Exception:
            continue
    return None, None


def _session_dirs(root):
    out = []
    try:
        for n in os.listdir(root):
            d = os.path.join(root, n)
            if os.path.isdir(d) and (os.path.isfile(os.path.join(d, "frames.log")) or
                                     os.path.isfile(os.path.join(d, "meta.json"))):
                out.append(d)
    except OSError:
        pass
    return out


def _newest(dirs):
    if not dirs:
        return None

    def key(d):
        n = os.path.basename(d)
        m = re.match(r"(\d{8}_\d{6})", n)
        return (m.group(1) if m else "", os.path.getmtime(d))
    return max(dirs, key=key)


def resolve_latest(sessions=None, port=None):
    if sessions:
        d = _newest(_session_dirs(sessions))
        if not d:
            raise LoadError("в каталоге %s нет сессий" % sessions)
        return d
    p, j = _ask_monitor(port)
    if j:
        d = j.get("dir") if j.get("active") else None
        d = d or j.get("last_dir") or j.get("dir")
        if d and os.path.isdir(d):
            return d
        root = j.get("sessions_root")
        if root:
            d = _newest(_session_dirs(root))
            if d:
                return d
    dirs = []
    for root in MONITOR_SESSION_DIRS:
        dirs += _session_dirs(root)
    d = _newest(dirs)
    if not d:
        raise LoadError("не нашёл сессий: монитор не отвечает, в стандартных каталогах пусто. "
                        "Укажите папку сессии, frames.log или --sessions DIR")
    return d


def load_session(src, sessions=None, port=None, use_cache=True):
    t_start = time.time()
    sess = Session()
    sess.src = src
    if src in (None, "", "latest"):
        src = resolve_latest(sessions, port)
    src = os.path.abspath(src)
    if os.path.isdir(src):
        sess.dir = src
        fp = os.path.join(src, "frames.log")
        if not os.path.isfile(fp):
            cands = sorted(glob.glob(os.path.join(src, "*.log")) + glob.glob(os.path.join(src, "*.trc")))
            if not cands:
                raise LoadError("в папке %s нет frames.log" % src)
            fp = cands[0]
        sess.frames_path = fp
    elif os.path.isfile(src):
        sess.frames_path = src
        sess.dir = os.path.dirname(src)
    else:
        raise LoadError("не найдено: %s" % src)
    mp = os.path.join(sess.dir, "meta.json")
    if os.path.isfile(mp):
        try:
            with open(mp, "r", encoding="utf-8") as f:
                sess.meta = json.load(f)
        except (ValueError, OSError):
            sess.meta = {}
    live = isinstance(sess.meta, dict) and "stopped" in sess.meta and sess.meta.get("stopped") is None
    streams, stat, kind, cached = parse_frames_file(sess.frames_path, use_cache, live)
    sess.streams, sess.stat, sess.kind, sess.from_cache = streams, stat, kind, cached
    sess.live = live
    sess.epoch = kind == "candump"
    evp = os.path.join(sess.dir, "events.jsonl")
    sess.events = read_events(evp)
    if kind != "candump" and sess.events:
        # время .trc — смещение от начала записи; события с эпохой к нему не привязать
        sess.warnings.append("события events.jsonl не используются: время .trc — смещение, а не эпоха")
        sess.events = []
    _finish(sess)
    sess.parse_time = time.time() - t_start
    return sess


def fmt_epoch(t):
    try:
        return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "?"
