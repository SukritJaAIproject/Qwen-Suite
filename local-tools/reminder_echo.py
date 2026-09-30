"""Print one stored reminder for the OpenClaw automation that fires it (added 2026-09-30).

reminders.py creates the automation with the fixed argv [python.exe, this file, <id>]. The
Gateway runs it and announces stdout to the owner's Telegram with no model call, so the text is
read from <install root>\\data\\reminders\\<id>.json here and never appears on a command line.

OpenClaw appends stderr to the announced message ("stdout: ... stderr: ..."), so stderr is
silenced and every failure still prints one usable line and exits 0. Output is written as UTF-8
bytes: OpenClaw decodes valid UTF-8 first, whatever the console code page is.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("QWEN_REMINDERS_ROOT") or Path(__file__).resolve().parent.parent / "data" / "reminders")
TZ = timezone(timedelta(hours=7), "Asia/Bangkok")
PREFIX = "⏰ เตือนความจำ: "


def _defang(text: str) -> str:
    # MEDIA:<path> is OpenClaw's attachment directive elsewhere; a reminder only carries text.
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        lines.append("MEDIA :" + stripped[6:] if stripped.upper().startswith("MEDIA:") else line)
    return "\n".join(lines)


def _mark_fired(path: Path, record: dict) -> None:
    now = datetime.now(TZ).isoformat(timespec="seconds")
    record["fire_count"] = int(record.get("fire_count") or 0) + 1
    record["last_fired_at"] = now
    record["updated_at"] = now
    if record.get("kind") == "once":
        record["status"] = "fired"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str]) -> int:
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except OSError:
        pass
    reminder_id = argv[1].strip().lower() if len(argv) > 1 else ""
    record, path = None, None
    if re.fullmatch(r"[0-9a-f]{12}", reminder_id):
        path = ROOT / f"{reminder_id}.json"
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            record = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            record = None
    else:
        reminder_id = "-"
    text = str(record.get("text") or "").strip() if record else ""
    message = PREFIX + _defang(text) if text else f"⏰ เตือนความจำ (ไม่พบข้อความของรายการ {reminder_id})"
    out = sys.stdout.buffer
    out.write(message.encode("utf-8"))
    out.flush()
    if record is not None and path is not None:
        try:
            _mark_fired(path, record)
        except Exception:
            pass  # the reminder was already printed; bookkeeping must not fail the job
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
