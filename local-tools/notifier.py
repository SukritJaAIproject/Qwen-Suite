"""Task-end notifications: a Windows toast and/or one Telegram message to the owner.

The Telegram recipient is always the owner listed in openclaw.json
(commands.ownerAllowFrom "telegram:<id>"), read at send time. It is never taken
from tool arguments and never written to the log or returned to callers.
Only a title and a status line are sent (plus an optional media file chosen by
code, never by the model).
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


INSTALL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = INSTALL_ROOT / "data" / "notify.json"
LOG_PATH = INSTALL_ROOT / "data" / "notifier.log"
OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
# node.exe + openclaw.mjs directly, not openclaw.cmd: cmd.exe would re-parse the
# message text and a title containing & | ^ % could run commands.
OPENCLAW_NODE = INSTALL_ROOT / "openclaw-runtime" / "node.exe"
OPENCLAW_ENTRY = INSTALL_ROOT / "openclaw-runtime" / "node_modules" / "openclaw" / "openclaw.mjs"
# OpenClaw only attaches local files from its own roots; its temp root is one of them.
MEDIA_STAGING = Path(tempfile.gettempdir()) / "openclaw" / "qwen-notify"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "telegram": True,
    "toast": True,
    "min_runtime_seconds": 60,
    "video_media": True,
}
CHANNELS = ("telegram", "toast")
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_TELEGRAM_PER_HOUR = 20
TELEGRAM_TIMEOUT_SECONDS = 90
TELEGRAM_MEDIA_TIMEOUT_SECONDS = 240
TOAST_TIMEOUT_SECONDS = 30
MAX_LOG_BYTES = 512 * 1024

_SEND_LOCK = threading.Lock()
_RATE_LOCK = threading.Lock()
_TELEGRAM_SENT: deque[float] = deque()

STATUS_TEXT = {
    "completed": "{label}เสร็จแล้ว: {title}{minutes}",
    "failed": "{label}ล้มเหลว: {title}{minutes}{lookup}",
    "timed_out": "{label}หมดเวลา: {title}{minutes}{lookup}",
    "interrupted": "{label}หยุดโดยไม่ทราบผล: {title}{minutes}{lookup}",
    "cancelled": "{label}ถูกยกเลิก: {title}{minutes}",
}

TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$heading = [Security.SecurityElement]::Escape([string]$env:QWEN_TOAST_TITLE)
$body = [Security.SecurityElement]::Escape([string]$env:QWEN_TOAST_BODY)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$heading</text><text>$body</text></binding></visual></toast>")
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
if ($env:QWEN_TOAST_DRYRUN -ne '1') {
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:QWEN_TOAST_APPID).Show($toast)
}
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<owner>")
    return text


def _log(event: dict[str, Any], secrets: Iterable[str] = ()) -> None:
    """Append one JSON line; the owner id is redacted from every value before writing."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            os.replace(LOG_PATH, LOG_PATH.with_suffix(LOG_PATH.suffix + ".1"))
        line = json.dumps({"ts": _now_iso(), **event}, ensure_ascii=False, default=str)
        line = _redact(line, [*secrets, owner_telegram_target()])
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def coerce_flag(value: Any) -> bool | None:
    """Gateway arguments arrive as raw JSON, so "false" must not count as true."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"", "none", "null", "auto", "default"}:
        return None
    if text in {"1", "true", "yes", "on", "ใช่", "เปิด"}:
        return True
    if text in {"0", "false", "no", "off", "ไม่", "ปิด"}:
        return False
    raise ValueError("notify ต้องเป็น true, false หรือเว้นว่างไว้")


def resolve_channels(notify: Any, source: str = "") -> list[str]:
    """Explicit true/false wins; otherwise pick by where the request came from.

    openclaw (Telegram/WhatsApp chat): the owner is probably away from the PC -> Telegram + toast.
    webui / direct MCP: the owner is at the PC -> toast only.
    internal callers (no source): nothing, the behaviour from before notifications existed.
    """
    flag = coerce_flag(notify)
    if flag is True:
        return ["telegram", "toast"]
    if flag is False:
        return []
    normalized = str(source or "").strip().casefold()
    if normalized == "openclaw":
        return ["telegram", "toast"]
    if normalized in {"webui", "mcp", "web"}:
        return ["toast"]
    return []


def load_config() -> dict[str, Any]:
    """Read notify.json, creating it with defaults when absent; bad values fall back to defaults."""
    config = dict(DEFAULT_CONFIG)
    try:
        if not CONFIG_PATH.exists():
            _atomic_json(CONFIG_PATH, DEFAULT_CONFIG)
            return config
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return config
    if not isinstance(raw, dict):
        return config
    for key, default in DEFAULT_CONFIG.items():
        value = raw.get(key)
        if isinstance(default, bool):
            if isinstance(value, bool):
                config[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            config[key] = value
    return config


def owner_telegram_target() -> str:
    """First numeric 'telegram:<id>' entry of commands.ownerAllowFrom, or '' when there is none."""
    try:
        data = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return ""
    commands = data.get("commands") if isinstance(data, dict) else None
    entries = commands.get("ownerAllowFrom") if isinstance(commands, dict) else None
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        text = str(entry).strip()
        if text.casefold().startswith("telegram:"):
            candidate = text.split(":", 1)[1].strip()
            if re.fullmatch(r"-?\d{3,20}", candidate):
                return candidate
    return ""


def _clean_title(title: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(title or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 120:
        text = text[:119].rstrip() + "…"
    return text or "งานเบื้องหลัง"


def format_message(
    title: str,
    status: str,
    minutes: float | None,
    *,
    task_id: str = "",
    label: str = "งาน",
    lookup: str = "get_project_task",
) -> str:
    status = str(status or "").strip().casefold()
    template = STATUS_TEXT.get(status, "{label}จบแล้ว (" + (status or "ไม่ทราบสถานะ") + "): {title}{minutes}")
    minutes_text = ""
    if minutes is not None:
        try:
            minutes_text = f" ({max(1, round(float(minutes)))} นาที)"
        except (TypeError, ValueError):
            minutes_text = ""
    lookup_text = ""
    clean_id = re.sub(r"[^0-9A-Za-z._-]", "", str(task_id or ""))[:80]
    if clean_id and lookup:
        lookup_text = f" — ดูรายละเอียดด้วย {lookup} {clean_id}"
    return template.format(label=label, title=_clean_title(title), minutes=minutes_text, lookup=lookup_text)


def _run(argv: list[str], timeout: int, env: dict[str, str] | None = None) -> tuple[int | None, str]:
    """Run without a console window; returns (exit code or None on timeout/spawn error, combined output)."""
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        # str(TimeoutExpired) repeats argv, which holds the owner id; report only the fact.
        return None, "timeout"
    except OSError as exc:
        return None, f"spawn error: {type(exc).__name__}"
    output = (completed.stdout or b"") + b"\n" + (completed.stderr or b"")
    return completed.returncode, output.decode("utf-8", errors="replace").strip()


def _rate_ok() -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        while _TELEGRAM_SENT and now - _TELEGRAM_SENT[0] > 3600:
            _TELEGRAM_SENT.popleft()
        if len(_TELEGRAM_SENT) >= MAX_TELEGRAM_PER_HOUR:
            return False
        _TELEGRAM_SENT.append(now)
        return True


def _stage_media(media_path: str) -> Path | None:
    """Copy a media file into OpenClaw's temp root so the CLI is allowed to attach it."""
    try:
        source = Path(media_path)
        if not source.is_file() or source.stat().st_size > MAX_MEDIA_BYTES:
            return None
        MEDIA_STAGING.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 86_400
        for stale in MEDIA_STAGING.glob("*"):
            try:
                if stale.is_file() and stale.stat().st_mtime < cutoff:
                    stale.unlink()
            except OSError:
                continue
        safe_name = re.sub(r"[^0-9A-Za-zก-๙._-]+", "_", source.name)[-80:] or "media"
        staged = MEDIA_STAGING / f"{uuid.uuid4().hex[:8]}-{safe_name}"
        shutil.copyfile(source, staged)
        return staged
    except OSError:
        return None


