"""Morning brief for the owner's Telegram - deterministic text, no model (added 2026-09-30).

The OpenClaw automation "qwen-morning-brief" runs [webui-venv python.exe, this file] at 07:00
Asia/Bangkok and announces stdout to the owner's Telegram; no model is loaded. The brief is
Thai, at most MAX_CHARS characters:

  date (Thai, Buddhist year) · today's calendar · unread mail count + newest unread (sender
  display name + subject) · today's reminders · one-line suite status

Sources, each failing soft into one Thai line so the rest still arrives:
- Outlook: outlook_tools.brief_data() - one child process under a time limit, never sends mail;
- reminders: reminders.list_reminders() (OpenClaw CLI), else the local records in data\\reminders;
- status: suite_health.report(only="light") - read-only, 127.0.0.1 only.
The three run in parallel; the whole brief is printed within ~60 s even if one hangs.

Like reminder_echo.py: stderr is silenced (OpenClaw would append it to the message), output is
UTF-8 bytes, exit code is always 0, and "MEDIA:" in mail text is defanged (OpenClaw's attachment
directive), so a crafted subject cannot attach a local file to the Telegram message.

Usage: python morning_brief.py        (prints the brief)
"""
from __future__ import annotations

import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

TZ = timezone(timedelta(hours=7), "Asia/Bangkok")
MAX_CHARS = 1500
OUTLOOK_TIMEOUT_S = 45.0
REMINDERS_CLI_TIMEOUT_S = 20
HEALTH_DEADLINE_S = 10.0
OVERALL_WAIT_S = 60.0

TH_WEEKDAYS = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]  # date.weekday() order
TH_MONTHS = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน", "กรกฎาคม", "สิงหาคม",
             "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]

H_TITLE, H_CAL, H_MAIL, H_REM, H_SYS, H_WARN = "☀️", "📅", "📧", "⏰", "🖥️", "⚠️"
SECTION_MARKS = (H_TITLE, H_CAL, H_MAIL, H_REM, H_SYS)


def thai_date(day: date) -> str:
    return f"วัน{TH_WEEKDAYS[day.weekday()]}ที่ {day.day} {TH_MONTHS[day.month - 1]} {day.year + 543}"


