# -*- coding: utf-8 -*-
"""Разбор диагностического обмена (ISO-TP 15765-2 + UDS 14229) из записи (SPEC §12).

ТОЛЬКО ЧТЕНИЕ: здесь нет ни передачи, ни сетевого/железного доступа — это разбор уже
записанного текстового лога (как UDS-диссектор в Wireshark/SavvyCAN). Активная отправка,
если понадобится повторить кадр своим контроллером, — отдельный раздел SPEC §11.

Что умеет:
  * собрать ISO-TP сообщения из кадров одного (канал, ID): single / first+consecutive,
    flow-control отмечается; нормальная адресация; 11- и 29-битные ID; classic 8 байт и FD до 64;
    незавершённые цепочки помечаются, а не теряются молча;
  * разобрать UDS: сервисы и коды отказа (NRC) с названиями; запрос/ответ (ответ = сервис+0x40,
    негативный 0x7F sid nrc); DID для 0x22/0x62 и 0x2E/0x6E; RID и тип для 0x31; DID и режим для 0x2F;
  * вывести пары «запрос→ответ» и блоки на шине прямо из обмена (окно ~1 с); типовые ID (0x7E0/0x7E8,
    0x7DF, 29-бит 0x18DAxxyy/0x18DBxxyy) — только подсказка о роли, не жёсткое правило.
"""
from collections import deque

import numpy as np

from .fieldspec import fmt_id


# ---------------------------------------------------------------------------
# Таблицы имён UDS (ISO 14229). Названия — как в стандарте (подсказка в отчёте).
# ---------------------------------------------------------------------------
SERVICES = {
    0x10: "DiagnosticSessionControl", 0x11: "ECUReset", 0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation", 0x22: "ReadDataByIdentifier", 0x23: "ReadMemoryByAddress",
    0x24: "ReadScalingDataByIdentifier", 0x27: "SecurityAccess", 0x28: "CommunicationControl",
    0x29: "Authentication", 0x2A: "ReadDataByPeriodicIdentifier", 0x2C: "DynamicallyDefineDataIdentifier",
    0x2E: "WriteDataByIdentifier", 0x2F: "InputOutputControlByIdentifier", 0x31: "RoutineControl",
    0x34: "RequestDownload", 0x35: "RequestUpload", 0x36: "TransferData", 0x37: "RequestTransferExit",
    0x38: "RequestFileTransfer", 0x3D: "WriteMemoryByAddress", 0x3E: "TesterPresent",
    0x83: "AccessTimingParameter", 0x84: "SecuredDataTransmission", 0x85: "ControlDTCSetting",
    0x86: "ResponseOnEvent", 0x87: "LinkControl",
}

NRC = {
    0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat", 0x14: "responseTooLong", 0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect", 0x24: "requestSequenceError", 0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction", 0x31: "requestOutOfRange", 0x33: "securityAccessDenied",
    0x34: "authenticationRequired", 0x35: "invalidKey", 0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired", 0x38: "secureDataTransmissionRequired",
    0x39: "secureDataTransmissionNotAllowed", 0x3A: "secureDataVerificationFailed",
    0x50: "certificateVerificationFailed", 0x70: "uploadDownloadNotAccepted", 0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure", 0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceived-ResponsePending", 0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession", 0x81: "rpmTooHigh", 0x82: "rpmTooLow",
    0x83: "engineIsRunning", 0x84: "engineIsNotRunning", 0x85: "engineRunTimeTooLow",
    0x86: "temperatureTooHigh", 0x87: "temperatureTooLow", 0x88: "vehicleSpeedTooHigh",
    0x89: "vehicleSpeedTooLow", 0x8A: "throttle/PedalTooHigh", 0x8B: "throttle/PedalTooLow",
    0x8C: "transmissionRangeNotInNeutral", 0x8D: "transmissionRangeNotInGear",
    0x8F: "brakeSwitch(es)NotClosed", 0x90: "shifterLeverNotInPark", 0x91: "torqueConverterClutchLocked",
    0x92: "voltageTooHigh", 0x93: "voltageTooLow",
}

SESSIONS = {0x01: "defaultSession", 0x02: "programmingSession", 0x03: "extendedDiagnosticSession",
            0x04: "safetySystemDiagnosticSession"}
RESET_TYPES = {0x01: "hardReset", 0x02: "keyOffOnReset", 0x03: "softReset",
               0x04: "enableRapidPowerShutDown", 0x05: "disableRapidPowerShutDown"}
ROUTINE_SUB = {0x01: "startRoutine", 0x02: "stopRoutine", 0x03: "requestRoutineResults"}
IO_PARAM = {0x00: "returnControlToECU", 0x01: "resetToDefault", 0x02: "freezeCurrentState",
            0x03: "shortTermAdjustment"}