def _send_telegram(text: str, media_path: str, dry_run: bool) -> dict[str, Any]:
    target = owner_telegram_target()
    if not target:
        return {"ok": False, "result": "no_owner"}
    if not OPENCLAW_NODE.is_file() or not OPENCLAW_ENTRY.is_file():
        return {"ok": False, "result": "openclaw_missing"}
    if not dry_run and not _rate_ok():
        return {"ok": False, "result": "rate_limited"}
    base = [
        str(OPENCLAW_NODE), str(OPENCLAW_ENTRY), "message", "send",
        "--channel", "telegram", "--target", target, "-m", text, "--json",
    ]
    if dry_run:
        base.append("--dry-run")
    media: Path | None = None
    if media_path:
        # A dry run never copies files; it only shows which file would be attached.
        media = Path(media_path) if dry_run else _stage_media(media_path)
    try:
        if media is not None:
            code, output = _run([*base, "--media", str(media)], TELEGRAM_MEDIA_TIMEOUT_SECONDS)
            if code == 0:
                return {"ok": True, "result": "dry_run" if dry_run else "sent", "media": True}
            # The text still matters more than the attachment: retry without it.
            first_error = _redact(output, [target])[-300:]
            code, output = _run(base, TELEGRAM_TIMEOUT_SECONDS)
            if code == 0:
                return {"ok": True, "result": "sent_without_media", "media_error": first_error}
        else:
            code, output = _run(base, TELEGRAM_TIMEOUT_SECONDS)
            if code == 0:
                return {"ok": True, "result": "dry_run" if dry_run else "sent"}
        return {"ok": False, "result": "timeout" if code is None and output == "timeout" else "error",
                "exit_code": code, "error": _redact(output, [target])[-300:]}
    finally:
        if media is not None and not dry_run:
            try:
                media.unlink()
            except OSError:
                pass


