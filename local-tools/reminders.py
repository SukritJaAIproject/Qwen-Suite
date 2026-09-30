"""Reminders and scheduled messages from chat, delivered with no model call (added 2026-09-30).

schedule_reminder() keeps the text in <install root>\\data\\reminders\\<id>.json and creates an
OpenClaw automation named "qwen-reminder <id>" whose payload is a fixed argv built here:
[webui-venv python.exe, reminder_echo.py, <id>]. When it fires, the Gateway runs that command
and announces its stdout to the owner's Telegram, so no model is loaded and the reminder text
never appears on a command line. The model only ever supplies `when`, `text` and `repeat`.

The agent's own automations tool stays off on purpose: its schema costs ~4.5 KB per prompt and
it can create arbitrary command/trigger jobs outside the Permission Center.

The time parser is deterministic (no model), Thai and English: 20m, 2h, 08:00, พรุ่งนี้ 08:00,
บ่าย 3 โมง, 2 ทุ่ม, 1 ต.ค. 2569 09:00, ทุกวัน 07:30, ทุกวันจันทร์ 08:00 ... Anything it cannot
read exactly is rejected with a Thai reason rather than guessed, and the reply always carries
the resolved date so a wrong reading is visible. Times are Asia/Bangkok: UTC+7 with no daylight
saving, so a fixed offset is exact and needs no tz database.

Limits: recurring no more often than hourly (a cron minute field must be one number), one-shots
within 365 days, at most 50 reminders in the scheduler.

OpenClaw is started as node.exe + openclaw.mjs - exactly what openclaw.cmd runs - so the JSON
argv never passes through cmd.exe quoting.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

TZ_NAME = "Asia/Bangkok"
TZ = timezone(timedelta(hours=7), TZ_NAME)
INSTALL_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("QWEN_REMINDERS_ROOT") or INSTALL_ROOT / "data" / "reminders")
OPENCLAW_ROOT = INSTALL_ROOT / "openclaw-runtime"
OPENCLAW_NODE = OPENCLAW_ROOT / "node.exe"
OPENCLAW_MJS = OPENCLAW_ROOT / "node_modules" / "openclaw" / "openclaw.mjs"
OPENCLAW_CMD = OPENCLAW_ROOT / "openclaw.cmd"
OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
PYTHON = INSTALL_ROOT / "webui-venv" / "Scripts" / "python.exe"
ECHO_SCRIPT = Path(__file__).resolve().with_name("reminder_echo.py")

NAME_PREFIX = "qwen-reminder"
MAX_ACTIVE = 50
MAX_AHEAD = timedelta(days=365)
MIN_DURATION = timedelta(minutes=1)
MAX_TEXT_CHARS = 1000
CLI_TIMEOUT = 60
COMMAND_TIMEOUT_SECONDS = 30
KEEP_FINISHED_DAYS = 30

ID_RE = re.compile(r"[0-9a-f]{12}")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_LOCK = threading.Lock()
_subprocess_run: Callable[..., Any] = subprocess.run  # tests replace this; they must never create a job

EXAMPLES_TH = "เช่น 20m, 2h, อีก 20 นาที, 08:00, พรุ่งนี้ 08:00, บ่าย 3 โมง, 1 ต.ค. 09:00, ทุกวัน 07:30, ทุกวันจันทร์ 08:00"


def _now() -> datetime:
    return datetime.now(TZ)


# ------------------------------------------------------------------------------ vocabulary
_TH_WEEKDAY_NAMES = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]  # date.weekday() order
_TH_MONTH_ABBR = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]

# cron day-of-week numbers: Sunday = 0
_WD_TH = r"(?:จันทร์|อังคาร|พุธ|พฤหัสบดี|พฤหัส|ศุกร์|เสาร์|อาทิตย์)"
_WD_TH_MAP = {"จันทร์": 1, "อังคาร": 2, "พุธ": 3, "พฤหัสบดี": 4, "พฤหัส": 4, "ศุกร์": 5, "เสาร์": 6, "อาทิตย์": 0}
_WD_EN = (r"(?:monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thu|"
          r"friday|fri|saturday|sat|sunday|sun)(?![a-z])")
_WD_EN_MAP = {
    "monday": 1, "mon": 1, "tuesday": 2, "tues": 2, "tue": 2, "wednesday": 3, "wed": 3,
    "thursday": 4, "thurs": 4, "thu": 4, "friday": 5, "fri": 5, "saturday": 6, "sat": 6,
    "sunday": 0, "sun": 0,
}
_TH_MONTH_WORDS = {
    1: ["มกราคม", "มกรา", "มค"], 2: ["กุมภาพันธ์", "กุมภา", "กพ"], 3: ["มีนาคม", "มีนา", "มีค"],
    4: ["เมษายน", "เมษา", "เมย"], 5: ["พฤษภาคม", "พฤษภา", "พค"], 6: ["มิถุนายน", "มิถุนา", "มิย"],
    7: ["กรกฎาคม", "กรกฏาคม", "กรกฎา", "กรกฏา", "กค"], 8: ["สิงหาคม", "สิงหา", "สค"],
    9: ["กันยายน", "กันยา", "กย"], 10: ["ตุลาคม", "ตุลา", "ตค"], 11: ["พฤศจิกายน", "พฤศจิกา", "พย"],
    12: ["ธันวาคม", "ธันวา", "ธค"],
}
_TH_MONTH_LOOKUP = {word: month for month, words in _TH_MONTH_WORDS.items() for word in words}
# Abbreviations may keep their dots (ต.ค., มี.ค.); longest first so a prefix never wins.
_TH_MONTH = "(?:" + "|".join(
    sorted((w if len(w) > 3 else r"\.?".join(w) + r"\.?" for w in _TH_MONTH_LOOKUP), key=len, reverse=True)
) + ")"
_EN_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
             r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?(?![a-z])")
_EN_MONTH_MAP = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
                 "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_AMPM = r"(a\.?\s?m\.?|p\.?\s?m\.?)(?![a-z])"

_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
_THAI_UNITS = {"หนึ่ง": 1, "เอ็ด": 1, "สอง": 2, "ยี่": 2, "สาม": 3, "สี่": 4, "ห้า": 5, "หก": 6,
               "เจ็ด": 7, "แปด": 8, "เก้า": 9}
_THAI_NUMBER = re.compile(
    r"(?:(ยี่|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า)?สิบ(เอ็ด|หนึ่ง|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า)?"
    r"|(หนึ่ง|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า))"
)
_NO_REPEAT = {"", "none", "no", "once", "one-time", "onetime", "never", "ครั้งเดียว", "ไม่ซ้ำ", "ไม่ต้องซ้ำ", "-"}
_CRON_SHAPE = re.compile(r"[\d*/,\-]+(?:\s+[\d*/,\-]+){4,5}")

# What may remain once every recognised part is cut out (politeness, "at", "เวลา" ...).
_FILLER_TH = re.compile(
    r"เตือน(?:ความจำ)?|ฉัน|ผม|หนู|ด้วย|หน่อย|นะ|ครับ|ค่ะ|คะ|จ้า|ตอน|เวลา|ช่วง|ประมาณ|ราว|ภายใน|ใน|อีก|"
    r"ตั้งแต่|เริ่ม|ที่|นี้|ของ|และ|กับ|วัน|เช้า|สาย|บ่าย|เย็น|ค่ำ|คืน|นาฬิกา|น\.?"
)
_FILLER_EN = re.compile(
    r"(?<![a-z])(?:remind|me|at|on|the|of|by|around|about|in|from|starting|start|and|morning|"
    r"afternoon|evening|tonight|night|o'?clock)(?![a-z])"
)
_PM_HINT = re.compile(r"บ่าย|เย็น|ค่ำ|(?<![a-z])(?:afternoon|evening)(?![a-z])")
_NIGHT_HINT = re.compile(r"คืน|(?<![a-z])(?:tonight|night)(?![a-z])")


@dataclass
class Schedule:
    kind: str                 # "once" | "cron"
    at: datetime | None       # the single run of a one-shot (Asia/Bangkok)
    cron: str | None          # 5-field expression evaluated in Asia/Bangkok
    summary_th: str
    next_run: datetime
    note_th: str = ""


class _Text:
    """The normalised input; each recognised piece is cut out so leftovers can be checked."""

    def __init__(self, value: str) -> None:
        self.s = value

    def take(self, pattern: str) -> re.Match[str] | None:
        match = re.search(pattern, self.s)
        if match:
            self.cut(match.start(), match.end())
        return match

    def cut(self, start: int, end: int) -> None:
        self.s = self.s[:start] + " " + self.s[end:]


def _normalize(value: str) -> str:
    s = unicodedata.normalize("NFC", value or "")
    s = re.sub(r"[\u200b\u200c\u200d\ufeff]", " ", s).translate(_THAI_DIGITS).lower()
    s = s.replace("ๆ", " ").replace("：", ":")
    # Era markers only precede a year: "1 ต.ค. พ.ศ. 2569" -> "1 ต.ค. 2569".
    s = re.sub(r"พ\.\s?ศ\.?|ค\.\s?ศ\.?|พศ\.?|คศ\.?", " ", s)
    s = _THAI_NUMBER.sub(_thai_number, s)
    s = re.sub(r"วัน\s+(?=" + _WD_TH + ")", "วัน", s)
    # 08.30 / 8.30 น. is how Thai writes clock time; d.m.yyyy dates keep their dots.
    s = re.sub(r"(?<![\d.])(\d{1,2})\.(\d{2})(?![\d.])", r"\1:\2", s)
    return re.sub(r"\s+", " ", s).strip()


def _thai_number(match: re.Match[str]) -> str:
    if match.group(3):
        value = _THAI_UNITS[match.group(3)]
    else:
        value = _THAI_UNITS.get(match.group(1) or "", 1) * 10 + _THAI_UNITS.get(match.group(2) or "", 0)
    return f" {value} "


# ------------------------------------------------------------------------------ recurrence
def _dows_from(text: str, pattern: str, mapping: dict[str, int]) -> list[int]:
    return list(dict.fromkeys(mapping[name] for name in re.findall(pattern, text)))


def _dow_range(first: int, last: int) -> list[int]:
    days = [first]
    while days[-1] != last:
        days.append((days[-1] + 1) % 7)
    return days


def _take_recurrence(t: _Text) -> dict[str, Any] | None:
    if t.take(r"ทุก\s*(?:\d+\s*)?(?:นาที|วินาที)|ทุก\s*ครึ่ง\s*(?:ชั่วโมง|ชม)"
              r"|every\s*(?:\d+\s*)?(?:minutes?|mins?|m|seconds?|secs?|s)(?![a-z])"
              r"|every\s+half\s+(?:an\s+)?hour"):
        raise ValueError("เตือนซ้ำได้ถี่สุดชั่วโมงละครั้ง")
    m = t.take(r"ทุก\s*(\d+)\s*(?:ชั่วโมง|ชม\.?)|every\s*(\d+)\s*(?:hours?|hrs?|h)(?![a-z])")
    if m:
        n = int(m.group(1) or m.group(2))
        if n == 24:
            return {"type": "daily"}
        if n < 1 or 24 % n:
            raise ValueError("ทุก N ชั่วโมง ใช้ได้เฉพาะ 1, 2, 3, 4, 6, 8, 12 ชั่วโมง")
        return {"type": "hourly", "every": n}
    if t.take(r"ทุก\s*(?:ชั่วโมง|ชม\.?)|(?<![a-z])(?:hourly|every\s+hour)(?![a-z])"):
        return {"type": "hourly", "every": 1}
    m = t.take(r"ทุก\s*(\d+)\s*(วัน(?!ที่|นี้)|สัปดาห์|อาทิตย์|เดือน|ปี)"
               r"|every\s*(\d+)\s*(days?|weeks?|months?|years?)(?![a-z])")
    if m:
        n = int(m.group(1) or m.group(3))
        unit = m.group(2) or m.group(4)
        kind = {"วัน": "daily", "สัปดาห์": "weekly", "อาทิตย์": "weekly", "เดือน": "monthly", "ปี": "yearly",
                "day": "daily", "week": "weekly", "month": "monthly", "year": "yearly"}[unit.rstrip("s")]
        if n != 1:
            raise ValueError(f"'ทุก {n} {unit}' ยังไม่รองรับ — ใช้ ทุกวัน / ทุกสัปดาห์ / ทุกเดือน หรือระบุวันในสัปดาห์")
        return {"type": kind}
    m = t.take(r"(?:ทุก\s*)?(?:วัน)?(" + _WD_TH + r")\s*(?:-|–|ถึง)\s*(?:วัน)?(" + _WD_TH + r")")
    if m:
        return {"type": "dow", "dows": _dow_range(_WD_TH_MAP[m.group(1)], _WD_TH_MAP[m.group(2)])}
    m = t.take(r"(?<![a-z])(?:every\s+)?(" + _WD_EN + r")\s*(?:-|–|to|through|thru)\s*(" + _WD_EN + r")")
    if m:
        return {"type": "dow", "dows": _dow_range(_WD_EN_MAP[m.group(1)], _WD_EN_MAP[m.group(2)])}
    if t.take(r"(?:ทุก\s*)?วัน\s*(?:ทำงาน|ธรรมดา)|(?<![a-z])(?:every\s+)?weekdays?(?![a-z])"):
        return {"type": "dow", "dows": [1, 2, 3, 4, 5]}
    if t.take(r"ทุก\s*(?:วันหยุด\s*)?สุดสัปดาห์|(?<![a-z])(?:every\s+)?weekends?(?![a-z])"):
        return {"type": "dow", "dows": [6, 0]}
    m = t.take(r"ทุก\s*(วัน)?(" + _WD_TH + r")((?:\s*(?:,|และ|กับ|/|หรือ)?\s*(?:วัน)?" + _WD_TH + r")*)")
    if m:
        if m.group(2) == "อาทิตย์" and not m.group(1) and not m.group(3).strip():
            # "ทุกอาทิตย์" is said for both "every week" and "every Sunday".
            raise ValueError("'ทุกอาทิตย์' กำกวม — ใช้ 'ทุกวันอาทิตย์' หรือ 'ทุกสัปดาห์'")
        return {"type": "dow", "dows": _dows_from(m.group(0), _WD_TH, _WD_TH_MAP)}
    # "every mon, wed and fri" / "mondays and fridays": the whole list is one recurrence.
    sep = r"\s*(?:,\s*(?:and|&|or)?|and|&|/|or)\s*"
    plural = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    m = t.take(r"every\s+(" + _WD_EN + r")((?:" + sep + _WD_EN + r")*)"
               r"|(?<![a-z])" + plural + r"s(?![a-z])(?:" + sep + plural + r"s?(?![a-z]))*")
    if m:
        return {"type": "dow", "dows": _dows_from(m.group(0).replace("days", "day"), _WD_EN, _WD_EN_MAP)}
    m = t.take(r"ทุก\s*(?:ปี\s*)?(?:วันที่\s*)?(?<!\d)(\d{1,2})\s*(" + _TH_MONTH + r")")
    if m:
        return {"type": "yearly", "month": _TH_MONTH_LOOKUP[m.group(2).replace(".", "")], "day": int(m.group(1))}
    m = t.take(r"ทุก\s*(?:เดือน\s*)?(?:วัน\s*ที่|ที่)\s*(\d{1,2})(?![\d:/])"
               r"|(?:every\s+month|monthly)\s+(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?(?![\d:])")
    if m:
        return {"type": "dom", "day": int(m.group(1) or m.group(2))}
    m = t.take(r"ทุก\s*(?:วัน|เช้า|เย็น|คืน|ค่ำ)(?!ที่)"
               r"|(?<![a-z])(?:every\s*day|everyday|daily|each\s+day|every\s+(?:morning|evening|night))(?![a-z])")
    if m:
        word = m.group(0)
        hint = "pm" if re.search(r"เย็น|ค่ำ|evening", word) else "night" if re.search(r"คืน|night", word) else ""
        return {"type": "daily", "hint": hint}
    if t.take(r"ทุก\s*สัปดาห์|(?<![a-z])(?:every\s+week|weekly)(?![a-z])"):
        return {"type": "weekly"}
    if t.take(r"ทุก\s*เดือน|(?<![a-z])(?:every\s+month|monthly)(?![a-z])"):
        return {"type": "monthly"}
    if t.take(r"ทุก\s*ปี|(?<![a-z])(?:every\s+year|yearly|annually)(?![a-z])"):
        return {"type": "yearly"}
    return None


def _same_recurrence(rec: dict[str, Any], again: dict[str, Any]) -> dict[str, Any] | None:
    """Two recurrence phrases that say the same thing ("ทุกวันจันทร์" + weekly) -> the specific one; else None."""
    a, b = ({k: v for k, v in r.items() if k != "hint"} for r in (rec, again))
    merged = None
    if a == b:
        merged = rec
    for generic, specific, keep in ((a, b, again), (b, a, rec)):
        if ((generic == {"type": "weekly"} and specific["type"] == "dow" and len(set(specific["dows"])) == 1)
                or (generic == {"type": "monthly"} and specific["type"] == "dom")
                or (generic == {"type": "yearly"} and specific["type"] == "yearly" and "month" in specific)):
            merged = keep
    if merged is None:
        return None
    # keep an evening/night word from either phrase ("daily" + "ทุกเย็น 7:00" is 19:00)
    return {**merged, "hint": rec.get("hint") or again.get("hint") or ""}


# ------------------------------------------------------------------------------ dates
def _year_from(raw: str | None, now: datetime) -> int | None:
    """4-digit years >= 2400 are Buddhist era; a 2-digit year is whichever era lands near now."""
    if not raw:
        return None
    value = int(raw)
    if len(raw) == 4:
        return value - 543 if value >= 2400 else value
    for candidate in (2000 + value, 2500 + value - 543):
        if now.year <= candidate <= now.year + 1:
            return candidate
    return None


def _take_date(t: _Text, now: datetime) -> dict[str, Any] | None:
    if re.search(r"(?:สัปดาห์|เดือน|ปี)\s*หน้า|(?<!วัน)อาทิตย์\s*หน้า|(?<![a-z])next\s+(?:week|month|year)(?![a-z])", t.s):
        raise ValueError("'สัปดาห์หน้า / เดือนหน้า' กำกวม — ระบุวัน เช่น 5 ต.ค. 09:00 หรือ วันจันทร์ 09:00")
    m = t.take(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?:(?:t|\s+)(\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?"
               r"\s*(z|[+-]\d{2}:?\d{2})?)?(?![\d-])")
    if m:
        spec: dict[str, Any] = {"kind": "date", "year": _year_from(m.group(1), now), "month": int(m.group(2)),
                                "day": int(m.group(3)), "year_explicit": True}
        if m.group(4):
            spec["time"] = (int(m.group(4)), int(m.group(5)))
            offset = m.group(6)
            if offset:
                digits = offset[1:].replace(":", "")
                spec["offset"] = timedelta(0) if offset == "z" else (-1 if offset[0] == "-" else 1) * timedelta(
                    hours=int(digits[:2]), minutes=int(digits[2:]))
        return spec
    m = re.search(r"(?:วัน\s*ที่\s*)?(?<!\d)(\d{1,2})\s*(" + _TH_MONTH + r")", t.s)
    if m:
        end, year = m.end(), None
        y = re.match(r"\s*(\d{4}|\d{2})(?![\d:])", t.s[end:])
        if y and _year_from(y.group(1), now):
            year, end = _year_from(y.group(1), now), end + y.end()
        t.cut(m.start(), end)
        return {"kind": "date", "year": year, "month": _TH_MONTH_LOOKUP[m.group(2).replace(".", "")],
                "day": int(m.group(1)), "year_explicit": year is not None}
    m = t.take(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s*)?(" + _EN_MONTH + r")(?:,?\s*(\d{4})(?![\d:]))?"
               r"|(?<![a-z])(" + _EN_MONTH + r")\s*(\d{1,2})(?:st|nd|rd|th)?(?![\d:])(?:,?\s*(\d{4})(?![\d:]))?")
    if m:
        year = _year_from(m.group(3) or m.group(6), now)
        return {"kind": "date", "year": year, "month": _EN_MONTH_MAP[(m.group(2) or m.group(4))[:3]],
                "day": int(m.group(1) or m.group(5)), "year_explicit": year is not None}
    m = t.take(r"(?<![\d/.:])(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![\d/:])"
               r"|(?<![\d/.:])(\d{1,2})[.-](\d{1,2})[.-](\d{4}|\d{2})(?![\d.:-])")
    if m:
        raw_year = m.group(3) or m.group(6)
        year = _year_from(raw_year, now)
        if raw_year and year is None:
            raise ValueError(f"ปี {raw_year} อยู่นอกช่วงที่ตั้งเตือนได้ (ไม่เกิน 365 วันข้างหน้า)")
        return {"kind": "date", "year": year, "month": int(m.group(2) or m.group(5)),
                "day": int(m.group(1) or m.group(4)), "year_explicit": year is not None}
    if t.take(r"มะรืน(?:\s*นี้)?|(?<![a-z])day\s+after\s+tomorrow(?![a-z])"):
        return {"kind": "rel", "days": 2}
    if t.take(r"พรุ่งนี้|(?<![a-z])(?:tomorrow|tmrw|tmr)(?![a-z])"):
        return {"kind": "rel", "days": 1}
    m = t.take(r"(?:คืน|เย็น|บ่าย|ค่ำ|เช้า)\s*นี้|(?<![a-z])(?:tonight|this\s+(?:evening|afternoon|morning))(?![a-z])")
    if m:
        word = m.group(0)
        hint = "night" if re.search(r"คืน|tonight", word) else "" if re.search(r"เช้า|morning", word) else "pm"
        return {"kind": "rel", "days": 0, "hint": hint}
    if t.take(r"วันนี้|(?<![a-z])today(?![a-z])"):
        return {"kind": "rel", "days": 0}
    m = t.take(r"(?:วัน)?(" + _WD_TH + r")(\s*หน้า|\s*นี้)?")
    if m:
        return {"kind": "weekday", "dow": _WD_TH_MAP[m.group(1)], "next": "หน้า" in (m.group(2) or "")}
    m = t.take(r"(?<![a-z])(next\s+|this\s+|on\s+)?(" + _WD_EN + r")")
    if m:
        return {"kind": "weekday", "dow": _WD_EN_MAP[m.group(2)], "next": (m.group(1) or "").strip() == "next"}
    m = t.take(r"วัน\s*ที่\s*(\d{1,2})(?![\d:/])|(?<![a-z])(?:on\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)(?![a-z])")
    if m:
        return {"kind": "dom", "day": int(m.group(1) or m.group(2))}
    return None


# ------------------------------------------------------------------------------ time of day
_NUM = r"(\d{1,2})"
_TAIL = r"(\s*ครึ่ง)?(?:\s*(\d{1,2})\s*นาที)?"   # "8 โมงครึ่ง", "8 โมง 20 นาที"


def _minutes(half: str | None, minutes: str | None) -> int:
    return int(minutes) if minutes else 30 if half else 0


def _checked(n: int, minute: int, low: int, high: int, word: str, convert: Callable[[int], int]) -> dict[str, Any]:
    if not low <= n <= high:
        raise ValueError(f"'{word} {n}' ไม่ใช่เวลาที่ถูกต้อง")
    if not 0 <= minute <= 59:
        raise ValueError(f"นาที {minute} ไม่ถูกต้อง")
    return {"h": convert(n), "m": minute, "clock": False}


def _with_ampm(hour: int, minute: int, ampm: str | None) -> dict[str, Any]:
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError(f"'{hour} {ampm}' ไม่ใช่เวลาที่ถูกต้อง")
        hour = hour % 12 + (12 if ampm.startswith("p") else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"เวลา {hour}:{minute:02d} ไม่ถูกต้อง (ชั่วโมง 0-23 นาที 0-59)")
    return {"h": hour, "m": minute, "clock": not ampm}


def _take_time(t: _Text, pm_hint: bool = False) -> dict[str, Any] | None:
    """Return {"h", "m", "clock"}; clock=True is a bare HH:MM that a period word may still shift."""
    m = t.take(r"(?<![\d:])(\d{1,2}):(\d{2})(?::\d{2})?(?![\d:])(?:\s*" + _AMPM + r")?")
    if m:
        return _with_ampm(int(m.group(1)), int(m.group(2)), m.group(3))
    m = t.take(r"(?<![\d:.])(\d{1,2})\s*" + _AMPM)
    if m:
        return _with_ampm(int(m.group(1)), 0, m.group(2))
    if t.take(r"เที่ยง\s*คืน|(?<![a-z])midnight(?![a-z])"):
        return {"h": 0, "m": 0, "clock": False}
    if t.take(r"เที่ยง(?:\s*(?:วัน|ตรง))?|(?<![a-z])(?:noon|midday)(?![a-z])"):
        return {"h": 12, "m": 0, "clock": False}
    m = t.take(r"ตี\s*" + _NUM + _TAIL)
    if m:
        return _checked(int(m.group(1)), _minutes(m.group(2), m.group(3)), 1, 5, "ตี", lambda n: n)
    m = t.take(r"บ่าย\s*" + _NUM + r"(?:\s*โมง)?(?:\s*เย็น)?" + _TAIL)
    if m:
        return _checked(int(m.group(1)), _minutes(m.group(2), m.group(3)), 1, 6, "บ่าย", lambda n: n + 12)
    m = t.take(r"บ่าย\s*โมง" + _TAIL)
    if m:
        return _checked(1, _minutes(m.group(1), m.group(2)), 1, 1, "บ่าย", lambda n: 13)
    m = t.take(_NUM + r"\s*โมง\s*เย็น" + _TAIL)
    if m:
        return _checked(int(m.group(1)), _minutes(m.group(2), m.group(3)), 1, 6, "โมงเย็น", lambda n: n + 12)
    m = t.take(_NUM + r"\s*โมง\s*เช้า" + _TAIL)
    if m:
        return _checked(int(m.group(1)), _minutes(m.group(2), m.group(3)), 1, 11, "โมงเช้า",
                        lambda n: n + 6 if n <= 5 else n)
    m = t.take(r"โมง\s*เช้า" + _TAIL)
    if m:
        return _checked(7, _minutes(m.group(1), m.group(2)), 7, 7, "โมงเช้า", lambda n: n)
    m = t.take(_NUM + r"\s*ทุ่ม" + _TAIL)
    if m:
        return _checked(int(m.group(1)), _minutes(m.group(2), m.group(3)), 1, 6, "ทุ่ม", lambda n: (n + 18) % 24)
    m = t.take(_NUM + r"\s*(?:นาฬิกา|น\.)")
    if m:
        return _checked(int(m.group(1)), 0, 0, 23, "นาฬิกา", lambda n: n)
    m = t.take(_NUM + r"\s*โมง" + _TAIL)
    if m:
        hour, minute = int(m.group(1)), _minutes(m.group(2), m.group(3))
        if pm_hint and 1 <= hour <= 6:
            return _checked(hour, minute, 1, 6, "โมง", lambda n: n + 12)
        if 1 <= hour <= 6:
            morning = hour + 6 if hour <= 5 else 6
            raise ValueError(f"'{hour} โมง' กำกวม — ระบุให้ชัด เช่น บ่าย {hour} โมง ({hour + 12}:00) "
                             f"หรือ {hour} โมงเช้า ({morning:02d}:00)")
        if 7 <= hour <= 12 and (pm_hint or _NIGHT_HINT.search(t.s)):
            # "ทุกเย็น 7 โมง" must not quietly become 07:00; "7 โมงเย็น" is rejected the same way
            options = ("เที่ยงคืน (00:00) หรือ เที่ยง (12:00)" if hour == 12
                       else f"{hour - 6} ทุ่ม ({hour + 12}:00) หรือ {hour} โมงเช้า ({hour:02d}:00)")
            raise ValueError(f"'{hour} โมง' กับคำว่า เย็น/ค่ำ/คืน กำกวม — ระบุให้ชัด เช่น {options}")
        return _checked(hour, minute, 7, 12, "โมง", lambda n: n)
    return None


# ------------------------------------------------------------------------------ durations
_DURATION_UNITS = (
    (r"นาที|minutes?|mins?|m(?![a-z])", 60),
    (r"ชั่วโมง|ชม\.?|hours?|hrs?|h(?![a-z])", 3600),
    (r"วัน(?!ที่|นี้|จันทร์|อังคาร|พุธ|พฤหัส|ศุกร์|เสาร์|อาทิตย์|ทำงาน|ธรรมดา)|days?(?![a-z])|d(?![a-z])", 86400),
    (r"สัปดาห์|อาทิตย์|weeks?(?![a-z])|wks?(?![a-z])|w(?![a-z])", 604800),
)


def _take_duration(t: _Text) -> timedelta | None:
    seconds, found = 0.0, False
    m = t.take(r"(?<![\d.:/])(\d{1,4})\s*(?:ชั่วโมง|ชม\.?)\s*ครึ่ง")
    if m:
        seconds, found = seconds + int(m.group(1)) * 3600 + 1800, True
    if t.take(r"ชั่วโมง\s*ครึ่ง"):
        seconds, found = seconds + 5400, True
    if t.take(r"ครึ่ง\s*(?:ชั่วโมง|ชม\.?)|(?<![a-z])half\s+(?:an\s+)?hour(?![a-z])"):
        seconds, found = seconds + 1800, True
    for pattern, unit in _DURATION_UNITS:
        while True:
            m = t.take(r"(?<![\d.:/])(\d{1,4}(?:\.\d)?)\s*(?:" + pattern + r")")
            if not m:
                break
            seconds, found = seconds + float(m.group(1)) * unit, True
    return timedelta(seconds=round(seconds)) if found else None


# ------------------------------------------------------------------------------ cron
def _cron_field(text: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        m = re.fullmatch(r"(\*|\d{1,2}(?:-\d{1,2})?)(?:/(\d{1,2}))?", part)
        if not m:
            raise ValueError(f"ช่อง cron '{text}' ไม่ถูกต้อง (ใช้ตัวเลข, *, a-b, */n, a,b)")
        base, step = m.group(1), int(m.group(2) or 1)
        if base == "*":
            first, last = low, high
        elif "-" in base:
            first, last = (int(x) for x in base.split("-"))
        else:
            first = int(base)
            last = high if m.group(2) else first
        if not (low <= first <= last <= high) or step < 1:
            raise ValueError(f"ช่อง cron '{text}' อยู่นอกช่วง {low}-{high}")
        values.update(range(first, last + 1, step))
    return values


def _cron_next(expr: str, after: datetime) -> datetime | None:
    fields = expr.split()
    minutes = sorted(_cron_field(fields[0], 0, 59))
    hours = sorted(_cron_field(fields[1], 0, 23))
    doms = _cron_field(fields[2], 1, 31)
    months = _cron_field(fields[3], 1, 12)
    dows = {d % 7 for d in _cron_field(fields[4], 0, 7)}
    # Vixie/croner rule: when both day fields are restricted, either one matching is enough.
    either = not fields[2].startswith("*") and not fields[4].startswith("*")
    after = after.astimezone(TZ)
    day = after.date()
    for _ in range(366 * 5):  # Feb 29 needs up to four years
        if day.month in months:
            dom_ok, dow_ok = day.day in doms, (day.weekday() + 1) % 7 in dows
            if (dom_ok or dow_ok) if either else (dom_ok and dow_ok):
                for hour in hours:
                    for minute in minutes:
                        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
                        if candidate > after:
                            return candidate
        day += timedelta(days=1)
    return None


def _dow_field(dows: list[int]) -> str:
    unique = sorted(set(d % 7 for d in dows))
    if len(unique) == 7:
        return "*"
    if unique == [1, 2, 3, 4, 5]:
        return "1-5"
    return ",".join(str(d) for d in unique)


def _dow_text(dows: list[int]) -> str:
    unique = set(d % 7 for d in dows)
    if len(unique) == 7:
        return "ทุกวัน"
    if unique == {1, 2, 3, 4, 5}:
        return "ทุกวันจันทร์-ศุกร์"
    if unique == {0, 6}:
        return "ทุกวันเสาร์-อาทิตย์"
    names = [_TH_WEEKDAY_NAMES[(d - 1) % 7] for d in sorted(unique, key=lambda d: (d - 1) % 7)]
    return "ทุกวัน" + ", ".join(names)


def _cron_schedule(expr: str, summary: str, now: datetime, note: str = "") -> Schedule:
    next_run = _cron_next(expr, now)
    if next_run is None:
        raise ValueError(f"ตาราง '{expr}' ไม่มีรอบเตือนถัดไป (เช่น วันที่ที่ไม่มีจริง)")
    return Schedule("cron", None, expr, summary, next_run, note)


def _raw_cron_schedule(expr: str, now: datetime) -> Schedule:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron ต้องมี 5 ช่อง (นาที ชั่วโมง วันที่ เดือน วันในสัปดาห์) — ไม่รองรับช่องวินาที")
    if not re.fullmatch(r"\d{1,2}", fields[0]):
        raise ValueError("เตือนซ้ำได้ถี่สุดชั่วโมงละครั้ง — ช่องนาทีของ cron ต้องเป็นตัวเลขเดียว เช่น '30 7 * * *'")
    for field, (low, high) in zip(fields, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))):
        _cron_field(field, low, high)
    expr = " ".join(fields)
    return _cron_schedule(expr, f"ตามตาราง cron '{expr}' (เวลาไทย)", now)


# ------------------------------------------------------------------------------ resolve
def _combine(day: date, tod: dict[str, Any]) -> datetime:
    return datetime(day.year, day.month, day.day, tod["h"], tod["m"], tzinfo=TZ)


def _make_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        month_text = _TH_MONTH_ABBR[month - 1] if 1 <= month <= 12 else f"เดือน {month}"
        raise ValueError(f"ไม่มีวันที่ {day} {month_text} {year + 543}") from None


def _next_week_day(dow: int, tod: dict[str, Any], now: datetime) -> date:
    """'<weekday>หน้า' / 'next <weekday>': that weekday in the following week, never this week.

    Weeks start on Sunday on a Thai calendar and on Monday at work. Both give the same day unless
    today or the target is a Sunday; then the phrase is rejected with both dates, not guessed."""
    today = now.date()
    monday_week = today + timedelta(days=7 - today.weekday() + (dow - 1) % 7)
    sunday_week = today + timedelta(days=7 - (today.weekday() + 1) % 7 + dow)
    if monday_week == sunday_week:
        return monday_week
    first, second = sorted((monday_week, sunday_week))
    at = f"{tod['h']:02d}:{tod['m']:02d}"
    raise ValueError(
        f"'วัน{_TH_WEEKDAY_NAMES[(dow - 1) % 7]}หน้า' กำกวม (อาจเป็นวันที่ {first.day} {_TH_MONTH_ABBR[first.month - 1]} "
        f"หรือ {second.day} {_TH_MONTH_ABBR[second.month - 1]}) — ระบุวันที่ให้ชัด เช่น "
        f"{first.day} {_TH_MONTH_ABBR[first.month - 1]} {at} หรือ {second.day} {_TH_MONTH_ABBR[second.month - 1]} {at}")


def _resolve_once(spec: dict[str, Any] | None, tod: dict[str, Any], now: datetime) -> datetime:
    """The first matching moment; past moments are returned as-is for the caller to reject."""
    if spec is None:
        candidate = _combine(now.date(), tod)
        return candidate if candidate > now else _combine(now.date() + timedelta(days=1), tod)
    kind = spec["kind"]
    if kind == "rel":
        return _combine(now.date() + timedelta(days=spec["days"]), tod)
    if kind == "weekday":
        if spec["next"]:
            return _combine(_next_week_day(spec["dow"], tod, now), tod)
        for offset in range(0, 8):
            day = now.date() + timedelta(days=offset)
            if (day.weekday() + 1) % 7 == spec["dow"] and _combine(day, tod) > now:
                return _combine(day, tod)
        raise ValueError("หาวันในสัปดาห์ที่ระบุไม่พบ")
    if kind == "dom":
        year, month = now.year, now.month
        for _ in range(14):
            try:
                candidate = _combine(date(year, month, spec["day"]), tod)
                if candidate > now:
                    return candidate
            except ValueError:
                pass
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        raise ValueError(f"ไม่มีวันที่ {spec['day']} ในเดือนถัด ๆ ไป")
    if "offset" in spec:
        moment = datetime(spec["year"], spec["month"], spec["day"], tod["h"], tod["m"], tzinfo=timezone(spec["offset"]))
        return moment.astimezone(TZ)
    if spec.get("year") is not None:
        return _combine(_make_date(spec["year"], spec["month"], spec["day"]), tod)
    errors = []
    for year in (now.year, now.year + 1):
        try:
            candidate = _combine(_make_date(year, spec["month"], spec["day"]), tod)
        except ValueError as exc:
            errors.append(exc)
            continue
        if candidate > now:
            return candidate
    if len(errors) == 2:
        raise errors[0]
    return candidate


def _once(at: datetime, now: datetime) -> Schedule:
    if at <= now:
        raise ValueError(f"เวลาที่ระบุผ่านไปแล้ว ({_fmt_th(at)})")
    if at - now > MAX_AHEAD:
        raise ValueError(f"ตั้งเตือนล่วงหน้าได้ไม่เกิน 365 วัน ({_fmt_th(at)})")
    return Schedule("once", at, None, _fmt_th(at), at)


def _build_cron(rec: dict[str, Any], spec: dict[str, Any] | None, tod: dict[str, Any] | None, now: datetime) -> Schedule:
    kind = rec["type"]
    if kind == "hourly":
        if spec:
            raise ValueError("การเตือนทุกชั่วโมงระบุวันไม่ได้")
        every, minute = rec["every"], tod["m"] if tod else 0
        if every == 1:
            return _cron_schedule(f"{minute} * * * *", f"ทุกชั่วโมง ตรงนาทีที่ {minute:02d}", now)
        hours = list(range(tod["h"] % every if tod else 0, 24, every))
        times = ", ".join(f"{h:02d}:{minute:02d}" for h in hours)
        return _cron_schedule(f"{minute} {','.join(map(str, hours))} * * *", f"ทุก {every} ชั่วโมง ({times} น.)", now)
    if not tod:
        raise ValueError("ระบุเวลาของการเตือนซ้ำด้วย เช่น ทุกวัน 07:30")
    hour, minute = tod["h"], tod["m"]
    at_text = f"เวลา {hour:02d}:{minute:02d} น."
    if kind in ("daily", "dow", "dom") and spec:
        raise ValueError("การเตือนซ้ำยังระบุวันเริ่มไม่ได้ — ตั้งเป็น เช่น ทุกวัน 07:30 แล้วค่อยยกเลิกเมื่อไม่ต้องการ")
    if kind == "daily":
        return _cron_schedule(f"{minute} {hour} * * *", f"ทุกวัน {at_text}", now)
    if kind == "dow":
        return _cron_schedule(f"{minute} {hour} * * {_dow_field(rec['dows'])}", f"{_dow_text(rec['dows'])} {at_text}", now)
    if kind == "weekly":
        dow = spec["dow"] if spec and spec["kind"] == "weekday" else (_resolve_once(spec, tod, now).weekday() + 1) % 7
        return _cron_schedule(f"{minute} {hour} * * {dow}", f"{_dow_text([dow])} {at_text}", now)
    if kind in ("dom", "monthly"):
        if kind == "dom":
            day = rec["day"]
        elif spec and spec["kind"] in ("dom", "date"):
            day = spec["day"]
        else:
            day = _resolve_once(spec, tod, now).day
        if not 1 <= day <= 31:
            raise ValueError(f"ไม่มีวันที่ {day}")
        note = f"เดือนที่ไม่มีวันที่ {day} จะไม่เตือน" if day > 28 else ""
        return _cron_schedule(f"{minute} {hour} {day} * *", f"ทุกวันที่ {day} ของเดือน {at_text}", now, note)
    # yearly
    if "month" in rec:
        month, day = rec["month"], rec["day"]
    elif spec and spec["kind"] == "date":
        month, day = spec["month"], spec["day"]
    else:
        anchor = _resolve_once(spec, tod, now)
        month, day = anchor.month, anchor.day
    _make_date(2028, month, day)  # a leap year, so 29 ก.พ. is allowed
    return _cron_schedule(f"{minute} {hour} {day} {month} *", f"ทุกปี วันที่ {day} {_TH_MONTH_ABBR[month - 1]} {at_text}", now)


def parse_when(when: str, repeat: str = "", now: datetime | None = None) -> Schedule:
    """Turn Thai/English `when` (+ optional `repeat`) into a one-shot time or a cron expression."""
    now = (now or _now()).astimezone(TZ)
    when_raw, repeat_raw = str(when or "").strip(), str(repeat or "").strip()
    if len(when_raw) > 200 or len(repeat_raw) > 100:
        raise ValueError("when / repeat ยาวเกินไป")
    rep = _normalize(repeat_raw)
    rep = "" if rep in _NO_REPEAT else rep
    for candidate in (rep, "" if rep else _normalize(when_raw)):
        if candidate and _CRON_SHAPE.fullmatch(candidate):
            return _raw_cron_schedule(candidate, now)
    t = _Text(_normalize(f"{rep} {when_raw}"))
    if not t.s:
        raise ValueError(f"ระบุเวลา (when) ด้วย — {EXAMPLES_TH}")
    rec = _take_recurrence(t)
    if rec:
        again = _take_recurrence(t)
        if again:
            rec = _same_recurrence(rec, again)
            if rec is None:
                if rep:
                    raise ValueError("การเตือนซ้ำใน when กับ repeat ไม่ตรงกัน — ระบุที่เดียว")
                raise ValueError("ระบุการเตือนซ้ำได้แบบเดียวต่อรายการ — เช่น ทุกวันจันทร์ ศุกร์ 09:00 "
                                 "หรือ every monday and friday 9am")
    if re.search(r"ทุก|(?<![a-z])every(?![a-z])", t.s):
        raise ValueError("ไม่เข้าใจรูปแบบการเตือนซ้ำ — ใช้ เช่น ทุกวัน 07:30, ทุกวันจันทร์ 08:00, ทุกวันจันทร์-ศุกร์ 08:00, "
                         "ทุกวันที่ 1 09:00, ทุกชั่วโมง, ทุก 2 ชั่วโมง")
    spec = _take_date(t, now)
    if spec and _take_date(t, now):
        raise ValueError("ระบุวันได้ครั้งเดียวต่อรายการ")
    hint = (rec or {}).get("hint") or (spec or {}).get("hint") or ""
    tod = _take_time(t, pm_hint=hint in ("pm", "night") or bool(_PM_HINT.search(t.s)))
    if tod and _take_time(t):
        raise ValueError("ระบุเวลาได้ครั้งเดียวต่อรายการ — ถ้าต้องการหลายเวลา ให้ตั้งทีละรายการ")
    if spec and "time" in spec:
        if tod:
            raise ValueError("ระบุเวลาได้ครั้งเดียวต่อรายการ")
        tod = _with_ampm(*spec["time"], None)
        tod["clock"] = False
    duration = _take_duration(t)
    leftover = " ".join(t.s.split())
    hint = hint or ("pm" if _PM_HINT.search(leftover) else "night" if _NIGHT_HINT.search(leftover) else "")
    rest = re.sub(r"[\s,;:.\-–/@()\"'!?+]+", "", _FILLER_EN.sub(" ", _FILLER_TH.sub(" ", leftover)))
    if rest:
        raise ValueError(f"ไม่เข้าใจส่วนนี้ของเวลา: '{leftover}' — {EXAMPLES_TH}")
    if tod and tod["clock"] and (spec or {}).get("hint") == "night" and (tod["h"] == 12 or tod["h"] <= 5):
        # "คืนนี้ 12:30 / 1:00" means after midnight, i.e. tomorrow's date
        raise ValueError("'คืนนี้' กับเวลาหลังเที่ยงคืนกำกวม — ใช้ เช่น พรุ่งนี้ 00:30 หรือ ตีหนึ่ง")
    if tod and not tod["clock"] and (spec or {}).get("hint") == "night" and tod["h"] <= 5 and now.hour >= 6:
        # "คืนนี้ ตีหนึ่ง / เที่ยงคืน / tonight 1am" said before the night: after midnight is tomorrow's
        # date. Said after midnight (00:00-05:59) it stays today, so "คืนนี้ ตี 2" at 00:30 is 02:00.
        spec = {**spec, "days": spec["days"] + 1}
    if tod and tod["clock"] and ((hint == "pm" and 1 <= tod["h"] <= 11) or (hint == "night" and 6 <= tod["h"] <= 11)):
        tod = {**tod, "h": tod["h"] + 12}
    if duration is not None:
        if rec:
            raise ValueError("ใช้ 'อีก ... นาที/ชั่วโมง' กับการเตือนซ้ำไม่ได้ — ใช้เวลา เช่น ทุกวัน 07:30")
        if spec:
            raise ValueError("ใช้ 'อีก ...' ร่วมกับวันที่ไม่ได้ — เลือกอย่างใดอย่างหนึ่ง")
        if tod:
            if duration.total_seconds() % 86400:
                raise ValueError("ใช้ 'อีก ...' ร่วมกับเวลาได้เฉพาะจำนวนวัน เช่น อีก 2 วัน 08:00")
            return _once(_combine(now.date() + timedelta(days=duration.days), tod), now)
        if duration < MIN_DURATION:
            raise ValueError("ตั้งเตือนล่วงหน้าอย่างน้อย 1 นาที")
        if duration > MAX_AHEAD:
            raise ValueError("ตั้งเตือนล่วงหน้าได้ไม่เกิน 365 วัน")
        return _once((now + duration).replace(microsecond=0), now)
    if rec:
        return _build_cron(rec, spec, tod, now)
    if not tod:
        if spec:
            raise ValueError("ระบุเวลาด้วย เช่น พรุ่งนี้ 08:00")
        raise ValueError(f"ไม่พบเวลาในข้อความ — {EXAMPLES_TH}")
    return _once(_resolve_once(spec, tod, now), now)


# ------------------------------------------------------------------------------ formatting
def _fmt_th(moment: datetime) -> str:
    moment = moment.astimezone(TZ)
    return (f"วัน{_TH_WEEKDAY_NAMES[moment.weekday()]}ที่ {moment.day} {_TH_MONTH_ABBR[moment.month - 1]} "
            f"{moment.year + 543} เวลา {moment:%H:%M} น.")


def _in_th(delta: timedelta) -> str:
    minutes = max(0, int((delta.total_seconds() + 59) // 60))
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    parts = [f"{days} วัน" if days else "", f"{hours} ชั่วโมง" if hours else "", f"{minutes} นาที" if minutes else ""]
    text = " ".join(p for p in parts if p)
    return f"อีก {text}" if text else "ภายใน 1 นาที"


# ------------------------------------------------------------------------------ storage
def _record_path(reminder_id: str) -> Path:
    if not ID_RE.fullmatch(reminder_id):
        raise ValueError("reminder_id ไม่ถูกต้อง")
    return DATA_ROOT / f"{reminder_id}.json"


def _write_record(record: dict[str, Any]) -> None:
    path = _record_path(record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _read_record(reminder_id: str) -> dict[str, Any] | None:
    try:
        value = json.loads(_record_path(reminder_id).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _load_records() -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not DATA_ROOT.is_dir():
        return records
    for path in DATA_ROOT.glob("*.json"):
        if ID_RE.fullmatch(path.stem):
            record = _read_record(path.stem)
            if record:
                records[path.stem] = record
    return records


def _delete_record(reminder_id: str) -> None:
    try:
        _record_path(reminder_id).unlink(missing_ok=True)
    except OSError:
        pass


def _new_id() -> str:
    while True:
        reminder_id = secrets.token_hex(6)
        if not _record_path(reminder_id).exists():
            return reminder_id


def _clean_text(text: str) -> str:
    body = unicodedata.normalize("NFC", str(text or "")).replace("\r\n", "\n").replace("\r", "\n")
    body = "".join(
        ch for ch in body
        if ch in "\n\t" or (unicodedata.category(ch) != "Cc" and not "\u202a" <= ch <= "\u202e" and not "\u2066" <= ch <= "\u2069")
    )
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not body:
        raise ValueError("ระบุข้อความที่จะเตือน (text) ด้วย")
    if len(body) > MAX_TEXT_CHARS:
        raise ValueError(f"ข้อความเตือนยาวเกิน {MAX_TEXT_CHARS} ตัวอักษร")
    if any(line.strip().upper().startswith("MEDIA:") for line in body.split("\n")):
        # MEDIA:<path> is OpenClaw's file-attachment directive; a reminder carries text only.
        raise ValueError("ข้อความเตือนห้ามมีบรรทัดที่ขึ้นต้นด้วย MEDIA:")
    return body


# ------------------------------------------------------------------------------ OpenClaw CLI
class _CliTimeout(RuntimeError):
    pass


def _owner_telegram_to() -> str:
    """The owner's Telegram chat id from commands.ownerAllowFrom ("telegram:<id>"); never logged."""
    try:
        config = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise RuntimeError("อ่าน openclaw.json ไม่ได้ — ตั้งเตือนไม่ได้เพราะไม่รู้ว่าจะส่งไป Telegram ไหน") from None
    entries = (config.get("commands") or {}).get("ownerAllowFrom") if isinstance(config, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        value = str(entry).strip()
        if value.lower().startswith("telegram:"):
            value = value.split(":", 1)[1].strip()
            if re.fullmatch(r"-?\d{5,20}", value):
                return value
    raise RuntimeError("ไม่พบ Telegram ของเจ้าของ (commands.ownerAllowFrom ใน openclaw.json) — ตั้งเตือนไม่ได้")


def _openclaw_launcher() -> list[str]:
    if OPENCLAW_NODE.exists() and OPENCLAW_MJS.exists():
        return [str(OPENCLAW_NODE), str(OPENCLAW_MJS)]
    if OPENCLAW_CMD.exists():
        return [str(OPENCLAW_CMD)]
    raise RuntimeError("ไม่พบ OpenClaw (openclaw-runtime) — ตั้งเตือนไม่ได้")


def _echo_argv(reminder_id: str) -> list[str]:
    python = PYTHON if PYTHON.exists() else Path(sys.executable)
    return [str(python), str(ECHO_SCRIPT), reminder_id]


def build_add_args(reminder_id: str, schedule: Schedule, owner_to: str) -> list[str]:
    """Arguments after the launcher for `automations add`; every flag checked against --help 2026-09-30."""
    args = ["automations", "add", "--name", f"{NAME_PREFIX} {reminder_id}"]
    if schedule.kind == "once":
        args += ["--at", schedule.at.astimezone(TZ).isoformat(timespec="seconds")]
    else:
        # --exact: OpenClaw staggers top-of-hour cron jobs by up to 5 minutes by default.
        args += ["--cron", schedule.cron, "--tz", TZ_NAME, "--exact"]
    args += [
        "--command-argv", json.dumps(_echo_argv(reminder_id)),
        "--announce", "--channel", "telegram", "--to", owner_to,
    ]
    if schedule.kind == "once":
        args.append("--delete-after-run")  # one-shots only: the scheduler deletes nothing but at-jobs
    args += ["--timeout-seconds", str(COMMAND_TIMEOUT_SECONDS), "--json"]
    return args


def _decode(data: Any) -> str:
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    return str(data or "").lstrip("\ufeff")


def _parse_json(text: str) -> Any:
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[min(starts):])[0]
    except ValueError:
        return None