DTC_SETTING = {0x01: "on", 0x02: "off"}
COMM_CONTROL = {0x00: "enableRxAndTx", 0x01: "enableRxAndDisableTx", 0x02: "disableRxAndEnableTx",
                0x03: "disableRxAndTx"}
DTC_SUB = {0x01: "reportNumberOfDTCByStatusMask", 0x02: "reportDTCByStatusMask",
           0x04: "reportDTCSnapshotRecordByDTCNumber", 0x06: "reportDTCExtendedDataRecordByDTCNumber",
           0x0A: "reportSupportedDTC"}

# Стандартные DID (ISO 14229-1, диапазон 0xF180..0xF1FF) — подсказка о смысле.
DID_NAMES = {
    0xF180: "BootSoftwareIdentification", 0xF181: "applicationSoftwareIdentification",
    0xF182: "applicationDataIdentification", 0xF183: "bootSoftwareFingerprint",
    0xF184: "applicationSoftwareFingerprint", 0xF185: "applicationDataFingerprint",
    0xF186: "ActiveDiagnosticSession", 0xF187: "VehicleManufacturerSparePartNumber",
    0xF188: "VehicleManufacturerECUSoftwareNumber", 0xF189: "VehicleManufacturerECUSoftwareVersionNumber",
    0xF18A: "SystemSupplierIdentifier", 0xF18B: "ECUManufacturingDate", 0xF18C: "ECUSerialNumber",
    0xF18E: "VehicleManufacturerKitAssemblyPartNumber", 0xF190: "VIN",
    0xF191: "VehicleManufacturerECUHardwareNumber", 0xF192: "SystemSupplierECUHardwareNumber",
    0xF193: "SystemSupplierECUHardwareVersionNumber", 0xF194: "SystemSupplierECUSoftwareNumber",
    0xF195: "SystemSupplierECUSoftwareVersionNumber", 0xF197: "SystemNameOrEngineType",
    0xF198: "RepairShopCodeOrTesterSerialNumber", 0xF199: "ProgrammingDate",
    0xF19D: "ECUInstallationDate", 0xF1A0: "VehicleManufacturerSpecific",
    0xFF00: "UDSVersion",
}


def did_hint(did):
    """Название DID (если стандартный) или категория по диапазону — подсказка, не факт."""
    if did in DID_NAMES:
        return DID_NAMES[did]
    if 0x0000 <= did <= 0x00FF:
        return "ISO/SAE reserved"
    if 0xF180 <= did <= 0xF1FF:
        return "identification (0xF1xx)"
    if 0xF200 <= did <= 0xF2FF:
        return "periodicDataIdentifier"
    if 0xF300 <= did <= 0xF3FF:
        return "dynamicallyDefinedDataIdentifier"
    if 0xF400 <= did <= 0xF5FF or 0xF600 <= did <= 0xF7FF:
        return "OBD-related dataIdentifier"
    if 0xFD00 <= did <= 0xFEFF:
        return "systemSupplierSpecific"
    if 0xFF00 <= did <= 0xFFFF:
        return "ISO/SAE reserved"
    return "прикладной DID (специфичный для блока)"


def service_name(sid):
    return SERVICES.get(sid, "service_0x%02X" % sid)


# Все SID, которые можно встретить как «начало» UDS-сообщения (запрос, ответ или негативный).
KNOWN_SIDS = set(SERVICES) | {s + 0x40 for s in SERVICES} | {0x7F}


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------
def hexb(b):
    """Байты → «12 34 AB» (пусто → «(нет)»)."""
    if b is None or len(b) == 0:
        return "(нет)"
    return " ".join("%02X" % x for x in b)


def ascii_hint(b):
    """ASCII-подсказка для строковых DID (VIN, парт-номера): «WVWZZZ…» или ''."""
    if not b:
        return ""
    printable = sum(1 for x in b if 0x20 <= x <= 0x7E)
    if printable >= max(3, int(len(b) * 0.7)):
        return "".join(chr(x) if 0x20 <= x <= 0x7E else "." for x in b)
    return ""