def _send_toast(heading: str, body: str, dry_run: bool) -> dict[str, Any]:
    if not POWERSHELL.is_file():
        return {"ok": False, "result": "powershell_missing"}
    encoded = base64.b64encode(TOAST_SCRIPT.encode("utf-16-le")).decode("ascii")
    env = dict(os.environ)
    # Text travels in environment variables so no title can change the script itself.
    env.update({
        "QWEN_TOAST_TITLE": heading,
        "QWEN_TOAST_BODY": body,
        "QWEN_TOAST_APPID": TOAST_APP_ID,
        "QWEN_TOAST_DRYRUN": "1" if dry_run else "0",
    })
    code, output = _run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        TOAST_TIMEOUT_SECONDS,
        env=env,
    )
    if code == 0:
        return {"ok": True, "result": "dry_run" if dry_run else "shown"}
    return {"ok": False, "result": "timeout" if code is None and output == "timeout" else "error",
            "exit_code": code, "error": output[-300:]}


def notify(
    title: str,
    status: str,
    minutes: float | None,
    media_path: str = "",
    dry_run: bool = False,
    *,
    task_id: str = "",
    channels: Iterable[str] | None = None,
    label: str = "งาน",
    lookup: str = "get_project_task",
) -> dict[str, Any]:
    """Send one end-of-task notification. Never raises; returns what happened per channel."""
    text = ""
    try:
        config = load_config()
        wanted = [c for c in (CHANNELS if channels is None else channels) if c in CHANNELS]
        active = [c for c in dict.fromkeys(wanted) if config.get(c)]
        text = format_message(title, status, minutes, task_id=task_id, label=label, lookup=lookup)
        skip_reason = ""
        if not config.get("enabled"):
            skip_reason = "disabled"
        elif minutes is not None and float(minutes) * 60 < float(config.get("min_runtime_seconds") or 0):
            skip_reason = "short_task"
        elif not active:
            skip_reason = "no_channel"
        if skip_reason:
            result: dict[str, Any] = {"status": "skipped", "reason": skip_reason, "text": text, "channels": {}}
            _log({"task_id": task_id, "status": status, "result": "skipped", "reason": skip_reason})
            return result

        outcomes: dict[str, dict[str, Any]] = {}
        with _SEND_LOCK:
            if "toast" in active:
                heading, _, rest = text.partition(": ")
                outcomes["toast"] = _send_toast(f"Qwen Suite — {heading}", rest or text, dry_run)
            if "telegram" in active:
                outcomes["telegram"] = _send_telegram(text, str(media_path or ""), dry_run)
        successes = sum(1 for outcome in outcomes.values() if outcome.get("ok"))
        if successes == len(outcomes):
            overall = "dry_run" if dry_run else "sent"
        elif successes:
            overall = "partial"
        else:
            overall = "failed"
        _log({
            "task_id": task_id,
            "status": status,
            "result": overall,
            "dry_run": dry_run,
            "title": _clean_title(title)[:80],
            "channels": {name: {k: v for k, v in outcome.items() if k != "ok"} for name, outcome in outcomes.items()},
        })
        return {"status": overall, "text": text, "channels": {name: outcome.get("result") for name, outcome in outcomes.items()}}
    except Exception as exc:  # notifications must never break the task monitor
        _log({"task_id": task_id, "status": status, "result": "failed", "error": type(exc).__name__})
        return {"status": "failed", "reason": type(exc).__name__, "text": text, "channels": {}}