def _run_openclaw(args: list[str], redact: tuple[str, ...] = ()) -> Any:
    argv = [*_openclaw_launcher(), *args]
    if argv[0].lower().endswith(".cmd") and any(re.search(r"[%!^&|<>]", a) for a in argv):
        raise RuntimeError("อาร์กิวเมนต์มีอักขระที่ cmd.exe ตีความ — ยกเลิกเพื่อความปลอดภัย")
    env = dict(os.environ, NO_COLOR="1", FORCE_COLOR="0")
    try:
        proc = _subprocess_run(
            argv, capture_output=True, timeout=CLI_TIMEOUT, env=env,
            cwd=str(OPENCLAW_ROOT) if OPENCLAW_ROOT.is_dir() else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise _CliTimeout(f"OpenClaw ไม่ตอบภายใน {CLI_TIMEOUT} วินาที — ตรวจว่า OpenClaw Gateway (พอร์ต 18789) ทำงานอยู่") from None
    except OSError as exc:
        raise RuntimeError(f"เรียก OpenClaw ไม่ได้: {exc}") from None
    stdout, stderr = _decode(proc.stdout), _decode(proc.stderr)
    payload = _parse_json(stdout)
    if proc.returncode != 0 or payload is None or (isinstance(payload, dict) and payload.get("ok") is False):
        message = ""
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            message = str(payload["error"].get("message") or "")
        message = message or (stderr.strip() or stdout.strip())[-600:] or f"exit code {proc.returncode}"
        for secret in redact:
            if secret:
                message = message.replace(secret, "<owner>")
        message = re.sub(r"telegram:\s*-?\d{5,}", "telegram:<owner>", message)
        raise RuntimeError(f"OpenClaw ปฏิเสธคำสั่ง: {message}")
    return payload


def _rid_from_name(name: Any) -> str:
    m = re.fullmatch(re.escape(NAME_PREFIX) + r" ([0-9a-f]{12})", str(name or "").strip())
    return m.group(1) if m else ""


def _reminder_jobs() -> list[dict[str, Any]]:
    """Only jobs named "qwen-reminder <id>"; the Gateway's own system jobs are never touched."""
    payload = _run_openclaw(["automations", "list", "--all", "--json"])
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise RuntimeError("OpenClaw ตอบรายการ automation ในรูปแบบที่ไม่รู้จัก")
    return [job for job in jobs if isinstance(job, dict) and _rid_from_name(job.get("name"))]


def _job_next_run(job: dict[str, Any]) -> datetime | None:
    value = job.get("nextRunAtMs") or (job.get("state") or {}).get("nextRunAtMs")
    try:
        return datetime.fromtimestamp(float(value) / 1000, TZ) if value else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _describe_job(job: dict[str, Any]) -> str:
    schedule = job.get("schedule") or {}
    if schedule.get("kind") == "at":
        try:
            return _fmt_th(datetime.fromisoformat(str(schedule.get("at"))))
        except ValueError:
            return str(schedule.get("at"))
    if schedule.get("kind") == "cron":
        return f"ตามตาราง cron '{schedule.get('expr')}' ({schedule.get('tz') or 'เวลาเครื่อง'})"
    return str(schedule.get("kind") or "-")


def _stamp() -> str:
    return _now().isoformat(timespec="seconds")


# ------------------------------------------------------------------------------ public actions
def schedule_reminder(when: str, text: str, repeat: str = "") -> dict[str, Any]:
    body = _clean_text(text)
    schedule = parse_when(when, repeat, now=_now())
    if not ECHO_SCRIPT.exists():
        raise RuntimeError("ไม่พบ reminder_echo.py — ตั้งเตือนไม่ได้")
    owner = _owner_telegram_to()
    with _LOCK:
        jobs = _reminder_jobs()
        if len(jobs) >= MAX_ACTIVE:
            raise ValueError(f"มีรายการเตือนค้างอยู่ {len(jobs)} รายการ (สูงสุด {MAX_ACTIVE}) — ยกเลิกบางรายการด้วย cancel_reminder ก่อน")
        reminder_id = _new_id()
        record = {
            "id": reminder_id, "text": body, "when": str(when or ""), "repeat": str(repeat or ""),
            "kind": schedule.kind, "at": schedule.at.isoformat(timespec="seconds") if schedule.at else None,
            "cron": schedule.cron, "tz": TZ_NAME, "summary_th": schedule.summary_th,
            "status": "pending", "job_id": None, "fire_count": 0,
            "created_at": _stamp(), "updated_at": _stamp(),
        }
        # The text must be on disk before the job exists: a 1-minute reminder can fire at once.
        _write_record(record)
        try:
            payload = _run_openclaw(build_add_args(reminder_id, schedule, owner), redact=(owner,))
        except _CliTimeout:
            record.update(status="unknown", updated_at=_stamp())
            _write_record(record)
            raise RuntimeError("OpenClaw ไม่ตอบ — ไม่แน่ใจว่าตั้งสำเร็จหรือไม่ ตรวจด้วย list_reminders ก่อนตั้งซ้ำ") from None
        except Exception:
            _delete_record(reminder_id)
            raise
        job = payload.get("job") if isinstance(payload, dict) and isinstance(payload.get("job"), dict) else payload
        job_id = str((job or {}).get("id") or "") if isinstance(job, dict) else ""
        record.update(status="active", job_id=job_id or None, updated_at=_stamp())
        _write_record(record)
    result: dict[str, Any] = {
        "status": "scheduled",
        "reminder_id": reminder_id,
        "when_th": schedule.summary_th,
        "next_run_th": _fmt_th(schedule.next_run),
        "next_run": schedule.next_run.isoformat(timespec="seconds"),
        "starts_in": _in_th(schedule.next_run - _now()),
        "repeat": schedule.summary_th if schedule.kind == "cron" else "ครั้งเดียว",
        "text": body,
        "delivered_to": "Telegram ของเจ้าของ (ตอนเตือนไม่เรียกโมเดล)",
        "active_count": len(jobs) + 1,
        "limit": MAX_ACTIVE,
        "note": "แจ้งผู้ใช้ตาม when_th · ยกเลิกด้วย cancel_reminder {\"reminder_id\": \"" + reminder_id + "\"}",
    }
    if schedule.cron:
        result["cron"] = schedule.cron
    if schedule.note_th:
        result["warning"] = schedule.note_th
    if job_id:
        result["job_id"] = job_id
    return result


def _reconcile(records: dict[str, dict[str, Any]], present: set[str]) -> None:
    """Mark records whose job is gone (fired one-shots, removed elsewhere) and prune old ones."""
    now = _now()
    for reminder_id, record in records.items():
        try:
            updated = datetime.fromisoformat(str(record.get("updated_at") or record.get("created_at")))
            updated = updated if updated.tzinfo else updated.replace(tzinfo=TZ)
        except ValueError:
            updated = now
        status = record.get("status")
        if reminder_id in present:
            continue
        if status in ("active", "pending", "fired", "unknown") and now - updated > timedelta(minutes=5):
            record.update(status="finished" if record.get("kind") == "once" else "missing", updated_at=_stamp())
            _write_record(record)
        elif status in ("finished", "missing", "cancelled") and now - updated > timedelta(days=KEEP_FINISHED_DAYS):
            _delete_record(reminder_id)


def list_reminders() -> dict[str, Any]:
    jobs = _reminder_jobs()
    records = _load_records()
    items = []
    for job in jobs:
        reminder_id = _rid_from_name(job.get("name"))
        record = records.get(reminder_id) or {}
        next_run = _job_next_run(job)
        schedule = job.get("schedule") or {}
        item: dict[str, Any] = {
            "reminder_id": reminder_id,
            "text": (record.get("text") or "(ไม่พบข้อความในเครื่อง)")[:300],
            "schedule": record.get("summary_th") or _describe_job(job),
            "repeat": "ครั้งเดียว" if schedule.get("kind") == "at" else str(schedule.get("expr") or schedule.get("kind") or "-"),
            "next_run_th": _fmt_th(next_run) if next_run else "-",
            "next_run": next_run.isoformat(timespec="seconds") if next_run else None,
            "enabled": bool(job.get("enabled", True)),
            "last_status": job.get("lastRunStatus") or (job.get("state") or {}).get("lastRunStatus"),
        }
        if not item["enabled"]:
            item["note"] = "ปิดอยู่ (มักเพราะส่งเข้า Telegram ไม่สำเร็จ) — ยกเลิกแล้วตั้งใหม่ได้"
        items.append(item)
    items.sort(key=lambda item: item["next_run"] or "9999")
    try:
        _reconcile(records, {item["reminder_id"] for item in items})
    except OSError:
        pass
    return {"status": "listed", "count": len(items), "limit": MAX_ACTIVE, "timezone": TZ_NAME, "reminders": items}


def cancel_reminder(reminder_id: str) -> dict[str, Any]:
    ref = str(reminder_id or "").strip().lower()
    ref = re.sub(r"^" + re.escape(NAME_PREFIX) + r"\s*", "", ref).strip()
    if not (ID_RE.fullmatch(ref) or UUID_RE.fullmatch(ref)):
        raise ValueError("reminder_id ไม่ถูกต้อง — ใช้รหัส 12 ตัวจาก schedule_reminder หรือ list_reminders")
    with _LOCK:
        jobs = _reminder_jobs()
        job = next((j for j in jobs if _rid_from_name(j.get("name")) == ref or str(j.get("id") or "").lower() == ref), None)
        if job is None:
            record = _read_record(ref) if ID_RE.fullmatch(ref) else None
            if record and record.get("status") in ("active", "pending", "unknown", "fired"):
                record.update(status="finished" if record.get("kind") == "once" else "missing", updated_at=_stamp())
                _write_record(record)
            return {
                "status": "not_found",
                "reminder_id": ref,
                "message": "ไม่พบรายการเตือนนี้ในตัวตั้งเวลา — อาจเตือนไปแล้วหรือถูกยกเลิกแล้ว ดูรายการที่ยังค้างด้วย list_reminders",
            }
        rid = _rid_from_name(job.get("name"))
        payload = _run_openclaw(["automations", "rm", str(job.get("id")), "--json"])
        if isinstance(payload, dict) and payload.get("removed") is False:
            raise RuntimeError("OpenClaw ไม่ได้ลบรายการนี้")
        record = _read_record(rid)
        if record:
            record.update(status="cancelled", updated_at=_stamp())
            _write_record(record)
    return {
        "status": "cancelled",
        "reminder_id": rid,
        "text": (record or {}).get("text"),
        "schedule": (record or {}).get("summary_th") or _describe_job(job),
    }