# ---------------------------------------------------------------------------
# Адреса блоков (нормальная адресация; типовые схемы — только подсказка о роли)
# ---------------------------------------------------------------------------
def addr_guess(can_id, ext):
    """Догадка о роли ID по типовой схеме. Возвращает dict или None.
    ecu — адрес блока; tester_side — True, если это сторона тестера (запросы); functional — broadcast."""
    if ext:
        pf = (can_id >> 16) & 0xFFFF
        ta = (can_id >> 8) & 0xFF
        sa = can_id & 0xFF
        if pf in (0x18DA, 0x18DB):
            functional = (pf == 0x18DB)
            tester = lambda a: 0xF0 <= a <= 0xFF
            if tester(sa) and not tester(ta):
                # тестер(sa) → ЭБУ(ta): запрос
                return {"scheme": "29DA" if not functional else "29DB", "ecu": ta, "peer": sa,
                        "tester_side": True, "functional": functional}
            if tester(ta) and not tester(sa):
                # ЭБУ(sa) → тестер(ta): ответ
                return {"scheme": "29DA", "ecu": sa, "peer": ta,
                        "tester_side": False, "functional": False}
            # обе стороны «не тестер» — всё равно физическая пара, берём меньший как ЭБУ-адрес
            return {"scheme": "29DA", "ecu": min(ta, sa), "peer": max(ta, sa),
                    "tester_side": None, "functional": functional}
        return None
    # 11-битные типовые
    if can_id == 0x7DF:
        return {"scheme": "11func", "ecu": None, "peer": None, "tester_side": True, "functional": True}
    if 0x7E0 <= can_id <= 0x7E7:
        return {"scheme": "11obd", "ecu": can_id - 0x7E0, "peer": None,
                "tester_side": True, "functional": False}
    if 0x7E8 <= can_id <= 0x7EF:
        return {"scheme": "11obd", "ecu": can_id - 0x7E8, "peer": None,
                "tester_side": False, "functional": False}
    return None


def role_hint_text(block):
    """Человеческая подсказка о роли блока по его ID (SPEC §12.2 — только подсказка)."""
    scheme = block.get("scheme")
    ecu = block.get("ecu_addr")
    if scheme in ("29DA", "29DB"):
        base = "UDS по физическому адресу" if scheme == "29DA" else "UDS функциональный (broadcast)"
        return "%s, ЭБУ 0x%02X" % (base, ecu) if ecu is not None else base
    if scheme == "11func":
        return "функциональный OBD/UDS запрос (broadcast 0x7DF)"
    if scheme == "11obd":
        return "OBD/UDS ЭБУ #%d (0x7E%X→0x7E%X)" % (ecu, ecu, ecu + 8)
    return "тип не распознан по ID — роль определена по обмену"


# ---------------------------------------------------------------------------
# Сообщение ISO-TP
# ---------------------------------------------------------------------------
class Msg(object):
    """Одно транспортное сообщение (или flow-control) на потоке (канал, ID)."""
    __slots__ = ("ch", "can_id", "ext", "kind", "t0", "t1", "data", "length", "nframes",
                 "complete", "sn_ok", "note", "fc", "_uds")

    def __init__(self, ch, can_id, ext, kind, t0, t1, data, length, nframes,
                 complete=True, sn_ok=True, note=None, fc=None):
        self.ch = int(ch)
        self.can_id = int(can_id)
        self.ext = bool(ext)
        self.kind = kind                 # single / multi / flow / incomplete / stray
        self.t0 = float(t0)
        self.t1 = float(t1)
        self.data = bytes(data)          # склеенный payload (без PCI)
        self.length = length             # заявленная длина (может быть None у осколка)
        self.nframes = int(nframes)
        self.complete = bool(complete)
        self.sn_ok = bool(sn_ok)
        self.note = note
        self.fc = fc                     # {"flag","flag_name","bs","stmin"} для kind=="flow"
        self._uds = None

    @property
    def name(self):
        return "%d:%s" % (self.ch, fmt_id(self.can_id, self.ext))

    @property
    def key(self):
        return (self.ch, self.ext, self.can_id)

    def uds(self):
        if self._uds is None:
            self._uds = parse_uds(self.data) if self.kind in ("single", "multi", "incomplete") else {"role": self.kind}
        return self._uds