def notify_in_background(
    kwargs: dict[str, Any],
    on_done: Callable[[dict[str, Any]], None] | None = None,
) -> threading.Thread:
    """Run notify(**kwargs) on a daemon thread; the Telegram CLI takes ~10 s to start."""

    def _worker() -> None:
        result = notify(**kwargs)
        if on_done is not None:
            try:
                on_done(result)
            except Exception:
                pass

    thread = threading.Thread(target=_worker, name="qwen-notify", daemon=True)
    thread.start()
    return thread


def notify_owner(
    heading: str,
    text: str,
    *,
    channels: Iterable[str] | None = None,
    dry_run: bool = False,
    tag: str = "",
) -> dict[str, Any]:
    """Send one short notice to the owner (toast heading + text; Telegram text). Never raises.

    For events that are not task ends, e.g. account notices from account_recovery.py. The same
    notify.json switches, owner lookup, rate limit and log as notify(); the text is written by code.
    """
    try:
        config = load_config()
        wanted = [c for c in (CHANNELS if channels is None else channels) if c in CHANNELS]
        active = [c for c in dict.fromkeys(wanted) if config.get(c)]
        heading = _clean_title(heading)[:80]
        body = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]+", " ", str(text or "")).strip()[:600]
        if not config.get("enabled") or not active:
            reason = "disabled" if not config.get("enabled") else "no_channel"
            _log({"task_id": tag, "status": "notice", "result": "skipped", "reason": reason})
            return {"status": "skipped", "reason": reason, "channels": {}}
        outcomes: dict[str, dict[str, Any]] = {}
        with _SEND_LOCK:
            if "toast" in active:
                outcomes["toast"] = _send_toast(f"Qwen Suite — {heading}", body, dry_run)
            if "telegram" in active:
                outcomes["telegram"] = _send_telegram(f"{heading}: {body}", "", dry_run)
        successes = sum(1 for outcome in outcomes.values() if outcome.get("ok"))
        overall = ("dry_run" if dry_run else "sent") if successes == len(outcomes) else ("partial" if successes else "failed")
        _log({
            "task_id": tag,
            "status": "notice",
            "result": overall,
            "dry_run": dry_run,
            "title": heading,
            "channels": {name: {k: v for k, v in outcome.items() if k != "ok"} for name, outcome in outcomes.items()},
        })
        return {"status": overall, "channels": {name: outcome.get("result") for name, outcome in outcomes.items()}}
    except Exception as exc:  # a notice must never break its caller
        _log({"task_id": tag, "status": "notice", "result": "failed", "error": type(exc).__name__})
        return {"status": "failed", "reason": type(exc).__name__, "channels": {}}


# --- Video Studio jobs ---------------------------------------------------------

def video_job_is_final(job: dict[str, Any]) -> bool:
    """True when a video job reached a state the watchdog will not retry any more."""
    status = str(job.get("status") or "")
    if status == "completed":
        return True
    if status in {"failed", "cancelled"}:
        if not job.get("auto_retry", True):
            return True
        return int(job.get("attempt") or 1) >= int(job.get("max_attempts") or 3)
    return False


def _job_minutes(job: dict[str, Any]) -> float | None:
    try:
        start = datetime.fromisoformat(str(job.get("created_at"))).timestamp()
        end = datetime.fromisoformat(str(job.get("finished_at") or job.get("updated_at"))).timestamp()
        return max(0.0, end - start) / 60
    except Exception:
        return None


def video_notification(job: dict[str, Any]) -> dict[str, Any] | None:
    """notify() kwargs for a final video job, or None when the job asked for no notification."""
    channels = [c for c in job.get("notify_channels") or [] if c in CHANNELS]
    if not channels or not video_job_is_final(job):
        return None
    status = str(job.get("status") or "")
    media = ""
    if status == "completed" and load_config().get("video_media"):
        output = Path(str(job.get("output_path") or ""))
        if output.suffix.casefold() == ".mp4" and output.is_file() and output.stat().st_size <= MAX_MEDIA_BYTES:
            media = str(output)
        else:
            for preview in job.get("previews") or []:
                if Path(str(preview)).is_file():
                    media = str(preview)
                    break
    return {
        "title": str(job.get("title") or "วิดีโอ"),
        "status": status,
        "minutes": _job_minutes(job),
        "media_path": media,
        "task_id": str(job.get("id") or ""),
        "channels": channels,
        "label": "วิดีโอ",
        "lookup": "get_video_job",
    }


def notify_video_job_in_background(job: dict[str, Any]) -> threading.Thread | None:
    kwargs = video_notification(job)
    if kwargs is None:
        return None
    return notify_in_background(kwargs)