def _text(value: Any, limit: int) -> str:
    text = re.sub(r"[‪-‮⁦-⁩‎‏]", "", str(value or ""))
    text = re.sub(r"\s+", " ", "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in text)).strip()
    text = re.sub(r"(?i)\b(media):", r"\1 :", text)
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _reason(exc: BaseException, limit: int = 230) -> str:
    return _text(str(exc) or type(exc).__name__, limit)


# ------------------------------------------------------------------------------ sources
def outlook_section() -> dict[str, Any]:
    import outlook_tools

    return outlook_tools.brief_data(max_unread=5, timeout=OUTLOOK_TIMEOUT_S)


def _local_reminders(R: Any, now: datetime) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for record in R._load_records().values():
        if record.get("status") not in ("active", "pending", "unknown"):
            continue
        text = str(record.get("text") or "")
        if record.get("kind") == "once" and record.get("at"):
            try:
                at = datetime.fromisoformat(str(record["at"])).astimezone(TZ)
            except ValueError:
                continue
            if at.date() == now.date() and at > now:
                rows.append((at.strftime("%H:%M"), text))
        elif record.get("cron"):
            moment = now
            for _ in range(24):
                try:
                    moment = R._cron_next(str(record["cron"]), moment)
                except ValueError:
                    break
                if moment is None or moment.date() != now.date():
                    break
                rows.append((moment.strftime("%H:%M"), text))
    return rows


def reminders_section(now: datetime | None = None) -> dict[str, Any]:
    import reminders as R

    now = (now or datetime.now(TZ)).astimezone(TZ)
    R.CLI_TIMEOUT = REMINDERS_CLI_TIMEOUT_S  # this process only: the brief must not wait 60 s
    try:
        listed = R.list_reminders()
        rows = []
        for item in listed.get("reminders") or []:
            if not item.get("next_run") or item.get("enabled") is False:
                continue
            when = datetime.fromisoformat(str(item["next_run"])).astimezone(TZ)
            if when.date() == now.date():
                rows.append((when.strftime("%H:%M"), str(item.get("text") or "")))
        return {"rows": sorted(rows), "note": ""}
    except Exception as exc:
        rows = _local_reminders(R, now)  # raises -> the section shows the error
        return {"rows": sorted(rows), "note": f"จากไฟล์ในเครื่อง เพราะตัวตั้งเวลาไม่ตอบ ({_text(exc, 80)})"}


def health_section() -> dict[str, Any]:
    import suite_health as H

    rep = H.report(only="light", fresh=True, deadline=HEALTH_DEADLINE_S)
    return {"overall_th": rep.get("overall_th") or "?", "problems": list(rep.get("problems") or []),
            "counts": rep.get("counts") or {}, "took_s": rep.get("took_s")}


# ------------------------------------------------------------------------------ text
def _more(n: int) -> list[str]:
    return [f"• …และอีก {n} รายการ"] if n > 0 else []


def compose(today: date, outlook: Any, reminders_part: Any, health: Any,
            max_items: tuple[int, int, int] = (8, 5, 5), width: int = 90) -> str:
    """Build the brief. Each source is its result dict or the exception it raised."""
    cal_n, mail_n, rem_n = max_items
    lines = [f"{H_TITLE} สรุปเช้า {thai_date(today)}", ""]

    if isinstance(outlook, BaseException) or not isinstance(outlook, dict):
        lines.append(f"{H_CAL} อ่าน Outlook ไม่ได้: {_reason(outlook, 340) if isinstance(outlook, BaseException) else 'ไม่ทันเวลา'}")
    else:
        agenda = outlook.get("agenda") or []
        if agenda:
            lines.append(f"{H_CAL} นัดวันนี้ {len(agenda)} รายการ")
            for item in agenda[:cal_n]:
                when = "ทั้งวัน" if item.get("all_day") else (item.get("time") or "").replace(" - ", "-")
                extra = f" @{_text(item.get('location'), 30)}" if item.get("location") else ""
                if item.get("organizer"):
                    extra += f" ({_text(item.get('organizer'), 25)})"
                flag = "(ยกเลิก) " if item.get("cancelled") else ""
                lines.append(_text(f"• {when} {flag}{_text(item.get('subject'), 70)}{extra}", width))
            lines += _more(len(agenda) - cal_n)
        else:
            lines.append(f"{H_CAL} วันนี้ไม่มีนัดในปฏิทิน Outlook")
        lines.append("")
        unread = outlook.get("unread") or []
        count = int(outlook.get("unread_count") or 0)
        lines.append(f"{H_MAIL} อีเมลยังไม่อ่าน {count:,} ฉบับ" + (f" (ล่าสุด {min(len(unread), mail_n)})" if unread else ""))
        for item in unread[:mail_n]:
            lines.append(_text(f"• {_text(item.get('from') or '?', 30)}: {_text(item.get('subject'), 80)}", width))
        age = outlook.get("newest_mail_age_days")
        if isinstance(age, int) and age >= 3:
            lines.append(f"{H_WARN} อีเมลล่าสุดใน Outlook เมื่อ {age:,} วันก่อน — Outlook อาจไม่ได้ซิงก์")
    lines.append("")

    if isinstance(reminders_part, BaseException) or not isinstance(reminders_part, dict):
        lines.append(f"{H_REM} อ่านรายการเตือนไม่ได้: {_reason(reminders_part) if isinstance(reminders_part, BaseException) else 'ไม่ทันเวลา'}")
    else:
        rows = reminders_part.get("rows") or []
        note = f" ({reminders_part['note']})" if reminders_part.get("note") else ""
        lines.append((f"{H_REM} เตือนวันนี้ {len(rows)} รายการ" if rows else f"{H_REM} วันนี้ไม่มีรายการเตือน") + note)
        for at, text in rows[:rem_n]:
            lines.append(_text(f"• {at} {_text(text, 80)}", width))
        lines += _more(len(rows) - rem_n)
    lines.append("")

    if isinstance(health, BaseException) or not isinstance(health, dict):
        lines.append(f"{H_SYS} ตรวจสถานะระบบไม่ได้: {_reason(health) if isinstance(health, BaseException) else 'ไม่ทันเวลา'}")
    else:
        line = f"{H_SYS} ระบบ: {health.get('overall_th')}"
        if health.get("problems"):
            line += " — " + "; ".join(str(p) for p in health["problems"][:2])
        lines.append(_text(line, 220))
    return "\n".join(lines).strip()


def fit(today: date, outlook: Any, reminders_part: Any, health: Any) -> str:
    """Shrink item counts and line widths until the brief is within MAX_CHARS."""
    for max_items, width in (((8, 5, 5), 90), ((6, 5, 4), 75), ((5, 3, 3), 60), ((3, 3, 3), 50), ((2, 2, 2), 45)):
        text = compose(today, outlook, reminders_part, health, max_items, width)
        if len(text) <= MAX_CHARS:
            return text
    return text[: MAX_CHARS - 1].rstrip() + "…"


def build() -> str:
    now = datetime.now(TZ)
    results: dict[str, Any] = {}
    pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="brief")
    futures = {
        "outlook": pool.submit(outlook_section),
        "reminders": pool.submit(reminders_section, now),
        "health": pool.submit(health_section),
    }
    wait(list(futures.values()), timeout=OVERALL_WAIT_S)
    for key, future in futures.items():
        if not future.done():
            results[key] = TimeoutError("ไม่ทันเวลา")
            continue
        try:
            results[key] = future.result()
        except BaseException as exc:  # every section fails soft
            results[key] = exc
    pool.shutdown(wait=False, cancel_futures=True)
    return fit(now.date(), results["outlook"], results["reminders"], results["health"])


def main() -> int:
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except OSError:
        pass
    try:
        text = build()
    except BaseException as exc:  # never leave the owner without a message
        text = f"{H_TITLE} สรุปเช้า {thai_date(datetime.now(TZ).date())}\nสร้างสรุปไม่สำเร็จ: {_reason(exc)}"
    out = sys.stdout.buffer
    out.write(text.encode("utf-8"))
    out.flush()
    return 0


if __name__ == "__main__":
    code = main()
    # A section thread that overran its own limit must not keep the job alive.
    os._exit(code)