# ---------------------------------------------------------------------------
# Сборка ISO-TP из одного потока
# ---------------------------------------------------------------------------
def assemble_stream(s):
    """Кадры одного потока (Stream) → список Msg (включая flow-control и осколки).
    Нормальная адресация. FD-кадры (до 64 байт) поддержаны."""
    out = []
    n = s.n
    if n == 0 or s.L == 0:
        return out
    rows = s.data.tolist()
    ts = s.t.tolist()
    dls = s.dlen.tolist() if s.dlen is not None else [s.L] * n
    ch, cid, ext = s.ch, s.can_id, s.ext
    pending = None   # {"data":bytearray,"length":int|None,"next_sn":int,"t0":..,"t1":..,
                     #  "frames":int,"sn_ok":bool,"orphan":bool}
    prev_completed = False   # последним на этом потоке было завершённое сообщение (для хвостовых CF)

    def flush_incomplete():
        nonlocal prev_completed
        p = pending
        if p is None:
            return
        if p.get("orphan"):
            note = "цепочка без first-frame (запись началась посреди сообщения)"
        else:
            note = "неполная цепочка: получено %d из %d байт" % (len(p["data"]), p["length"])
        out.append(Msg(ch, cid, ext, "incomplete", p["t0"], p["t1"], bytes(p["data"]),
                       p["length"], p["frames"], complete=False, sn_ok=p["sn_ok"], note=note))
        prev_completed = False

    for i in range(n):
        row = rows[i]
        dl = int(dls[i]) if dls[i] else len(row)
        if dl < 1:
            continue
        t = float(ts[i])
        b0 = row[0] & 0xFF
        pci = b0 >> 4
        low = b0 & 0x0F
        if pci == 0x0:                                   # single frame
            if pending is not None:
                flush_incomplete()
                pending = None
            if low == 0:                                 # FD-escape: длина в байте 1
                length = row[1] & 0xFF if dl >= 2 else 0
                start = 2
            else:
                length = low
                start = 1
            # берём только байты этого кадра (row[start:dl]) и не больше заявленной длины —
            # чтобы усечённый SF не «дочитал» padding из более длинных кадров потока
            data = bytes(x & 0xFF for x in row[start:dl])[:length]
            complete = len(data) == length
            note = None if complete else "single frame короче заявленной длины"
            out.append(Msg(ch, cid, ext, "single", t, t, data, length, 1, complete=complete, note=note))
            prev_completed = complete
        elif pci == 0x1:                                 # first frame
            if pending is not None:
                flush_incomplete()
            length = ((b0 & 0x0F) << 8) | (row[1] & 0xFF)
            if length == 0:                              # escape: 32-битная длина в байтах 2..5
                length = ((row[2] & 0xFF) << 24) | ((row[3] & 0xFF) << 16) | \
                         ((row[4] & 0xFF) << 8) | (row[5] & 0xFF)
                start = 6
            else:
                start = 2
            first = bytes(x & 0xFF for x in row[start:dl])
            pending = {"data": bytearray(first[:length]), "length": length, "next_sn": 1,
                       "t0": t, "t1": t, "frames": 1, "sn_ok": True, "orphan": False}
            prev_completed = False                       # начата новая цепочка
            if len(pending["data"]) >= length:           # редкий случай — FF уже всё вместил
                out.append(Msg(ch, cid, ext, "multi", pending["t0"], t, bytes(pending["data"][:length]),
                               length, 1, complete=True, sn_ok=True))
                pending = None
                prev_completed = True
        elif pci == 0x2:                                 # consecutive frame
            if pending is None:                          # CF без first-frame
                if prev_completed:
                    # хвостовой (посторонний) CF сразу после завершённого сообщения — это не
                    # «осколок» цепочки, а дубль/мусор; помечаем отдельно и НЕ считаем незавершённым
                    out.append(Msg(ch, cid, ext, "stray", t, t, bytes(x & 0xFF for x in row[1:dl]),
                                   None, 1, complete=False,
                                   note="посторонний CF без first-frame (после завершённого сообщения)"))
                    continue
                # запись началась посреди сообщения — осколок
                pending = {"data": bytearray(), "length": None, "next_sn": (low + 1) & 0x0F,
                           "t0": t, "t1": t, "frames": 0, "sn_ok": True, "orphan": True}
            elif low != pending["next_sn"]:
                pending["sn_ok"] = False
                pending["next_sn"] = (low + 1) & 0x0F
            else:
                pending["next_sn"] = (low + 1) & 0x0F
            chunk = bytes(x & 0xFF for x in row[1:dl])
            if pending["length"] is not None:
                room = pending["length"] - len(pending["data"])
                pending["data"] += chunk[:room]
            else:
                pending["data"] += chunk
            pending["frames"] += 1
            pending["t1"] = t
            if pending["length"] is not None and len(pending["data"]) >= pending["length"]:
                out.append(Msg(ch, cid, ext, "multi", pending["t0"], t,
                               bytes(pending["data"][:pending["length"]]), pending["length"],
                               pending["frames"], complete=True, sn_ok=pending["sn_ok"]))
                pending = None
                prev_completed = True
        elif pci == 0x3:                                 # flow control (в разбор данных не идёт)
            flag = low
            fc = {"flag": flag, "flag_name": {0: "ContinueToSend", 1: "Wait", 2: "Overflow"}.get(flag, "?"),
                  "bs": row[1] & 0xFF if dl >= 2 else None, "stmin": row[2] & 0xFF if dl >= 3 else None}
            out.append(Msg(ch, cid, ext, "flow", t, t, b"", None, 1, complete=True, fc=fc))
        # прочие PCI (>3) при нормальной адресации невозможны — игнорируем
    if pending is not None:
        flush_incomplete()
    return out


