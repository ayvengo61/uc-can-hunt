#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
car.py — команды монитору UC_<плата>_Monitor.exe для совместного поиска сигналов в CAN
(CAN_HUNT; контракт — SPEC.md §7 и §9). Только стандартная библиотека.

Примеры:
  python car.py status
  python car.py sniff on
  python car.py session start rpm
  python car.py ask "Заведи двигатель. Когда работает ровно — нажми «Холостые»" --buttons "Холостые=idle,Не заводится"
  python car.py wait --timeout 60
  python car.py watch set "Обороты=0:0C9:1.7:16:be*0.25@об/мин"
  python car.py watch values --count 5
  python car.py active on --ch 0 --br 125            # перевести канал в активный режим (ПИШЕТ в шину!)
  python car.py tx 0 384 01FF00                       # передать кадр один раз
  python car.py tx 0 384 01FF00 --period 100          # передавать периодически (100 мс)
  python car.py tx-list                               # активные слоты передачи
  python car.py tx-stop                               # остановить всю передачу
  python car.py active off --ch 0                     # вернуть канал в тихий режим

Порт монитора: --port, иначе переменная UC_PORT, иначе порт, с которым работали в прошлый раз (state.json, в том
числе демо 18473), иначе автопоиск среди 8473 (G4), 8723 (H723), 8503 (H503) и портов из файлов
%TEMP%\UC_<плата>_Monitor[_demo].port (монитор пишет туда порт, если стандартный был занят и он взял +1…+9).

Коды выхода:
  0 — успешно / событие пришло;
  1 — монитор недоступен (не запущен, связь потеряна, монитор перезапускался во время ожидания);
  2 — таймаут ожидания (wait, ask);
  3 — ошибка команды (монитор ответил ok:false, неверные аргументы, старая версия монитора, контроллер не подключён,
      сниффер включён не в режиме «молча»);
  4 — ask / wait --say: пользователь прислал сообщение вместо ответа на инструкцию (инструкция остаётся на экране);
  5 — ask / wait --say: инструкцию заменили новой (ожидание снято, ответа на неё уже не будет);
  6 — ask / wait: связь с контроллером потеряна или сниффер выключился — кадры не пишутся.