# ---------------------------------------------------------------------------
# Разбор UDS одного сообщения
# ---------------------------------------------------------------------------
def _dids(payload):
    """Список DID из потока байт (по 2 байта)."""
    out = []
    i = 0
    while i + 2 <= len(payload):
        out.append((payload[i] << 8) | payload[i + 1])
        i += 2
    return out


def parse_uds(data):
    """Разбор payload UDS. Возвращает dict с role, service, именами и полями сервиса."""
    r = {"role": "empty", "sid": None, "service": None, "service_name": None, "summary": ""}
    if not data:
        r["summary"] = "(пусто)"
        return r
    sid = data[0]
    r["sid"] = sid
    if sid == 0x7F:                                       # негативный ответ
        req = data[1] if len(data) >= 2 else None
        nrc = data[2] if len(data) >= 3 else None
        r["role"] = "negative"
        r["service"] = req
        r["service_name"] = service_name(req) if req is not None else None
        r["nrc"] = nrc
        r["nrc_name"] = NRC.get(nrc, "NRC_0x%02X" % nrc) if nrc is not None else None
        r["summary"] = "НЕГАТИВНЫЙ на %s: %s (0x%02X)" % (
            r["service_name"] or "?", r["nrc_name"] or "?", nrc or 0)
        return r
    if sid in SERVICES:
        r["role"] = "request"
        r["service"] = sid
    elif (sid - 0x40) in SERVICES:
        r["role"] = "response"
        r["service"] = sid - 0x40
    else:
        r["role"] = "unknown"
        r["service"] = sid
        r["service_name"] = service_name(sid)
        r["summary"] = "неизвестный сервис 0x%02X (%s)" % (sid, hexb(data))
        return r
    svc = r["service"]
    r["service_name"] = service_name(svc)
    is_resp = (r["role"] == "response")
    body = data[1:]
    parts = ["%s %s" % (r["service_name"], "(ответ)" if is_resp else "(запрос)")]

    if svc == 0x10:                                       # DiagnosticSessionControl
        if body:
            st = body[0] & 0x7F
            r["session"] = st
            r["session_name"] = SESSIONS.get(st, "session_0x%02X" % st)
            parts.append(r["session_name"])
            if is_resp and len(body) >= 5:
                r["p2"] = (body[1] << 8) | body[2]
                r["p2_star"] = ((body[3] << 8) | body[4]) * 10
                parts.append("P2=%d мс P2*=%d мс" % (r["p2"], r["p2_star"]))
    elif svc == 0x11:                                     # ECUReset
        if body:
            rt = body[0] & 0x7F
            r["reset"] = rt
            r["reset_name"] = RESET_TYPES.get(rt, "reset_0x%02X" % rt)
            parts.append(r["reset_name"])
    elif svc == 0x27:                                     # SecurityAccess
        if body:
            lvl = body[0]
            r["sec_level"] = lvl
            kind = "requestSeed" if (lvl % 2 == 1) else "sendKey"
            parts.append("уровень 0x%02X (%s)" % (lvl, kind))
            if len(body) > 1:
                parts.append(("seed " if kind == "requestSeed" else "key ") + hexb(body[1:]))
    elif svc == 0x28:                                     # CommunicationControl
        if body:
            r["comm_control"] = body[0]
            parts.append(COMM_CONTROL.get(body[0], "0x%02X" % body[0]))
    elif svc == 0x3E:                                     # TesterPresent
        if body:
            parts.append("suppressPosResp" if body[0] & 0x80 else "sub 0x%02X" % body[0])
    elif svc == 0x85:                                     # ControlDTCSetting
        if body:
            r["dtc_setting"] = body[0] & 0x7F
            parts.append(DTC_SETTING.get(r["dtc_setting"], "0x%02X" % r["dtc_setting"]))
    elif svc == 0x14:                                     # ClearDiagnosticInformation
        if len(body) >= 3:
            r["dtc_group"] = (body[0] << 16) | (body[1] << 8) | body[2]
            parts.append("группа DTC 0x%06X" % r["dtc_group"])
    elif svc == 0x19:                                     # ReadDTCInformation
        if body:
            r["dtc_sub"] = body[0]
            parts.append(DTC_SUB.get(body[0], "sub 0x%02X" % body[0]))
            if len(body) > 1:
                parts.append(hexb(body[1:]))
    elif svc == 0x22:                                     # ReadDataByIdentifier
        if not is_resp:
            dids = _dids(body)
            r["dids"] = dids
            parts.append("DID " + ", ".join("0x%04X" % d for d in dids) if dids else "DID ?")
            if dids:
                r["did"] = dids[0]
        else:
            if len(body) >= 2:
                did = (body[0] << 8) | body[1]
                val = body[2:]
                r["did"] = did
                r["value"] = val
                a = ascii_hint(val)
                parts.append("DID 0x%04X = %s%s" % (did, hexb(val), (" «%s»" % a) if a else ""))
    elif svc == 0x2E:                                     # WriteDataByIdentifier
        if len(body) >= 2:
            did = (body[0] << 8) | body[1]
            r["did"] = did
            if not is_resp:
                val = body[2:]
                r["value"] = val
                a = ascii_hint(val)
                parts.append("DID 0x%04X ← %s%s" % (did, hexb(val), (" «%s»" % a) if a else ""))
            else:
                parts.append("DID 0x%04X записан" % did)
    elif svc == 0x2F:                                     # InputOutputControlByIdentifier
        if len(body) >= 2:
            did = (body[0] << 8) | body[1]
            r["did"] = did
            parts.append("DID 0x%04X" % did)
            if len(body) >= 3:
                p = body[2]
                r["io_param"] = p
                r["io_param_name"] = IO_PARAM.get(p, "param_0x%02X" % p)
                parts.append(r["io_param_name"])
                if len(body) > 3:
                    r["io_state"] = body[3:]
                    parts.append("состояние " + hexb(body[3:]))
    elif svc == 0x31:                                     # RoutineControl
        if body:
            sub = body[0]
            r["routine_sub"] = sub
            r["routine_sub_name"] = ROUTINE_SUB.get(sub, "sub_0x%02X" % sub)
            parts.append(r["routine_sub_name"])
            if len(body) >= 3:
                rid = (body[1] << 8) | body[2]
                r["rid"] = rid
                parts.append("RID 0x%04X" % rid)
                if len(body) > 3:
                    r["routine_data"] = body[3:]
                    parts.append(("статус " if is_resp else "параметры ") + hexb(body[3:]))
    elif svc in (0x34, 0x35):                             # RequestDownload / RequestUpload
        parts.append(hexb(body))
    elif svc == 0x36:                                     # TransferData
        if body:
            r["block_seq"] = body[0]
            parts.append("блок #%d" % body[0])
    else:
        if body:
            parts.append(hexb(body))
    r["summary"] = " ".join(parts)
    return r


# ---------------------------------------------------------------------------
# Отбор потоков, похожих на диагностику
# ---------------------------------------------------------------------------
def _typical_diag_id(can_id, ext):
    if ext:
        return (can_id >> 16) in (0x18DA, 0x18DB)
    return can_id == 0x7DF or 0x7E0 <= can_id <= 0x7EF or 0x700 <= can_id <= 0x7FF


def _has_known_sid(s, limit=400):
    """Есть ли среди первых кадров SF/FF с известным UDS-сервисом."""
    rows = s.data
    n = min(s.n, limit)
    for i in range(n):
        row = rows[i]
        b0 = int(row[0])
        hi = b0 >> 4
        low = b0 & 0x0F
        sid = None
        if hi == 0x0:
            j = 2 if low == 0 else 1
            if s.L > j:
                sid = int(row[j])
        elif hi == 0x1:
            length = ((b0 & 0x0F) << 8) | int(row[1]) if s.L > 1 else 0
            j = 6 if length == 0 else 2
            if s.L > j:
                sid = int(row[j])
        if sid is not None and (sid in KNOWN_SIDS):
            return True
    return False


def looks_diag(s):
    """Поток похож на диагностику (ISO-TP): по типовому ID или по содержимому кадров."""
    if s.n == 0 or s.L == 0:
        return False
    hi = s.data[:, 0] >> 4
    isotp_frac = float(np.count_nonzero(hi <= 3)) / s.n
    if _typical_diag_id(s.can_id, s.ext):
        return isotp_frac >= 0.3 or _has_known_sid(s)
    if isotp_frac < 0.6:
        return False
    return _has_known_sid(s)


def collect_messages(sess, chs=None):
    """Все ISO-TP сообщения записи (по потокам, похожим на диагностику), в порядке времени."""
    msgs = []
    for s in sess.streams.values():
        if chs is not None and s.ch not in chs:
            continue
        if not looks_diag(s):
            continue
        msgs.extend(assemble_stream(s))
    msgs.sort(key=lambda m: (m.t0, m.ch, m.can_id))
    return msgs