Состояние (порт, последний увиденный seq событий) — %LOCALAPPDATA%\canhunt\state.json.
Для тестов: CANHUNT_STATE — другой файл состояния, CANHUNT_PORTS — свой список портов автопоиска.
"""

import argparse
import http.client
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True           # не оставлять __pycache__ в папке плагина
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Разбор spec — общий с canhunt.py и тем же правилам следует монитор (SPEC §2); модуль только на stdlib
from canhunt_lib.specparse import SpecSyntaxError, parse_parts  # noqa: E402

DEFAULT_PORTS = (8473, 8723, 8503)
EXE_BY_PORT = {8473: "UC_G4_Monitor.exe", 8723: "UC_H723_Monitor.exe", 8503: "UC_H503_Monitor.exe"}
EXE_BY_BOARD = {"G4": "UC_G4_Monitor.exe", "H723": "UC_H723_Monitor.exe", "H503": "UC_H503_Monitor.exe"}

EXIT_OK = 0
EXIT_NO_MONITOR = 1
EXIT_TIMEOUT = 2
EXIT_ERROR = 3
EXIT_NOTE = 4
EXIT_REPLACED = 5
EXIT_LINK = 6

USER_TYPES = "button,input,mark,note"            # что ждём от пользователя по умолчанию
WAIT_TYPES = USER_TYPES + ",link"                # wait без --types: и события связи с контроллером (SPEC §6)
ASK_TYPES = "button,input,mark,note,phase,link,say"   # ask / wait --say: всё, что меняет смысл ожидания
KNOWN_TYPES = ("session_start", "session_stop", "say", "button", "input", "phase", "mark", "note", "watch", "link")
POLL_CHUNK = 60.0                               # длинный опрос кусками: так заметен перезапуск монитора
# столько ждём восстановления связи/сниффера (переподключение USB, повтор команды сниффера монитором), прежде
# чем бить тревогу; CANHUNT_LINK_GRACE — для тестов
try:
    LINK_GRACE = float(os.environ.get("CANHUNT_LINK_GRACE", "") or 12.0)
except ValueError:
    LINK_GRACE = 12.0
MAX_WATCH = 12

PHASE_BAD = re.compile(r"[,\s]")
HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")


class CarError(Exception):
    """Понятная пользователю ошибка с кодом выхода."""

    def __init__(self, msg, code=EXIT_ERROR):
        Exception.__init__(self, msg)
        self.code = code


class NoMonitor(CarError):
    def __init__(self, msg):
        CarError.__init__(self, msg, EXIT_NO_MONITOR)


def out(line=""):
    print(line, flush=True)


def dump(obj):
    out(json.dumps(obj, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- HTTP

# Прокси Windows не должны перехватывать 127.0.0.1
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def exe_hint(port=None, board=None):
    if board in EXE_BY_BOARD:
        return EXE_BY_BOARD[board]
    if port is not None and (port % 10000) in EXE_BY_PORT:
        return EXE_BY_PORT[port % 10000]
    return "UC_G4_Monitor.exe (или UC_H723_Monitor.exe / UC_H503_Monitor.exe)"


def no_monitor_text(port, why=""):
    s = "монитор не запущен на порту %d — откройте %s" % (port, exe_hint(port))
    if port >= 10000:
        s += " с ключом --demo"
    if why:
        s += " (%s)" % why
    return s


def http_request(port, method, path, body=None, timeout=5.0):
    """(код HTTP, байты ответа). Нет связи → NoMonitor."""
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    headers = {}
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b""
        return e.code, raw
    except urllib.error.URLError as e:
        raise NoMonitor(no_monitor_text(port, _why(e.reason)))
    except (OSError, http.client.HTTPException) as e:
        raise NoMonitor(no_monitor_text(port, _why(e)))


def _why(e):
    if isinstance(e, ConnectionRefusedError):
        return "порт закрыт"
    if isinstance(e, TimeoutError):
        return "не отвечает"
    if isinstance(e, (ConnectionResetError, ConnectionAbortedError, http.client.RemoteDisconnected)):
        return "соединение оборвано"
    return type(e).__name__


class Monitor:
    """Обёртка над HTTP-API монитора (SPEC §7)."""

    def __init__(self, port):
        self.port = port
        self.board = None

    def _json(self, method, path, body=None, timeout=5.0):
        code, raw = http_request(self.port, method, path, body, timeout)
        if code != 200 and not is_monitor(self.port):
            raise CarError("на порту %d отвечает не монитор UC (нет /status) — проверьте --port / UC_PORT; "
                           "монитор: %s" % (self.port, exe_hint(self.port, self.board)))
        if code == 404:
            raise CarError("монитор на порту %d не знает %s — это старая версия без совместной работы; "
                           "обновите/пересоберите %s (build_exe.bat)"
                           % (self.port, path.split("?")[0], exe_hint(self.port, self.board)))
        if code != 200:
            raise CarError("монитор ответил HTTP %d на %s" % (code, path.split("?")[0]))
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise CarError("монитор прислал не JSON на %s: %r" % (path.split("?")[0], raw[:120]))

    def get(self, path, timeout=5.0):
        return self._json("GET", path, None, timeout)

    def post(self, path, body=None, timeout=5.0):
        return self._json("POST", path, {} if body is None else body, timeout)

    def text(self, path, timeout=5.0):
        code, raw = http_request(self.port, "GET", path, None, timeout)
        return raw.decode("utf-8", "replace") if code == 200 else ""


# ---------------------------------------------------------------- порт

def is_monitor(port, timeout=2.5):
    try:
        code, raw = http_request(port, "GET", "/status", timeout=timeout)
    except NoMonitor:
        return False
    return code == 200 and raw.startswith(b"connected=")


def port_files():
    """Порты из файлов, которые пишет монитор при старте: %TEMP%\\UC_<плата>_Monitor[_demo].port.
    Если стандартный порт занят, монитор берёт следующий (+1…+9) — узнать его можно только так."""
    out = []
    tmp = tempfile.gettempdir()
    for b in ("G4", "H723", "H503"):
        for tag in ("", "_demo"):
            try:
                with open(os.path.join(tmp, "UC_%s_Monitor%s.port" % (b, tag)), "r", encoding="ascii") as f:
                    p = int(f.read().strip())
                if 0 < p < 65536 and p not in out:
                    out.append(p)
            except (OSError, ValueError):
                pass
    return out


def candidate_ports():
    env = os.environ.get("CANHUNT_PORTS", "").strip()
    if env:
        try:
            return [int(x) for x in re.split(r"[,\s;]+", env) if x]
        except ValueError:
            raise CarError("CANHUNT_PORTS: ожидается список портов через запятую, получено «%s»" % env)
    out = list(DEFAULT_PORTS)
    for p in port_files():               # в том числе демо (18473…) и сдвинутый на +1…+9 порт
        if p not in out:
            out.append(p)
    return out


def resolve_port(args, state):
    if args.port:
        return int(args.port)
    env = os.environ.get("UC_PORT", "").strip()
    if env:
        try:
            return int(env)
        except ValueError:
            raise CarError("UC_PORT должен быть числом, сейчас «%s»" % env)
    cands = candidate_ports()
    cached = state.get("port")
    # сначала тот, с кем работали в прошлый раз (в том числе демо 18473, заданный раньше через --port) — это быстро
    if isinstance(cached, int) and (cached in cands or not os.environ.get("CANHUNT_PORTS", "").strip()):
        if is_monitor(cached):
            return cached
        cands = [p for p in cands if p != cached]
    # остальные — параллельно: отказ соединения в Windows приходит только через ~2 с
    found = {}
    threads = []
    for p in cands:
        t = threading.Thread(target=lambda p=p: found.__setitem__(p, is_monitor(p)))
        t.daemon = True
        t.start()
        threads.append(t)
    for t in threads:
        t.join(5.0)
    for p in cands:
        if found.get(p):
            return p
    tried = ([cached] if isinstance(cached, int) and cached not in cands else []) + cands
    raise NoMonitor("монитор не запущен — откройте UC_G4_Monitor.exe (или UC_H723_Monitor.exe / UC_H503_Monitor.exe); "
                    "проверены порты %s" % ", ".join(str(p) for p in tried))


# ---------------------------------------------------------------- состояние

def state_file():
    p = os.environ.get("CANHUNT_STATE", "").strip()
    if p:
        return p
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "canhunt", "state.json")


def load_state():
    try:
        with open(state_file(), "r", encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def update_state(fn):
    """Перечитать, изменить, записать атомарно. Состояние не критично: ошибки записи молча пропускаются."""
    path = state_file()
    for _ in range(5):
        try:
            s = load_state()
            fn(s)
            s["updated"] = round(time.time(), 3)
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = "%s.%d.tmp" % (path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            return
        except OSError:
            time.sleep(0.05)


def saved_seq(port):
    v = (load_state().get("seq") or {}).get(str(port))
    return int(v) if isinstance(v, (int, float)) else None


def remember_seq(port, seq, force=False):
    def fn(s):
        d = s.get("seq")
        if not isinstance(d, dict):
            d = s["seq"] = {}
        old = d.get(str(port))
        if force or not isinstance(old, (int, float)) or seq > old:
            d[str(port)] = int(seq)
    update_state(fn)


def current_seq(mon):
    st = mon.get("/coach/state")
    mon.board = st.get("board") or mon.board
    return int(st.get("seq") or 0)


def last_seen(mon, cur):
    """Последний увиденный seq; если монитор перезапускался (seq стал меньше) — текущий."""
    saved = saved_seq(mon.port)
    if saved is None or saved > cur:
        remember_seq(mon.port, cur, force=True)
        return cur
    return saved


# ---------------------------------------------------------------- форматирование

def hms(t):
    try:
        t = float(t)
    except (TypeError, ValueError):
        return "--:--:--"
    return time.strftime("%H:%M:%S", time.localtime(t)) + (".%d" % int((t % 1) * 10))


def dur(sec):
    sec = int(round(max(0.0, float(sec or 0))))
    if sec < 60:
        return "%d с" % sec
    if sec < 3600:
        return ("%d мин" % (sec // 60)) if sec % 60 == 0 else ("%d мин %d с" % (sec // 60, sec % 60))
    return "%d ч %d мин" % (sec // 3600, sec % 3600 // 60)


def num(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        if math.isnan(v):
            return "—"
        if abs(v - round(v)) < 1e-9 and abs(v) < 1e15:
            return str(int(round(v)))
        return ("%.3f" % v).rstrip("0").rstrip(".")
    return str(v)


def short(text, n=90):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def fmt_phase(ph):
    if ph is None:
        return ""
    return " → фаза «%s»" % ph if ph != "" else " → конец фазы"


def fmt_button(b):
    return "«%s»%s" % (b.get("label", ""), fmt_phase(b.get("phase")))


def fmt_event(e):
    typ = e.get("type")
    by = e.get("by")
    who = {"user": "пользователь", "claude": "Claude", "system": "монитор"}.get(by, by or "?")
    head = "#%s %s" % (e.get("seq", "?"), hms(e.get("t")))
    if typ == "say":
        s = "%s Claude: инструкция «%s»" % (head, short(e.get("text"), 120))
        if e.get("buttons"):
            s += " [кнопки: %s]" % ", ".join(fmt_button(b) for b in e["buttons"])
        if e.get("input") not in (None, "", "none"):
            s += " [ввод %s: «%s»]" % (e.get("input"), e.get("input_label", ""))
        return s
    if typ == "button":
        return "%s %s: кнопка «%s»%s (инструкция #%s)" % (head, who, e.get("label", ""), fmt_phase(e.get("phase")), e.get("say"))
    if typ == "input":
        return "%s %s: ввёл «%s» (инструкция #%s)" % (head, who, e.get("value", ""), e.get("say"))
    if typ == "phase":
        lab = e.get("label") or ""
        return "%s %s: %s" % (head, who, ("фаза «%s» началась" % lab) if lab else "фаза закончилась")
    if typ == "mark":
        lab = e.get("label") or ""
        return "%s %s: отметка%s" % (head, who, (" «%s»" % lab) if lab else "")
    if typ == "note":
        return "%s %s: сообщение «%s»" % (head, who, e.get("text", ""))
    if typ == "watch":
        names = [f.get("name", "?") for f in (e.get("fields") or [])]
        return "%s %s: наблюдение — %s" % (head, who, ", ".join(names) if names else "очищено")
    if typ == "session_start":
        return "%s сессия начата «%s»: %s" % (head, e.get("name", ""), e.get("dir", ""))
    if typ == "session_stop":
        return "%s сессия остановлена (%s кадров): %s" % (head, e.get("frames", "?"), e.get("dir", ""))
    if typ == "link":
        return "%s монитор: %s" % (head, e.get("text") or link_text(e))
    rest = dict((k, v) for k, v in e.items() if k not in ("seq", "t", "type", "by"))
    return "%s %s %s: %s" % (head, who, typ, json.dumps(rest, ensure_ascii=False))


def link_text(e):
    """Состояние связи из события link или /coach/state.link."""
    if not e.get("connected"):
        return "связь с контроллером потеряна"
    sn = e.get("sniff")
    if isinstance(sn, list) and not any(sn):
        return "сниффер выключен — кадры не записываются"
    return "контроллер на связи" + (", сниффер включён" if isinstance(sn, list) else "")


def link_bad(lk):
    """Кадры не идут: нет связи с контроллером или сниффер выключен на всех каналах."""
    if not isinstance(lk, dict):
        return False
    if not lk.get("connected"):
        return True
    sn = lk.get("sniff")
    return isinstance(sn, list) and not any(sn)


def new_events(resp, since):
    evs = [e for e in (resp.get("events") or []) if isinstance(e, dict) and isinstance(e.get("seq"), (int, float))]
    return sorted([e for e in evs if e["seq"] > since], key=lambda e: e["seq"])


# ---------------------------------------------------------------- разбор аргументов

def split_buttons(s):
    """Разделитель кнопок: «|», если он есть в строке (тогда запятые остаются в надписях),
    иначе запятая. «\\,» — запятая внутри надписи при разделителе-запятой."""
    if "|" in s:
        return s.split("|")
    parts, cur, i = [], "", 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] == ",":
            cur += ","; i += 2; continue
        if s[i] == ",":
            parts.append(cur); cur = ""; i += 1; continue
        cur += s[i]; i += 1
    parts.append(cur)
    return parts


def parse_buttons(s):
    """«Начал=rpm2000,Закончил=-,Не вышло» → кнопки SPEC §7.
    =имя — начать фазу, =- — закончить фазу, без = — фазу не трогать.
    Надпись с запятой: разделяй кнопки «|» («Темно, с пассажиром|Светло») или пиши «\\,»."""
    if s is None:
        return None
    btns = []
    for part in split_buttons(s):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            label, phase = part.rsplit("=", 1)
            label, phase = label.strip(), phase.strip()
            if phase == "-":
                phase = ""
            elif phase == "":
                raise CarError("кнопка «%s»: после «=» нужно имя фазы или «-» (закончить фазу)" % part)
            elif PHASE_BAD.search(phase):
                raise CarError("кнопка «%s»: имя фазы без пробелов и запятых (например rpm2000, idle, brake)" % part)
        else:
            label, phase = part, None
        if not label:
            raise CarError("кнопка «%s»: пустая надпись" % part)
        btns.append({"label": label, "phase": phase})
    if not btns:
        raise CarError("--buttons: ни одной кнопки")
    return btns


def parse_field(s):
    """«Имя=CH:ID:BYTE.BIT:LEN:ORDER[:s][*F][+O]@единица» → поле для /watch."""
    s = s.strip()
    name = None
    rest = s
    if "=" in s:
        name, rest = s.split("=", 1)
        name = name.strip()
    unit = ""
    if "@" in rest:
        rest, unit = rest.split("@", 1)
        unit = unit.strip()
    spec = rest.strip()
    try:
        parse_parts(spec)
    except SpecSyntaxError as e:
        raise CarError("неверная спецификация поля «%s»: %s; формат CH:ID:BYTE.BIT:LEN:ORDER[:s][*FACTOR][+OFFSET], "
                       "например 0:0C9:1.7:16:be*0.25" % (spec, e))
    return {"name": name or spec, "spec": spec, "unit": unit}


def firmware_note(spec):
    """SPEC §2: прошивка читает только le; be длиннее байта (не укладывающееся в один байт) — не читается.
    29-битный ID не больше 7FF прошивка примет за 11-битный."""
    try:
        p = parse_parts(spec)
    except SpecSyntaxError:
        return None
    notes = []
    if p["order"] == "be" and p["length"] > p["bit"] + 1:
        notes.append("поле %s — Motorola (be) через границу байта: прошивка UniversalController такое пока не читает" % spec)
    if p["ext"] and p["id"] <= 0x7FF:
        notes.append("ID %08X — 29-битный, но не больше 7FF: прошивка UniversalController в конфиге примет его "
                     "за 11-битный" % p["id"])
    return "; ".join(notes) or None


# ---------------------------------------------------------------- команды

def cmd_status(mon, args):
    st = mon.get("/coach/state")
    mon.board = st.get("board") or mon.board
    data = mon.get("/data")
    try:
        ids = mon.get("/can/ids")
    except CarError:
        ids = None
    try:
        stat = mon.text("/status").strip()
    except CarError:
        stat = ""
    board = mon.board or (ids or {}).get("board") or "?"
    update_state(lambda s: s.update(port=mon.port, board=board))
    if args.json:
        dump({"port": mon.port, "board": board, "status": stat, "coach": st, "data": data,
              "fps": (ids or {}).get("fps")})
        return EXIT_OK

    problems = []
    out("Монитор UC_%s: http://127.0.0.1:%d  (страница пользователя: http://127.0.0.1:%d/assist)"
        % (board, mon.port, mon.port))
    com = data.get("com_port") or ""
    connected = bool(data.get("connected"))
    age = data.get("age_ms")
    if com == "DEMO":
        out("Контроллер: ДЕМО-режим (кадры имитирует монитор, машины нет)")
    elif connected:
        s = "Контроллер: подключён (%s)" % com
        if isinstance(age, (int, float)) and age > 3000:
            s += ", но диагностика не приходит %s" % dur(age / 1000.0)
            problems.append("контроллер молчит — проверьте USB и питание")
        out(s)
    else:
        out("Контроллер: НЕ подключён — монитор ищет COM-порт")
        problems.append("контроллер не подключён к монитору (USB)")

    d = data.get("data") if isinstance(data.get("data"), dict) else {}
    sn = d.get("sniff") if isinstance(d.get("sniff"), dict) else None
    can = d.get("can") if isinstance(d.get("can"), list) else []
    tx_supported = isinstance(d.get("tx"), dict)     # прошивка с передачей (app/can_tx.c) отдаёт секцию tx
    fps = (ids or {}).get("fps") or []
    nch = int(st.get("channels") or (sn or {}).get("ch") or (ids or {}).get("channels") or 3)
    if sn is None:
        if connected:
            out("Сниффер: нет в прошивке")
            problems.append("прошивка без сниффера (нет «sniff» в диагностике) — прошейте версию с app/can_sniff.c")
    else:
        on = sn.get("on") or []
        parts = []
        active_chans = []
        any_on = False
        for c in range(nch):
            is_on = c < len(on) and bool(on[c])
            any_on = any_on or is_on
            cfg = can[c] if c < len(can) and isinstance(can[c], dict) else {}
            f = fps[c] if c < len(fps) else None
            if not is_on:
                parts.append("CAN%d выкл" % c)
                continue
            s = "CAN%d ВКЛ %s кбит/с" % (c, cfg.get("bitrate", "?"))
            if cfg.get("mode") == "FD":
                s += " FD/%s" % cfg.get("dbitrate", "?")
            if cfg.get("silent"):
                s += " молча"
            elif "silent" in cfg:
                if tx_supported:
                    s += " АКТИВНЫЙ (передача) — пишет в шину"    # осознанный active on (car.py active on)
                    active_chans.append(c)
                else:
                    s += " НЕ молча (подтверждает кадры и передаёт TX конфига!)"
                    problems.append("CAN%d слушается не в режиме «молча»: контроллер подтверждает кадры (ACK) и "
                                    "передаёт в шину кадры своего конфига — выключите сниффер (car.py sniff off) "
                                    "и включите заново прошивкой, где сниффер всегда «молча»; при неизвестной "
                                    "скорости шины машины этот канал не включать" % c)
            if f is not None:
                s += ", %s кадр/с" % f
                if f == 0:
                    s += " (кадров нет: скорость шины? CAN-H/L? зажигание?)"
            lec = cfg.get("lec")
            if isinstance(lec, int) and lec not in (0, 7):
                s += ", ошибки шины lec=%d" % lec
            parts.append(s)
        out("Сниффер: " + "; ".join(parts))
        if sn.get("drop") or sn.get("lost"):
            out("  потери кадров: drop %s, lost %s (поток больше, чем пропускает USB?)" % (sn.get("drop"), sn.get("lost")))
        if active_chans:
            out("  ВНИМАНИЕ: %s в активном режиме — ПИШУТ в шину машины (вернуть в тихий: car.py active off)."
                % ", ".join("CAN%d" % c for c in active_chans))
        if not any_on:
            problems.append("сниффер выключен — car.py sniff on")

    ses = st.get("session") or {}
    if ses.get("active"):
        out("Сессия: «%s» идёт %s, %s кадров, %s событий" % (
            ses.get("name"), dur(time.time() - float(ses.get("started") or time.time())),
            ses.get("frames", "?"), ses.get("events", "?")))
        out("  " + str(ses.get("dir")))
    else:
        s = "Сессия: не идёт"
        if ses.get("last_dir"):
            s += " (последняя: %s)" % ses.get("last_dir")
        out(s)
    out("Фаза: " + ("«%s»" % st.get("phase") if st.get("phase") else "нет"))
    say = st.get("say")
    if isinstance(say, dict):
        out("Инструкция #%s «%s» — %s" % (say.get("id", say.get("seq")), short(say.get("text"), 70),
                                          "ответ получен" if st.get("answered") else "ждёт ответа"))
    w = st.get("watch") or []
    if w:
        out("Наблюдение: " + ", ".join("%s [%s]" % (f.get("name"), f.get("spec")) for f in w))
    out("Событий: seq %s" % st.get("seq", 0))
    if problems:
        out("НЕ ГОТОВО: " + "; ".join(problems))
    else:
        out("Готово к работе.")
    return EXIT_OK


def _sniff_state(mon):
    data = mon.get("/data")
    d = data.get("data") if isinstance(data.get("data"), dict) else {}
    sn = d.get("sniff") if isinstance(d.get("sniff"), dict) else None
    return data, sn


def _can_list(data):
    """Список диагностики каналов can[] из /data (или пустой)."""
    d = data.get("data") if isinstance(data.get("data"), dict) else {}
    return d.get("can") if isinstance(d.get("can"), list) else []


def cmd_sniff(mon, args):
    on = args.state == "on"
    body = {"cmd": "sniff"}
    if args.ch is not None:
        if not 0 <= args.ch <= 2:
            raise CarError("--ch: канал 0..2")
        body["ch"] = args.ch
    body["on"] = 1 if on else 0
    if on:
        if args.br:
            body["br"] = args.br
        if args.fd is not None:
            body["fd"] = args.fd
        if args.dbr:
            body["dbr"] = args.dbr
    elif args.br or args.fd is not None or args.dbr:
        raise CarError("--br/--fd/--dbr имеют смысл только для «sniff on»")

    data, sn = _sniff_state(mon)
    demo = data.get("com_port") == "DEMO"
    if not data.get("connected"):
        raise CarError("контроллер не подключён к монитору — проверьте USB, монитор сам найдёт COM-порт")
    diag = data.get("data")
    if sn is None and isinstance(diag, dict) and "uptime_ms" in diag:
        raise CarError("в диагностике контроллера нет «sniff» — прошивка без сниффера; "
                       "прошейте версию с app/can_sniff.c")
    nch = int((sn or {}).get("ch") or 3)
    if args.ch is not None and args.ch >= nch:
        raise CarError("у этой платы %d канал(а) CAN: --ch 0..%d" % (nch, nch - 1))

    r = mon.post("/cmd", body)
    if not r.get("ok"):
        raise CarError("монитор не смог передать команду контроллеру (COM-порт закрыт?)")

    # подтверждение: ждём, пока диагностика контроллера покажет нужное состояние
    want = [args.ch] if args.ch is not None else list(range(nch))
    deadline = time.monotonic() + args.confirm
    ok = False
    while True:
        data, sn = _sniff_state(mon)
        if sn is not None:
            st = sn.get("on") or []
            ok = all(c < len(st) and bool(st[c]) == on for c in want)
        if ok or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    # «молча»: канал, включённый в конфиге в режиме Normal, со старой прошивкой остаётся Normal — контроллер
    # подтверждает кадры и передаёт TX конфига (прошивка со сниффером «всегда молча» так не делает)
    loud = []
    if ok and on and sn is not None:
        d = data.get("data") if isinstance(data.get("data"), dict) else {}
        can = d.get("can") if isinstance(d.get("can"), list) else []
        st = sn.get("on") or []
        for c in want:
            cfg = can[c] if c < len(can) and isinstance(can[c], dict) else {}
            if c < len(st) and st[c] and "silent" in cfg and not cfg.get("silent"):
                loud.append(c)
    if args.json:
        dump({"ok": ok and not loud, "sent": body, "sniff": sn, "not_silent": loud, "demo": demo})
        return EXIT_OK if ok and not loud else EXIT_ERROR
    if sn is None:
        raise CarError("команда отправлена, но в диагностике нет «sniff» — прошивка без сниффера? "
                       "Прошейте версию с app/can_sniff.c")
    st = sn.get("on") or []
    line = ", ".join("CAN%d %s" % (c, "ВКЛ" if c < len(st) and st[c] else "выкл") for c in range(nch))
    if ok and loud:
        out("Сниффер: " + line)
        raise CarError("НЕ ГОТОВО: %s не в режиме «молча» — контроллер подтверждает кадры и передаёт в шину кадры "
                       "своего конфига. Выключите: car.py sniff off%s; прошейте версию, где сниффер всегда «молча», "
                       "или выключите этот канал в конфиге, если шина машины неизвестна"
                       % (", ".join("CAN%d" % c for c in loud), (" --ch %d" % loud[0]) if len(loud) == 1 else ""))
    if ok:
        out("Сниффер: " + line + (" (демо: команду выполнила демо-машина)" if demo else ""))
        if on:
            out("Проверьте поток: car.py status (кадр/с по каналам).")
        return EXIT_OK
    raise CarError("контроллер не подтвердил за %s с; сейчас: %s" % (num(args.confirm), line))


# ---------------------------------------------------------------- передача (TX, SPEC §11)

def _chan_word(cfg):
    """Как канал показывается в статусе передачи: активный (пишет) или молча."""
    return "АКТИВНЫЙ (передача)" if cfg.get("silent") == 0 else "молча"


def _chan_speed_word(cfg):
    """Скорость канала для подтверждения: «125 кбит/с» или «500 кбит/с FD/2000»."""
    br = cfg.get("bitrate")
    if not br:
        return "скорость ?"
    s = "%d кбит/с" % br
    if cfg.get("mode") == "FD":
        d = cfg.get("dbitrate")
        s += " FD" + ("/%d" % d if d else "")
    return s


def cmd_active(mon, args):
    """Активный режим канала (Normal) для передачи — это sniff с флагом tx (SPEC §11.1).
    Канал начинает подтверждать кадры (ACK) и может слать. car.py сам канал в активный НЕ переводит нигде,
    кроме этой явной команды."""
    on = args.state == "on"
    if on and args.ch is None:
        raise CarError("active on: укажите канал — car.py active on --ch N [--br 125]")
    if args.ch is not None and not 0 <= args.ch <= 2:
        raise CarError("--ch: канал 0..2")
    if not on and (args.br or args.fd is not None or args.dbr):
        raise CarError("--br/--fd/--dbr имеют смысл только для «active on»")

    data, sn = _sniff_state(mon)
    demo = data.get("com_port") == "DEMO"
    if not data.get("connected"):
        raise CarError("контроллер не подключён к монитору — проверьте USB, монитор сам найдёт COM-порт")
    diag = data.get("data")
    if sn is None and isinstance(diag, dict) and "uptime_ms" in diag:
        raise CarError("в диагностике контроллера нет «sniff» — прошивка без сниффера/передачи; "
                       "прошейте версию с app/can_sniff.c и app/can_tx.c")
    nch = int((sn or {}).get("ch") or 3)
    if args.ch is not None and args.ch >= nch:
        raise CarError("у этой платы %d канал(а) CAN: --ch 0..%d" % (nch, nch - 1))

    want = [args.ch] if args.ch is not None else list(range(nch))
    can = _can_list(data)
    if on and not args.json:
        out("ВНИМАНИЕ: активный режим (Normal) — CAN%d начнёт ПИСАТЬ в шину машины: подтверждать кадры (ACK) "
            "и передавать заданные слоты. Включай только на стоянке и осознанно." % args.ch)

    # Скорость канала сохраняем ЯВНО: и при active off (вернуть на ту же скорость прослушки, а не на
    # конфиг — прошивка при выходе из режима иначе откатывает канал к скорости из конфига), и при
    # active on без --br (не сбрасывать текущую). Текущую скорость берём из диагностики can[ch].
    sent = []
    for c in want:
        cur = can[c] if c < len(can) and isinstance(can[c], dict) else {}
        cur_br = int(cur.get("bitrate") or 0)
        cur_fd = 1 if cur.get("mode") == "FD" else 0
        cur_dbr = int(cur.get("dbitrate") or 0)
        br = (args.br if (on and args.br) else 0) or cur_br
        fd = args.fd if (on and args.fd is not None) else cur_fd
        dbr = (args.dbr if (on and args.dbr) else 0) or (cur_dbr if fd else 0)
        b = {"cmd": "sniff", "on": 1, "ch": c, "tx": 1 if on else 0}
        if br:
            b["br"] = br
        b["fd"] = 1 if fd else 0
        if fd and dbr:
            b["dbr"] = dbr
        r = mon.post("/cmd", b)
        if not r.get("ok"):
            raise CarError("монитор не смог передать команду контроллеру (COM-порт закрыт?)")
        sent.append(b)
    body = sent[0] if len(sent) == 1 else {"cmd": "sniff", "multi": sent}

    target = 0 if on else 1                       # silent: 0 — активный, 1 — молча
    deadline = time.monotonic() + args.confirm
    ok = False
    while True:
        data, sn = _sniff_state(mon)
        can = _can_list(data)
        ok = all(c < len(can) and isinstance(can[c], dict) and can[c].get("silent") == target for c in want)
        if ok or time.monotonic() >= deadline:
            break
        time.sleep(0.25)

    can = _can_list(data)
    line = ", ".join("CAN%d — %s (%s)" % (c, _chan_word(cc), _chan_speed_word(cc))
                     for c in want for cc in [can[c] if c < len(can) and isinstance(can[c], dict) else {}])
    if args.json:
        dump({"ok": ok, "sent": body, "channels": line, "demo": demo})
        return EXIT_OK if ok else EXIT_ERROR
    if not ok:
        raise CarError("контроллер не подтвердил режим за %s с; сейчас: %s" % (num(args.confirm), line))
    if on:
        out("Активный режим включён: " + line + (" (демо)" if demo else ""))
        out("Теперь можно слать кадры: car.py tx %d <ID> <DATA> [--period MS]. "
            "Вернуть в тихий режим: car.py active off --ch %d" % (args.ch, args.ch))
        if args.br and args.br <= 125:
            out("ВНИМАНИЕ: на медленной шине (%d кбит/с) несколько периодических слотов с малым периодом "
                "могут насытить шину — держи суммарный поток кадров умеренным." % args.br)
    else:
        out("Активный режим выключен, канал снова слушает молча: " + line)
        out("Слоты передачи этого канала остановлены (прошивка снимает их при выходе из активного режима).")
    return EXIT_OK


def _parse_tx_id(s):
    """ID кадра из hex-строки → (hex-строка без 0x в верхнем регистре, число, 29-бит по умолчанию)."""
    s = (s or "").strip()
    if s[:2].lower() == "0x":
        s = s[2:]
    if not s or not HEX_RE.match(s):
        raise CarError("ID: hex-цифры, например 384 или 18DAF110 (сейчас «%s»)" % s)
    if len(s) > 8:
        raise CarError("ID: не больше 8 hex-цифр: %s" % s)
    val = int(s, 16)
    if val > 0x1FFFFFFF:
        raise CarError("ID больше 29 бит: %s" % s)
    ext_auto = len(s) == 8 or val > 0x7FF          # SPEC §2: 8 цифр или больше 7FF — 29-битный
    return s.upper(), val, ext_auto


def _parse_tx_data(s, fd):
    """Данные из hex (пробелы между байтами допускаются) → (hex-строка в верхнем регистре, число байт)."""
    s = "".join((s or "").split())
    if s[:2].lower() == "0x":
        s = s[2:]
    if len(s) % 2 != 0:
        raise CarError("DATA: чётное число hex-цифр (по 2 на байт): «%s»" % s)
    if s and not HEX_RE.match(s):
        raise CarError("DATA: hex-байты, например 0011AA (сейчас «%s»)" % s)
    n = len(s) // 2
    maxlen = 64 if fd else 8
    if n > maxlen:
        raise CarError("DATA: %d байт — больше %d (%s)" % (
            n, maxlen, "CAN FD ≤ 64 байт" if fd else "classic CAN ≤ 8 байт; для FD добавь --fd"))
    return s.upper(), n


def cmd_tx(mon, args):
    """Передать кадр (POST /tx, SPEC §11.4). Без --period — один раз, иначе периодический слот.
    Если канал не в активном режиме — отказ (код 3); канал в активный car.py сам не переводит."""
    ch = args.ch
    if not 0 <= ch <= 2:
        raise CarError("канал 0..2")
    hexid, _val, ext_auto = _parse_tx_id(args.id)
    ext = 1 if (args.ext or ext_auto) else 0
    fd = 1 if args.fd else 0
    brs = 1 if args.brs else 0
    data_hex, dlc = _parse_tx_data(args.data, fd)
    period = args.period or 0
    count = args.count or 0
    if period and period < 5:
        raise CarError("--period: минимум 5 мс (защита шины от заливания)")
    if args.count is not None and not period:
        raise CarError("--count имеет смысл только с --period (периодическая отправка)")
    if brs and not fd:
        raise CarError("--brs (ускорение фазы данных) бывает только у FD-кадра — добавь --fd")

    data, sn = _sniff_state(mon)
    demo = data.get("com_port") == "DEMO"
    if not data.get("connected"):
        raise CarError("контроллер не подключён к монитору — проверьте USB")
    nch = int((sn or {}).get("ch") or 3)
    if ch >= nch:
        raise CarError("у этой платы %d канал(а) CAN: канал 0..%d" % (nch, nch - 1))
    on_list = (sn or {}).get("on") or []
    is_on = ch < len(on_list) and bool(on_list[ch])
    can = _can_list(data)
    cfg = can[ch] if ch < len(can) and isinstance(can[ch], dict) else {}
    if not is_on:
        raise CarError("CAN%d выключен — сначала включите активный режим: car.py active on --ch %d "
                       "(канал начнёт писать в шину)" % (ch, ch))
    if cfg.get("silent") != 0:
        raise CarError("CAN%d слушает молча, передача запрещена. Включите активный режим осознанно: "
                       "car.py active on --ch %d — канал начнёт ПИСАТЬ в шину машины" % (ch, ch))

    body = {"ch": ch, "id": hexid, "ext": ext, "fd": fd, "brs": brs, "dlc": dlc, "data": data_hex,
            "period": period, "count": count}
    if args.slot is not None:
        body["slot"] = args.slot
    r = mon.post("/tx", body)
    if not r.get("ok"):
        raise CarError(r.get("error") or ("монитор не принял передачу: %s" % r))
    slot = r.get("slot")
    if args.json:
        dump(r)
        return EXIT_OK
    kind = "29-бит" if ext else "11-бит"
    fl = ("" if not fd else " FD") + ("" if not brs else " BRS")
    where = "CAN%d" % ch + (" (демо)" if demo else "")
    if period:
        cnt = ("%d раз" % count) if count else "без предела"
        out("Периодическая передача в %s: ID %s [%s%s], данные %s, период %d мс, %s (слот %s)"
            % (where, hexid, kind, fl, data_hex or "(пусто)", period, cnt, slot))
        out("Остановить: car.py tx-stop %s  (все слоты: car.py tx-stop)" % slot)
        br = cfg.get("bitrate") or 0
        if period <= 10 and br and br <= 125:
            out("ВНИМАНИЕ: период %d мс на шине %d кбит/с — большой поток; несколько таких слотов "
                "могут насытить кузовную шину. Следи за счётчиком ошибок (car.py status)." % (period, br))
    else:
        out("Отправлен кадр в %s: ID %s [%s%s], данные %s (слот %s)"
            % (where, hexid, kind, fl, data_hex or "(пусто)", slot))
    return EXIT_OK


def cmd_tx_list(mon, args):
    """Активные каналы и слоты передачи (GET /tx, SPEC §11.4)."""
    r = mon.get("/tx")
    if args.json:
        dump(r)
        return EXIT_OK
    active = r.get("active") or []
    slots = r.get("slots") or []
    out("Каналы: " + (", ".join("CAN%d %s" % (c, "АКТИВНЫЙ (передача)" if a else "молча")
                                 for c, a in enumerate(active)) or "—"))
    if not slots:
        out("Слотов передачи нет.")
        return EXIT_OK
    for s in slots:
        kind = "29-бит" if s.get("ext") else "11-бит"
        per = s.get("period") or 0
        if per:
            cnt = s.get("count") or 0
            tail = "период %d мс, отправлено %s%s" % (
                per, s.get("sent", "?"), (" из %d" % cnt) if cnt else " (без предела)")
        else:
            tail = "одиночная, отправлено %s" % s.get("sent", "?")
        payload = s.get("data")
        payload = ("данные %s" % payload) if payload else ("%s байт" % s.get("dlc", "?"))
        out("Слот %s: CAN%s ID %s [%s], %s — %s"
            % (s.get("slot"), s.get("ch"), s.get("id"), kind, payload, tail))
    return EXIT_OK


def cmd_tx_stop(mon, args):
    """Остановить слот передачи или все (POST /tx/stop, SPEC §11.4)."""
    body = {} if args.slot is None else {"slot": args.slot}
    r = mon.post("/tx/stop", body)
    if not r.get("ok"):
        raise CarError(r.get("error") or ("монитор не остановил передачу: %s" % r))
    if args.json:
        dump(r)
        return EXIT_OK
    out("Остановлены все слоты передачи." if args.slot is None else "Слот %d остановлен." % args.slot)
    return EXIT_OK


_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def cmd_session(mon, args):
    act = args.action
    if act == "start":
        name = args.name or ""
        clean = _NAME_RE.sub("", name) or "session"
        r = mon.post("/session/start", {"name": name})
        if not r.get("ok"):
            raise CarError("монитор не начал сессию: %s" % (r.get("error") or r))
        update_state(lambda s: s.update(session_dir=r.get("dir")))
        warn = []
        try:
            st = mon.get("/coach/state")
            ph = st.get("phase") or ""
        except CarError:
            ph = ""
        if ph:
            warn.append("идёт фаза «%s» — она попадёт в запись с её начала (закончить: car.py phase -)" % ph)
        try:
            data, sn = _sniff_state(mon)
            if not data.get("connected"):
                warn.append("контроллер не подключён — кадров в записи не будет (USB, питание)")
            elif sn is not None and not any(sn.get("on") or []):
                warn.append("сниффер выключен — кадры НЕ записываются: car.py sniff on")
            elif sn is None and isinstance(data.get("data"), dict) and "uptime_ms" in data["data"]:
                warn.append("прошивка без сниффера — кадры шины записываться не будут")
        except CarError:
            pass
        if args.json:
            r["warnings"] = warn
            dump(r)
            return EXIT_OK
        if name and clean != name:
            out("Имя очищено до «%s» (допустимы A-Z a-z 0-9 _ -)." % clean)
        for w in warn:
            out("ВНИМАНИЕ: " + w)
        out("Сессия начата, запись идёт:")
        out(str(r.get("dir")))                       # папка — последней строкой
        return EXIT_OK
    if act == "stop":
        if args.name:
            raise CarError("session stop без имени")
        r = mon.post("/session/stop", {})
        if args.json:
            dump(r)
            return EXIT_OK
        if not r.get("ok"):
            out("Активной сессии не было.")
            return EXIT_OK
        update_state(lambda s: s.update(session_dir=r.get("dir")))
        out("Сессия остановлена: %s кадров, %s событий" % (r.get("frames", "?"), r.get("events", "?")))
        out(str(r.get("dir")))
        return EXIT_OK
    r = mon.get("/session")
    if args.json:
        dump(r)
        return EXIT_OK
    if r.get("active"):
        out("Сессия «%s» идёт %s: %s кадров, %s событий, фаза %s" % (
            r.get("name"), dur(time.time() - float(r.get("started") or time.time())), r.get("frames", "?"),
            r.get("events", "?"), ("«%s»" % r["phase"]) if r.get("phase") else "нет"))
        out(str(r.get("dir")))
    else:
        out("Сессия не идёт. Последняя:")
        out(str(r.get("last_dir") or "—"))
    return EXIT_OK


def build_say(args):
    text = " ".join(args.text).replace("\\n", "\n").strip()
    if not text:
        raise CarError("пустой текст инструкции")
    if args.label and not args.input:
        raise CarError("--label задаёт подпись поля ввода — нужен ещё --input number|text")
    body = {"text": text, "input": args.input or "none", "input_label": args.label or ""}
    btns = parse_buttons(args.buttons)
    if btns is not None:
        body["buttons"] = btns
    return body


def do_say(mon, args):
    body = build_say(args)
    r = mon.post("/coach/say", body)
    if not r.get("ok") or not isinstance(r.get("id"), (int, float)):
        raise CarError("монитор не принял инструкцию: %s" % (r.get("error") or r))
    return int(r["id"]), body, r


def _say_lines(sid, body):
    lines = ["Инструкция #%d на экране: «%s»" % (sid, short(body["text"], 120))]
    btns = body.get("buttons")
    if btns or body.get("input") == "none":
        btns = btns or [{"label": "Готово", "phase": None}]
        lines.append("  кнопки: " + ", ".join(fmt_button(b) for b in btns))
    if body.get("input") != "none":
        lines.append("  поле ввода (%s): «%s»" % (body["input"], body.get("input_label", "")))
    return lines


def cmd_say(mon, args):
    sid, body, r = do_say(mon, args)
    if args.json:
        dump(r)
        return EXIT_OK
    for ln in _say_lines(sid, body):
        out(ln)
    out("Ждать ответ: car.py wait --say %d" % sid)
    return EXIT_OK


def long_poll(mon, since, secs, types):
    secs = max(1, int(math.ceil(secs)))
    path = "/coach/wait?since=%d&timeout=%d&types=%s" % (since, secs, types)
    for attempt in range(3):
        try:
            return mon.get(path, timeout=secs + 15)
        except NoMonitor:
            if attempt == 2:
                raise NoMonitor("связь с монитором потеряна (порт %d) — проверьте, что %s открыт, и повторите"
                                % (mon.port, exe_hint(mon.port, mon.board)))
            time.sleep(2.0)


def coach_state(mon):
    """/coach/state или None (монитор недоступен на миг — не повод прерывать ожидание)."""
    try:
        return mon.get("/coach/state")
    except CarError:
        return None


def await_link(mon, grace=LINK_GRACE):
    """Связь с контроллером или сниффер пропали: подождать, не восстановятся ли (переподключение USB,
    монитор сам повторяет команду сниффера). Возвращает (восстановилось, секунд, состояние link)."""
    t0 = time.monotonic()
    lk = None
    while True:
        st = coach_state(mon)
        lk = (st or {}).get("link")
        if lk is not None and not link_bad(lk):
            return True, time.monotonic() - t0, lk
        if time.monotonic() - t0 >= grace:
            return False, time.monotonic() - t0, lk
        time.sleep(1.0)


def link_lost_text(lk, session_active):
    what = link_text(lk or {"connected": False})
    return "ВНИМАНИЕ: %s%s. Проверьте USB и питание контроллера, затем car.py status и при необходимости car.py sniff on" % (
        what, " — кадры в запись НЕ пишутся" if session_active else "")


def _stale_line(e):
    typ = e.get("type")
    if typ == "button":
        return "  ВНИМАНИЕ: нажата кнопка «%s» инструкции #%s (не текущей)%s" % (
            e.get("label", ""), e.get("say"), fmt_phase(e.get("phase")))
    if typ == "input":
        return "  ВНИМАНИЕ: введено «%s» в инструкцию #%s (не текущую)" % (e.get("value", ""), e.get("say"))
    lab = e.get("label") or ""
    return "  ВНИМАНИЕ: пользователь: %s" % (("фаза «%s» началась" % lab) if lab else "фаза закончилась")


def wait_answer(mon, sid, timeout, seen, as_json):
    """Ждать кнопку/ввод на инструкцию sid. По пути печатает отметки и сообщения пользователя, нажатия
    кнопок прежних инструкций (они могут менять фазу), потерю связи с контроллером; бросает ожидание,
    если инструкцию заменили новой."""
    since = sid
    t0 = time.monotonic()
    deadline = t0 + timeout
    got, stale, answer, note, replaced, lost = [], [], None, None, None, None
    gaps = []                                           # (секунд) — связь прерывалась и восстановилась
    last_btn = None
    while answer is None:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        r = long_poll(mon, since, min(left, POLL_CHUNK), ASK_TYPES)
        rseq = int(r.get("seq") or 0)
        if rseq < since:
            raise CarError("монитор перезапускался — инструкция #%d потеряна; дайте её заново (ask)" % sid,
                           EXIT_NO_MONITOR)
        evs = new_events(r, since)
        bad_link = None
        for e in evs:
            since = max(since, int(e["seq"]))
            typ = e.get("type")
            if typ == "say":
                if int(e.get("id", e["seq"])) != sid:
                    replaced = e                            # новая инструкция — эта больше не на экране
                    break
                continue
            if typ == "link":
                bad_link = e if link_bad(e) else None      # важно последнее состояние
                continue
            if e.get("by") != "user":
                continue
            if typ in ("button", "input"):
                if e.get("say") == sid:
                    answer = e
                    break
                last_btn = int(e["seq"])
                if e["seq"] > seen:                         # кнопка прежней инструкции: может сменить фазу
                    stale.append(e)
                    if not as_json:
                        out(_stale_line(e))
                continue
            if typ == "phase":
                if last_btn is not None and int(e["seq"]) == last_btn + 1:
                    continue                                # фаза той же кнопки — уже показана
                if e["seq"] > seen:
                    stale.append(e)
                    if not as_json:
                        out(_stale_line(e))
                continue
            if typ not in ("mark", "note") or e["seq"] <= seen:
                continue                                    # уже видели раньше
            got.append(e)
            if not as_json:
                out("  " + fmt_event(e))
            if typ == "note":
                note = e
        if answer is not None or replaced is not None:
            break
        st = coach_state(mon)
        if st is not None:
            cur = (st.get("say") or {}).get("id")
            if cur is not None and int(cur) != sid:
                replaced = {"id": cur}                      # монитор старой версии не будит опрос на say
                break
            if bad_link is None and link_bad(st.get("link")):
                bad_link = st.get("link")
        if bad_link is not None:
            ok, secs, lk = await_link(mon)
            if not ok:
                lost = lk or bad_link
                break
            gaps.append(secs)
            if not as_json:
                out("  (связь с контроллером или сниффер прерывались ~%d с — в записи может быть пропуск)"
                    % max(1, round(secs)))
        if note is not None:
            break
        if not evs and not r.get("timeout"):
            time.sleep(0.2)                                 # защита от пустых мгновенных ответов
    elapsed = time.monotonic() - t0
    seqs = [int(e["seq"]) for e in got + stale] + ([int(answer["seq"])] if answer else [])
    if seqs:
        remember_seq(mon.port, max(seqs))
    st = coach_state(mon) or {}
    if answer:
        result = "answer"
    elif replaced is not None:
        result = "replaced"
    elif lost is not None:
        result = "link"
    elif note:
        result = "note"
    else:
        result = "timeout"
    if as_json:
        dump({"say": sid, "result": result, "answer": answer, "events": got, "stale": stale,
              "replaced_by": (replaced or {}).get("id") if replaced else None, "link": lost, "link_gaps": gaps,
              "phase": st.get("phase"), "elapsed": round(elapsed, 1)})
    elif answer:
        marks = sum(1 for e in got if e.get("type") == "mark")
        if answer.get("type") == "button":
            s = "Ответ: кнопка «%s»" % answer.get("label", "")
            ph = answer.get("phase")
            if ph:
                s += ", фаза «%s» началась" % ph
            elif ph == "":
                s += ", фаза закончилась"
        else:
            s = "Ответ: введено «%s»" % answer.get("value", "")
        s += " (#%s, через %s)" % (answer.get("seq"), dur(elapsed))
        if marks:
            s += "; отметок по пути: %d" % marks
        out(s)
        if stale:
            out("ВНИМАНИЕ: по пути нажимали кнопки прежних инструкций (выше) — фаза могла смениться; сейчас фаза: %s"
                % (("«%s»" % st.get("phase")) if st.get("phase") else "нет"))
    elif replaced is not None:
        out("Инструкция #%d заменена инструкцией #%s — ожидание снято (ответа на #%d уже не будет)."
            % (sid, replaced.get("id", replaced.get("seq")), sid))
    elif lost is not None:
        out(link_lost_text(lost, bool((st.get("session") or {}).get("active"))))
        out("Инструкция #%d остаётся на экране; ждать ответ дальше: car.py wait --say %d" % (sid, sid))
    elif note:
        cur = (st.get("say") or {}).get("id")
        if cur is None or int(cur) == sid:
            out("Пользователь написал сообщение вместо ответа. Инструкция #%d ещё на экране; "
                "ждать дальше: car.py wait --say %d" % (sid, sid))
        else:
            out("Пользователь написал сообщение. На экране уже инструкция #%s." % cur)
    else:
        out("Таймаут: за %s ответа на инструкцию #%d нет." % (dur(elapsed), sid))
    if answer:
        return EXIT_OK
    if replaced is not None:
        return EXIT_REPLACED
    if lost is not None:
        return EXIT_LINK
    return EXIT_NOTE if note else EXIT_TIMEOUT


def cmd_ask(mon, args):
    seen = last_seen(mon, current_seq(mon))
    sid, body, _ = do_say(mon, args)
    if not args.json:
        for ln in _say_lines(sid, body):
            out(ln)
        out("Жду ответ до %s…" % dur(args.timeout))
    return wait_answer(mon, sid, args.timeout, seen, args.json)


def _types(s):
    types = [t.strip() for t in (s or WAIT_TYPES).split(",") if t.strip()]
    bad = [t for t in types if t not in KNOWN_TYPES]
    if bad or not types:
        raise CarError("--types: неизвестные типы %s; допустимы %s" % (", ".join(bad) or "—", ", ".join(KNOWN_TYPES)))
    return types


def check_say_id(mon, sid):
    """wait --say N: N должно быть инструкцией (say), и именно она сейчас на экране у пользователя.
    Иначе ожидание бессмысленно: ответа на N не будет, а Claude простоит до таймаута (15 мин)."""
    r = mon.get("/coach/events?since=%d" % max(0, sid - 1))
    if int(r.get("seq") or 0) < sid:
        return                              # такого seq ещё нет — монитор перезапускался; скажет wait_answer
    evs = new_events(r, sid - 1)
    if not evs or int(evs[0]["seq"]) != sid:
        return                              # событие уже вытеснено из памяти монитора — проверить нельзя
    st = mon.get("/coach/state")
    cur = (st.get("say") or {}).get("id")
    if evs[0].get("type") != "say":
        raise CarError("#%d — не инструкция, а событие «%s»; на экране у пользователя инструкция #%s"
                       % (sid, evs[0].get("type"), cur if cur is not None else "— (нет)"))
    if cur is not None and int(cur) != sid:
        raise CarError("инструкция #%d уже заменена: на экране у пользователя #%s (ждать её: car.py wait --say %s)"
                       % (sid, cur, cur))


def cmd_wait(mon, args):
    seen = last_seen(mon, current_seq(mon))
    if args.say is not None:
        check_say_id(mon, args.say)
        return wait_answer(mon, args.say, args.timeout, seen, args.json)
    types = _types(args.types)
    since = args.since if args.since is not None else seen
    t0 = time.monotonic()
    deadline = t0 + args.timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        r = long_poll(mon, since, min(left, POLL_CHUNK), ",".join(types))
        if int(r.get("seq") or 0) < since:
            if not args.json:
                out("(монитор перезапускался — счёт событий начат заново)")
            since = 0
            continue
        evs = new_events(r, since)
        hits = [e for e in evs if e.get("by") == "user" and e.get("type") in types]
        if evs:
            since = int(evs[-1]["seq"])
        links = [e for e in evs if e.get("type") == "link"]
        if "link" in types and not hits and links and link_bad(links[-1]):
            ok, secs, lk = await_link(mon)
            if ok:
                if not args.json:
                    out("(связь с контроллером или сниффер прерывались ~%d с — в записи может быть пропуск)"
                        % max(1, round(secs)))
                continue
            st = coach_state(mon) or {}
            if args.json:
                dump({"seq": since, "events": links, "timeout": False, "link": lk})
            else:
                out(link_lost_text(lk or links[-1], bool((st.get("session") or {}).get("active"))))
            return EXIT_LINK
        if hits:
            remember_seq(mon.port, max(int(e["seq"]) for e in hits))
            if args.json:
                dump({"seq": r.get("seq"), "events": hits, "timeout": False})
            else:
                for e in hits:
                    out(fmt_event(e))
            return EXIT_OK
        if not evs and not r.get("timeout"):
            time.sleep(0.2)
    if args.json:
        dump({"seq": since, "events": [], "timeout": True})
    else:
        out("Таймаут: за %s событий от пользователя нет (seq %d)." % (dur(time.monotonic() - t0), since))
    return EXIT_TIMEOUT


def cmd_events(mon, args):
    cur = current_seq(mon)
    if args.since is not None:
        since = args.since
    elif args.last:
        since = max(0, cur - args.last)          # «последние N» — даже если уже видели
    else:
        since = last_seen(mon, cur)
    if since > cur and not args.json:
        out("(since %d больше последнего seq %d — монитор перезапускался? показываю всё)" % (since, cur))
        since = 0
    allev = []
    last = since
    for _ in range(10):                         # по 500 за запрос
        r = mon.get("/coach/events?since=%d" % last)
        evs = new_events(r, last)
        allev.extend(evs)
        if len(evs) < 500:
            break
        last = int(evs[-1]["seq"])
    if args.last:
        allev = allev[-args.last:]
    top = max([cur] + [int(e["seq"]) for e in allev])
    remember_seq(mon.port, top)
    if args.json:
        dump({"seq": top, "events": allev})
        return EXIT_OK
    if not allev:
        out("Новых событий нет (seq %d)." % top)
    for e in allev:
        out(fmt_event(e))
    return EXIT_OK


def cmd_phase(mon, args):
    label = " ".join(args.label).strip()
    if label == "-":
        label = ""
    if PHASE_BAD.search(label):
        raise CarError("имя фазы без пробелов и запятых (например rpm2000, idle); «-» — закончить фазу")
    r = mon.post("/coach/phase", {"label": label})
    if not r.get("ok"):
        raise CarError("монитор не принял фазу: %s" % (r.get("error") or r))
    if args.json:
        dump(r)
    else:
        out(("Фаза «%s» началась" % label if label else "Фаза закончилась") + (" (#%s)" % r["seq"] if "seq" in r else ""))
    return EXIT_OK


def cmd_mark(mon, args):
    label = " ".join(args.label).strip()
    r = mon.post("/coach/mark", {"label": label})
    if not r.get("ok"):
        raise CarError("монитор не принял отметку: %s" % (r.get("error") or r))
    if args.json:
        dump(r)
    else:
        out("Отметка%s поставлена%s" % ((" «%s»" % label) if label else "", (" (#%s)" % r["seq"]) if "seq" in r else ""))
    return EXIT_OK


def cmd_note(mon, args):
    text = " ".join(args.text).replace("\\n", "\n").strip()
    if not text:
        raise CarError("пустое сообщение")
    r = mon.post("/coach/note", {"text": text})
    if not r.get("ok"):
        raise CarError("монитор не принял сообщение: %s" % (r.get("error") or r))
    if args.json:
        dump(r)
    else:
        out("Сообщение в журнале страницы" + ((" (#%s)" % r["seq"]) if "seq" in r else ""))
    return EXIT_OK


def _print_values(r, compact):
    vals = r.get("values") or []
    if compact:
        parts = []
        for v in vals:
            parts.append("%s=%s%s" % (v.get("name"), num(v.get("value")), (" " + v["unit"]) if v.get("unit") else ""))
        out("%s  %s" % (hms(r.get("t") or time.time()), "  ".join(parts) if parts else "(наблюдение пусто)"))
        return
    if not vals:
        out("Наблюдение пусто: car.py watch set \"Имя=SPEC@единица\"")
        return
    w = max(len(str(v.get("name", ""))) for v in vals)
    for v in vals:
        name = str(v.get("name", "")).ljust(w)
        if v.get("value") is None:
            out("%s = нет данных (кадров этого ID нет или поле длиннее данных)   %s" % (name, v.get("spec")))
            continue
        age = v.get("age_ms")
        s = "%s = %s%s   (raw %s" % (name, num(v.get("value")), (" " + v["unit"]) if v.get("unit") else "", num(v.get("raw")))
        if isinstance(age, (int, float)):
            s += ", %s" % (("%d мс назад" % age) if age < 2000 else ("УСТАРЕЛО: %s назад" % dur(age / 1000.0)))
        s += ")   %s" % v.get("spec")
        out(s)


def cmd_watch(mon, args):
    act = args.action or "list"
    if act in ("set", "add"):
        if not args.fields:
            raise CarError("укажите поля: \"Имя=CH:ID:BYTE.BIT:LEN:ORDER[:s][*FACTOR][+OFFSET]@единица\"")
        new = [parse_field(f) for f in args.fields]
        if act == "add":
            names = set(f["name"] for f in new)
            cur = [f for f in (mon.get("/watch").get("fields") or []) if f.get("name") not in names]
            fields = cur + new
        else:
            fields = new
        if len(fields) > MAX_WATCH:
            raise CarError("в наблюдении не больше %d полей (получилось %d)" % (MAX_WATCH, len(fields)))
        r = mon.post("/watch", {"fields": fields})
        if not r.get("ok"):
            raise CarError("монитор отклонил поле: %s" % (r.get("error") or r))
        if args.json:
            dump(r)
            return EXIT_OK
        out("Наблюдение (%d): %s" % (len(fields), ", ".join("%s [%s]" % (f["name"], f["spec"]) for f in fields)))
        for f in new:
            n = firmware_note(f["spec"])
            if n:
                out("ВНИМАНИЕ: " + n)
        out("Пользователь видит живые значения на странице /assist.")
        return EXIT_OK
    if args.fields:
        raise CarError("watch %s не принимает полей" % act)
    if act == "clear":
        r = mon.post("/watch", {"fields": []})
        if not r.get("ok"):
            raise CarError("монитор не очистил наблюдение: %s" % (r.get("error") or r))
        if args.json:
            dump(r)
        else:
            out("Наблюдение очищено.")
        return EXIT_OK
    if act == "values":
        n = max(1, args.count)
        res = []
        for i in range(n):
            r = mon.get("/watch/values")
            if args.json:
                res.append(r)
            else:
                _print_values(r, compact=n > 1)
            if i + 1 < n:
                time.sleep(max(0.05, args.every))
        if args.json:
            dump(res[0] if n == 1 else res)
        return EXIT_OK
    r = mon.get("/watch")
    if args.json:
        dump(r)
        return EXIT_OK
    fields = r.get("fields") or []
    if not fields:
        out("Наблюдение пусто.")
    for f in fields:
        out("%s = %s%s" % (f.get("name"), f.get("spec"), (" @" + f["unit"]) if f.get("unit") else ""))
    return EXIT_OK


# ---------------------------------------------------------------- CLI

class RuParser(argparse.ArgumentParser):
    def error(self, message):
        # код 2 занят под «таймаут» — ошибки аргументов дают 3
        self.print_usage(sys.stderr)
        sys.stderr.write("ОШИБКА аргументов: %s\n" % message)
        sys.exit(EXIT_ERROR)


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=argparse.SUPPRESS,
                        help="HTTP-порт монитора (иначе UC_PORT, иначе автопоиск 8473/8723/8503)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="сырой JSON вместо текста")

    p = RuParser(prog="car.py", parents=[common],
                 description="Команды монитору UC_<плата>_Monitor.exe для совместного поиска сигналов в CAN (SPEC §9).",
                 epilog="Коды выхода: 0 — ок; 1 — монитор недоступен; 2 — таймаут; 3 — ошибка; 4 — вместо ответа пришло "
                        "сообщение; 5 — инструкцию заменили новой; 6 — потеряна связь с контроллером / сниффер выключен.")
    sub = p.add_subparsers(dest="command", metavar="КОМАНДА", parser_class=RuParser)
    sub.required = True

    s = sub.add_parser("status", parents=[common], help="плата, связь, сниффер, кадр/с, сессия, фаза")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("sniff", parents=[common], help="включить/выключить сниффер: sniff on|off [--ch N]")
    s.add_argument("state", choices=["on", "off"])
    s.add_argument("--ch", type=int, help="канал 0..2 (по умолчанию все)")
    s.add_argument("--br", type=int, help="скорость, кбит/с (по умолчанию как в конфиге)")
    s.add_argument("--fd", type=int, choices=[0, 1], help="0 — классический CAN, 1 — CAN FD")
    s.add_argument("--dbr", type=int, help="скорость фазы данных FD, кбит/с")
    s.add_argument("--confirm", type=float, default=4.0, help="сколько секунд ждать подтверждения от контроллера")
    s.set_defaults(func=cmd_sniff)

    s = sub.add_parser("active", parents=[common],
                       help="активный режим канала для передачи (ПИШЕТ в шину!): active on --ch N | active off [--ch N]")
    s.add_argument("state", choices=["on", "off"])
    s.add_argument("--ch", type=int, help="канал 0..2 (для «on» обязателен)")
    s.add_argument("--br", type=int, help="скорость шины, кбит/с (для кузовной обычно 125)")
    s.add_argument("--fd", type=int, choices=[0, 1], help="0 — классический CAN, 1 — CAN FD")
    s.add_argument("--dbr", type=int, help="скорость фазы данных FD, кбит/с")
    s.add_argument("--confirm", type=float, default=4.0, help="сколько секунд ждать подтверждения от контроллера")
    s.set_defaults(func=cmd_active)

    s = sub.add_parser("tx", parents=[common], help="передать кадр в шину: tx CH ID DATA [--period MS] (нужен active on)")
    s.add_argument("ch", type=int, help="канал 0..2")
    s.add_argument("id", help="ID кадра в hex: 384, 18DAF110")
    s.add_argument("data", help="данные в hex (пробелы между байтами можно): 01FF00")
    s.add_argument("--ext", action="store_true", help="29-битный ID (иначе определяется по ID)")
    s.add_argument("--fd", action="store_true", help="кадр CAN FD")
    s.add_argument("--brs", action="store_true", help="FD с ускорением фазы данных (BRS)")
    s.add_argument("--period", type=int, help="периодическая отправка, период в мс (≥5); без него — один раз")
    s.add_argument("--count", type=int, help="сколько раз (только с --period; без него — без предела)")
    s.add_argument("--slot", type=int, help="номер слота 0..7 (иначе первый свободный)")
    s.set_defaults(func=cmd_tx)

    s = sub.add_parser("tx-list", parents=[common], help="слоты передачи и активные каналы")
    s.set_defaults(func=cmd_tx_list)

    s = sub.add_parser("tx-stop", parents=[common], help="остановить передачу: tx-stop [SLOT] (без номера — все слоты)")
    s.add_argument("slot", type=int, nargs="?", help="номер слота (без него — все)")
    s.set_defaults(func=cmd_tx_stop)

    s = sub.add_parser("session", parents=[common], help="session start ИМЯ | session stop | session")
    s.add_argument("action", nargs="?", choices=["start", "stop"])
    s.add_argument("name", nargs="?")
    s.set_defaults(func=cmd_session)

    for name, fn, hlp in (("say", cmd_say, "показать инструкцию пользователю"),
                          ("ask", cmd_ask, "показать инструкцию и ждать ответ на неё")):
        s = sub.add_parser(name, parents=[common], help=hlp)
        s.add_argument("text", nargs="+", help="текст инструкции (\\n — перенос строки)")
        s.add_argument("--buttons", help="«Начал=rpm2000,Закончил=-,Не вышло»: =имя — начать фазу, =- — закончить, без = — не трогать")
        s.add_argument("--input", choices=["number", "text"], help="поле ввода на странице")
        s.add_argument("--label", help="подпись поля ввода")
        if name == "ask":
            s.add_argument("--timeout", type=float, default=900.0, help="сколько ждать ответ, с (по умолчанию 900)")
        s.set_defaults(func=fn)

    s = sub.add_parser("wait", parents=[common], help="ждать событие от пользователя (длинный опрос)")
    s.add_argument("--since", type=int, help="seq, после которого ждать (по умолчанию — последний увиденный)")
    s.add_argument("--timeout", type=float, default=60.0, help="секунд (по умолчанию 60)")
    s.add_argument("--types", help="типы через запятую (по умолчанию %s; link — связь с контроллером)" % WAIT_TYPES)
    s.add_argument("--say", type=int, help="ждать ответ (кнопку/ввод) именно на инструкцию с этим id")
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser("events", parents=[common], help="события после seq")
    s.add_argument("--since", type=int, help="по умолчанию — последний увиденный seq")
    s.add_argument("--last", type=int, help="показать только последние N")
    s.set_defaults(func=cmd_events)

    s = sub.add_parser("phase", parents=[common], help="начать фазу (phase idle) или закончить (phase -)")
    s.add_argument("label", nargs="+")
    s.set_defaults(func=cmd_phase)

    s = sub.add_parser("mark", parents=[common], help="поставить отметку")
    s.add_argument("label", nargs="*")
    s.set_defaults(func=cmd_mark)

    s = sub.add_parser("note", parents=[common], help="сообщение в журнал страницы пользователя")
    s.add_argument("text", nargs="+")
    s.set_defaults(func=cmd_note)

    s = sub.add_parser("watch", parents=[common],
                       help="наблюдение: watch set|add \"Имя=SPEC@ед\" ... | watch clear | watch values | watch")
    s.add_argument("action", nargs="?", choices=["set", "add", "clear", "values", "list"])
    s.add_argument("fields", nargs="*")
    s.add_argument("--count", type=int, default=1, help="watch values: сколько раз прочитать")
    s.add_argument("--every", type=float, default=1.0, help="watch values: пауза между чтениями, с")
    s.set_defaults(func=cmd_watch)
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    args.json = getattr(args, "json", False)
    args.port = getattr(args, "port", None)
    try:
        state = load_state()
        port = resolve_port(args, state)
        rc = args.func(Monitor(port), args) or EXIT_OK
        if state.get("port") != port:                   # запоминаем только монитор, который ответил
            update_state(lambda s: s.update(port=port))
        return rc
    except CarError as e:
        if args.json:
            out(json.dumps({"ok": False, "error": str(e), "code": e.code}, ensure_ascii=False))
        else:
            sys.stderr.write("ОШИБКА: %s\n" % e)
            sys.stderr.flush()
        return e.code
    except KeyboardInterrupt:
        sys.stderr.write("прервано\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