# ---------------------------------------------------------------------------
# Блоки: пары адресов из обмена + типовые подсказки
# ---------------------------------------------------------------------------
class _UF(object):
    """Система непересекающихся множеств для склейки потоков в блоки."""
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _pairs_from_exchange(msgs, window=1.0):
    """Рёбра запрос→ответ из самого обмена (окно window с): ответ = сервис недавнего запроса
    с другого ID (SPEC §12.2). Возвращает set рёбер (key_req, key_resp).

    Функциональные (broadcast) запросы (0x7DF, 29-бит 0x18DBxxyy) пар НЕ образуют: на один
    такой запрос отвечают несколько ЭБУ, и связывание их в один блок перебило бы верную
    группировку по физическому адресу. Разводить реальные пары физических ЭБУ умеет build_blocks
    (by_ecu), а из обмена пары нужны лишь для нетиповых (не broadcast) физических запросов.

    recent — deque недавних запросов в окне; устаревшие снимаются popleft() (амортизированно O(1))
    вместо пересборки списка на каждый ответ (иначе на диагностически-плотных логах O(N×плотность))."""
    edges = set()
    recent = deque()   # недавние запросы: (t, key, service), по возрастанию времени
    for m in msgs:
        if m.kind not in ("single", "multi"):
            continue
        u = m.uds()
        role = u.get("role")
        svc = u.get("service")
        # окно скольжения: msgs отсортированы по времени, front — самый старый запрос
        while recent and m.t0 - recent[0][0] > window:
            recent.popleft()
        if role == "request":
            g = addr_guess(m.can_id, m.ext)
            if g and (g.get("functional") or g.get("scheme") in ("11func", "29DB")):
                continue   # функциональный/broadcast запрос — пары не создаём
            recent.append((m.t0, m.key, svc))
        elif role in ("response", "negative"):
            for (tq, kq, sq) in reversed(recent):
                if kq == m.key:
                    continue
                if sq == svc and kq[0] == m.key[0]:      # тот же канал, тот же сервис
                    edges.add((kq, m.key))
                    break
    return edges


def build_blocks(msgs):
    """Сгруппировать потоки в блоки (ЭБУ). Склейка: по физическому адресу (29-бит) и по
    наблюдаемым парам запрос→ответ. Возвращает список блоков (dict)."""
    keys = {}
    for m in msgs:
        keys.setdefault(m.key, addr_guess(m.can_id, m.ext))
    uf = _UF()
    for k in keys:
        uf.find(k)
    # склейка по физическому адресу ЭБУ (запрос 18DA10F1 и ответ 18DAF110 — один блок)
    by_ecu = {}
    for k, g in keys.items():
        if g and g.get("scheme") in ("29DA", "11obd") and g.get("ecu") is not None:
            by_ecu.setdefault((k[0], g["scheme"], g["ecu"]), []).append(k)
    for grp in by_ecu.values():
        for other in grp[1:]:
            uf.union(grp[0], other)
    # склейка по наблюдаемым парам, но не сливаем два потока с РАЗНЫМИ известными адресами ЭБУ
    # (страховка: разные физические ЭБУ должны остаться разными блоками)
    for (kq, kr) in _pairs_from_exchange(msgs):
        gq, gr = keys.get(kq), keys.get(kr)
        eq = gq.get("ecu") if gq else None
        er = gr.get("ecu") if gr else None
        if eq is not None and er is not None and eq != er:
            continue
        uf.union(kq, kr)

    groups = {}
    for m in msgs:
        groups.setdefault(uf.find(m.key), []).append(m)

    blocks = []
    for root, gmsgs in groups.items():
        kset = sorted({m.key for m in gmsgs})
        guesses = [keys[k] for k in kset]
        ecu_addr = None
        scheme = None
        for g in guesses:
            if g:
                scheme = g.get("scheme")
                if g.get("ecu") is not None:
                    ecu_addr = g["ecu"]
                    break
        req_ids, resp_ids = [], []
        for k in kset:
            g = keys[k]
            side = g.get("tester_side") if g else None
            # уточняем сторону по фактическим ролям сообщений
            roles = [mm.uds().get("role") for mm in gmsgs if mm.key == k and mm.kind in ("single", "multi")]
            nreq = roles.count("request")
            nresp = roles.count("response") + roles.count("negative")
            if side is True or (side is None and nreq >= nresp and nreq > 0):
                req_ids.append(k)
            elif side is False or (side is None and nresp > nreq):
                resp_ids.append(k)
            else:
                (req_ids if nreq >= nresp else resp_ids).append(k)
        blocks.append({"root": root, "keys": kset, "scheme": scheme, "ecu_addr": ecu_addr,
                       "req_ids": req_ids, "resp_ids": resp_ids, "messages": gmsgs})
    blocks.sort(key=lambda b: (b["ecu_addr"] if b["ecu_addr"] is not None else 0x10000,
                               b["keys"][0]))
    return blocks


def block_label(b):
    if b["ecu_addr"] is not None:
        return "ЭБУ 0x%02X" % b["ecu_addr"]
    names = [name_of(k) for k in b["keys"]]
    return "блок " + "/".join(names)


def name_of(key):
    ch, ext, cid = key
    return "%d:%s" % (ch, fmt_id(cid, ext))


def block_matches_ecu(b, addr):
    """--ecu ADDR: совпадение по адресу ЭБУ, по любому ID блока или по младшему байту ID."""
    if addr is None:
        return True
    if b["ecu_addr"] is not None and addr == b["ecu_addr"]:
        return True
    for (ch, ext, cid) in b["keys"]:
        if addr == cid or addr == (cid & 0xFF) or addr == ((cid >> 8) & 0xFF):
            return True
    return False


# ---------------------------------------------------------------------------
# Сводка по блоку: сервисы, сессии, DID, routine, записи, негативные
# ---------------------------------------------------------------------------
def summarize_block(b):
    """Собрать по блоку: счётчики сервисов, сессии, прочитанные/записанные DID, RID, негативные."""
    services = {}
    sessions = {}
    reads = {}      # did -> {"bytes","count","last_t","ascii"}
    writes = {}     # did -> {"bytes","count","last_t"}
    io = {}         # did -> {"param","state","count"}
    routines = {}   # rid -> {"subs":set,"count","last_data"}
    negatives = []
    requested = {}  # did -> count (0x22, чтобы знать что тестер читал даже без ответа)
    for m in b["messages"]:
        if m.kind not in ("single", "multi", "incomplete"):
            continue
        u = m.uds()
        role = u.get("role")
        svc = u.get("service")
        if svc is not None and role in ("request", "response", "negative", "unknown"):
            nm = "0x%02X %s" % (svc, service_name(svc))
            services[nm] = services.get(nm, 0) + 1
        if role == "negative":
            negatives.append({"t": m.t0, "service": svc, "service_name": u.get("service_name"),
                              "nrc": u.get("nrc"), "nrc_name": u.get("nrc_name"), "id": m.name})
            continue
        if svc == 0x10 and u.get("session") is not None:
            snm = u.get("session_name")
            sessions[snm] = sessions.get(snm, 0) + 1
        elif svc == 0x22:
            if role == "request":
                for d in u.get("dids", []):
                    requested[d] = requested.get(d, 0) + 1
            elif role == "response" and u.get("did") is not None:
                d = u["did"]
                val = u.get("value") or b""
                e = reads.setdefault(d, {"bytes": bytes(val), "count": 0, "last_t": m.t0})
                e["bytes"], e["count"], e["last_t"] = bytes(val), e["count"] + 1, m.t0
        elif svc == 0x2E and u.get("did") is not None:
            d = u["did"]
            if role == "request":
                val = u.get("value") or b""
                e = writes.setdefault(d, {"bytes": bytes(val), "count": 0, "last_t": m.t0})
                e["bytes"], e["count"], e["last_t"] = bytes(val), e["count"] + 1, m.t0
            else:
                writes.setdefault(d, {"bytes": b"", "count": 0, "last_t": m.t0})["count"] += 1
        elif svc == 0x2F and u.get("did") is not None:
            d = u["did"]
            e = io.setdefault(d, {"param": None, "state": b"", "count": 0})
            e["count"] += 1
            if u.get("io_param_name"):
                e["param"] = u["io_param_name"]
            # состояние — то, что тестер КОМАНДОВАЛ (в запросе), а не эхо из ответа
            if role == "request" and u.get("io_state"):
                e["state"] = bytes(u["io_state"])
        elif svc == 0x31 and u.get("rid") is not None:
            rid = u["rid"]
            e = routines.setdefault(rid, {"subs": set(), "count": 0, "last_data": b""})
            e["count"] += 1
            if u.get("routine_sub_name"):
                e["subs"].add(u["routine_sub_name"])
            if u.get("routine_data"):
                e["last_data"] = bytes(u["routine_data"])
    return {"services": services, "sessions": sessions, "reads": reads, "writes": writes,
            "io": io, "routines": routines, "negatives": negatives, "requested": requested}


def did_dump(b):
    """Дамп конфигурации блока: did -> {"bytes","source","name","count"}.
    Приоритет — прочитанное значение (0x62); иначе записанное (0x2E); иначе только факт запроса (0x22)."""
    s = summarize_block(b)
    dump = {}
    for d, e in s["reads"].items():
        dump[d] = {"bytes": e["bytes"], "source": "read", "name": did_hint(d), "count": e["count"]}
    for d, e in s["writes"].items():
        if d not in dump or not dump[d]["bytes"]:
            dump[d] = {"bytes": e["bytes"], "source": "write", "name": did_hint(d), "count": e["count"]}
    for d, c in s["requested"].items():
        if d not in dump:
            dump[d] = {"bytes": b"", "source": "requested", "name": did_hint(d), "count": c}
    return dump
