"""Start Codex-Qwen bridge jobs from the local-tools gateway (chat) and report on them.

server.py's start_codex_job / get_codex_job tools call the two functions below.

start_codex_job() never runs Codex or a model itself. It checks the request, writes the prompt
to a file and starts
    powershell -File Invoke-QwenCodex.ps1 -Exec -Detach -JsonOut -JobId <id> -Cwd <folder>
               -PromptFile <file> -Write|-ReadOnly -MaxMinutes <n> -Source <openclaw|webui|mcp>
without waiting for it (CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW, + CREATE_BREAKAWAY_FROM_JOB
when allowed; stdin closed, only its stdout/stderr handle - the launch log - inherited; see
_spawn_detached for why not DETACHED_PROCESS) and returns at once with the job id. That
launcher checks the arguments, starts the hidden job process and exits within seconds.
From there the job is exactly a desktop job: the bridge re-checks everything, waits
for the one-job lock and an idle Ollama, runs the policy hook check, the time limit, unloads the
model and sends the end notification - Telegram + toast when the request came from OpenClaw,
toast when it came from Open WebUI (notifier.resolve_channels, through codex_jobs.notify).

It is launched with subprocess directly, never through run_command, so command_policy (which
blocks any command naming codex-home, and reads the body of a -File script) is not involved;
the chat model cannot pass shell text into this launch - only a prompt (written to a file), a
folder the gateway already resolved, a mode and a number.

get_codex_job() is read-only: codex_jobs.list_jobs (answers masked, hidden when a job touched a
secret folder) plus the tail of the answer, the changed files and the commands the policy hook
blocked; before the bridge has queued the job it reads the launch log instead.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent.parent
BRIDGE_SCRIPT = HERE / "Invoke-QwenCodex.ps1"
POWERSHELL = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")

JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
MAX_PROMPT_CHARS = 20_000
MIN_MINUTES, MAX_MINUTES = 5, 120
TAIL_LINES = 20
TAIL_CHARS = 3000
LAUNCH_GRACE_SECONDS = 120

# Same list as $BlockedParts in Invoke-QwenCodex.ps1 and server.py BLOCKED_PARTS.
BLOCKED_PARTS = {".aws", ".azure", ".gnupg", ".kube", ".ssh", "credentials", "login data", "web data",
                 ".claude", ".codex", ".openclaw", "codex-home"}
SOURCES = {"openclaw", "webui", "mcp"}
CHANNELS_TH = {"openclaw": "Telegram และ toast บนเครื่อง", "webui": "toast บนเครื่อง", "mcp": "toast บนเครื่อง"}

_LAUNCHED: list[subprocess.Popen] = []
DETACHED_PROCESS = 0x00000008  # deliberately NOT used, see _spawn_detached
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


# ------------------------------------------------------------------------------ helpers
def _codex_jobs():
    spec = importlib.util.spec_from_file_location("qwen_codex_jobs_dispatch", HERE / "codex_jobs.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _settings(base: Path) -> dict[str, Any]:
    try:
        data = json.loads((base / "data" / "codex-bridge.json").read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}  # the bridge writes the defaults (write_mode disabled) on its first run


def _normalize_mode(mode: Any) -> str:
    text = str(mode or "write").strip().casefold()
    if text in {"write", "w", "edit", "full", "full-access", "แก้ไข", "แก้ไฟล์"}:
        return "write"
    if text in {"read", "r", "read-only", "readonly", "read_only", "อ่าน", "อ่านอย่างเดียว"}:
        return "read"
    raise ValueError("mode ต้องเป็น write (แก้ไฟล์ได้) หรือ read (อ่านอย่างเดียว)")


def _normalize_source(source: Any) -> str:
    text = str(source or "").strip().casefold()
    return text if text in SOURCES else "mcp"


def _clamp_minutes(value: Any) -> int:
    try:
        minutes = int(float(value))
    except (TypeError, ValueError):
        raise ValueError("max_minutes ต้องเป็นตัวเลขนาที") from None
    return max(MIN_MINUTES, min(MAX_MINUTES, minutes))


def _under(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _home() -> Path:
    return Path.home().resolve()


def check_folder(cwd: Any, base: Path | None = None) -> Path:
    """The job folder, re-checked. server.py already resolved it with its own path rules
    (allowed roots, secret-path denylist); this is the bridge's defence in depth."""
    base = Path(base) if base else DEFAULT_ROOT
    raw = str(cwd or "").strip().strip('"')
    if not raw:
        raise ValueError("ต้องระบุโฟลเดอร์งาน (project หรือ cwd)")
    given = Path(raw)
    if not given.is_absolute():
        raise ValueError(f"โฟลเดอร์งานต้องเป็น path เต็ม: {raw}")
    folder = given.resolve(strict=True)  # FileNotFoundError when missing
    if not folder.is_dir():
        raise ValueError(f"ไม่ใช่โฟลเดอร์: {folder}")
    for candidate in {given, folder}:
        if candidate == Path(candidate.anchor):
            raise ValueError(f"ใช้ทั้งไดรฟ์ ({candidate}) เป็นโฟลเดอร์งานไม่ได้ ให้เลือกโฟลเดอร์งาน")
        if {part.casefold() for part in candidate.parts} & BLOCKED_PARTS:
            raise ValueError(f"โฟลเดอร์นี้เก็บข้อมูลลับหรือประวัติของเอเจนต์ ห้ามใช้เป็นโฟลเดอร์งาน: {candidate}")
        home = _home()
        if candidate == home or _under(candidate, home / "AppData"):
            raise ValueError(f"ห้ามใช้โฟลเดอร์โปรไฟล์หรือ AppData เป็นโฟลเดอร์งาน: {candidate}")
        suite = base.resolve()
        if _under(candidate, suite) or _under(suite, candidate):
            # A full-access job inside the suite could rewrite command_policy.py, the rules or
            # this bridge and switch the protection off for every later job and chat command.
            raise ValueError(f"ห้ามสั่งงาน Codex จากแชตในโฟลเดอร์ของ Qwen Suite ({suite}) - "
                             "ใช้โฟลเดอร์โปรเจกต์ (E:\\qwen-projects\\<ชื่อ>\\work) หรือโฟลเดอร์งานอื่น")
    return folder


def new_job_id() -> str:
    """Same shape as the bridge's New-JobId (yyyyMMdd-HHmmss-xxxx)."""
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def _spawn_detached(argv: list[str], log_path: Path, env: dict[str, str], cwd: Path) -> int:
    """Start argv in its own process group with its own hidden console, independent of this
    server (breaking away from a job object when one allows it); only the launch log handle
    is inherited (close_fds + redirected std handles), stdin is closed.

    Not DETACHED_PROCESS: powershell.exe 5.1 started without any console exits at once with
    code 0 and runs nothing (verified 2026-09-30). CREATE_NO_WINDOW gives it a console of its
    own with no window, which is what Start-Process -WindowStyle Hidden amounts to."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    _LAUNCHED[:] = [p for p in _LAUNCHED if p.poll() is None]  # finished launchers are reaped here
    with log_path.open("wb") as log:
        for extra in (CREATE_BREAKAWAY_FROM_JOB, 0):
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        cwd=str(cwd), env=env, creationflags=flags | extra, close_fds=True, shell=False)
                _LAUNCHED.append(proc)  # never waited on; kept so Popen does not warn when collected
                return proc.pid
            except OSError as exc:
                # A job object that forbids breakaway refuses the flag (access denied): retry without.
                if extra and getattr(exc, "winerror", None) == 5:
                    continue
                raise
    raise OSError("could not start the Codex bridge")


def _launch_env(base: Path) -> dict[str, str]:
    env = dict(os.environ)
    if base.resolve() == DEFAULT_ROOT.resolve():
        # A chat job always runs with the real settings: no test overrides leak in from the server.
        for name in [k for k in env if k.upper().startswith("QWEN_CODEX_")]:
            env.pop(name, None)
    else:
        env["QWEN_CODEX_ROOT"] = str(base)
    return env


# ------------------------------------------------------------------------------- start
def start_codex_job(prompt: str, project: str = "", cwd: str = "", mode: str = "write", max_minutes: int = 30,
                    source: str = "", *, root: str | Path | None = None,
                    spawn: Callable[[list[str], Path, dict[str, str], Path], int] | None = None) -> dict[str, Any]:
    """Queue one Codex-Qwen job and return at once: {job_id, status: 'queued', message_th, ...}.

    cwd must be the folder server.py already resolved (inside its allowed roots); project is the
    project slug, for the record only. mode 'write' needs write_mode full-access or
    windows-sandbox in data\\codex-bridge.json, else the call is refused before anything starts.
    """
    base = Path(root) if root else DEFAULT_ROOT
    text = str(prompt or "").strip()
    if not text:
        raise ValueError("ต้องมีคำสั่งงาน (prompt) บอกว่าจะให้ Codex ทำอะไร")
    if len(text) > MAX_PROMPT_CHARS:
        raise ValueError(f"คำสั่งงานยาวเกิน {MAX_PROMPT_CHARS} ตัวอักษร ให้สรุปให้สั้นลง")
    job_mode = _normalize_mode(mode)
    folder = check_folder(cwd, base)
    minutes = _clamp_minutes(max_minutes)
    src = _normalize_source(source)
    settings = _settings(base)
    write_mode = str(settings.get("write_mode") or "disabled")
    if job_mode == "write" and write_mode not in {"full-access", "windows-sandbox"}:
        return {
            "status": "refused",
            "reason": "write_mode_disabled",
            "message_th": ("ยังไม่เปิดโหมดแก้ไขไฟล์ของ Qwen Codex (write_mode ใน E:\\localllm\\data\\codex-bridge.json "
                           f"เป็น {write_mode}) - สั่งแบบ mode=read ได้ หรือให้เจ้าของเครื่องเปิดก่อน"),
        }
    try:
        queue_ahead = int(_codex_jobs().list_jobs(base, limit=50).get("running_count") or 0)
    except Exception:
        queue_ahead = 0

    job_id = new_job_id()
    log_dir = base / "logs" / "codex-bridge"
    pending = log_dir / "pending"
    dispatch_dir = log_dir / "dispatch"
    pending.mkdir(parents=True, exist_ok=True)
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = pending / f"{job_id}.prompt.md"
    prompt_file.write_text(text + "\n", encoding="utf-8")
    launch_log = dispatch_dir / f"{job_id}.launch.log"
    argv = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(BRIDGE_SCRIPT),
            "-Exec", "-Detach", "-JsonOut", "-JobId", job_id, "-Cwd", str(folder), "-PromptFile", str(prompt_file),
            "-Write" if job_mode == "write" else "-ReadOnly", "-MaxMinutes", str(minutes), "-Source", src]
    record: dict[str, Any] = {
        "id": job_id, "source": src, "mode": job_mode, "cwd": str(folder), "project": str(project or ""),
        "max_minutes": minutes, "requested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "launch_log": str(launch_log), "write_mode": write_mode,
    }
    record_path = dispatch_dir / f"{job_id}.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        pid = (spawn or _spawn_detached)(argv, launch_log, _launch_env(base), base)
    except Exception as exc:
        for leftover in (prompt_file, record_path):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise RuntimeError(f"เริ่ม Qwen Codex ไม่ได้: {type(exc).__name__}: {exc}") from None
    record["launcher_pid"] = pid
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")

    idle_minutes = int(settings.get("idle_wait_minutes") or 60)
    recent = int(settings.get("recent_activity_minutes") or 15)
    notify_on = settings.get("notify", True) is not False
    mode_th = "แก้ไขไฟล์ได้ (สิทธิ์ผู้ใช้เต็ม มีนโยบายคำสั่งกั้น)" if job_mode == "write" else "อ่านอย่างเดียว"
    if write_mode == "windows-sandbox" and job_mode == "write":
        mode_th = "แก้ไขไฟล์ได้ใน Windows sandbox ของ Codex"
    queue_th = f" มีงาน Codex อื่นอยู่ในคิว {queue_ahead} งาน จะเริ่มต่อจากนั้น" if queue_ahead else ""
    notify_th = f"เสร็จแล้วจะแจ้งทาง {CHANNELS_TH[src]}" if notify_on else "ไม่มีการแจ้งเตือน (ปิดไว้ใน codex-bridge.json)"
    return {
        "status": "queued",
        "job_id": job_id,
        "mode": job_mode,
        "cwd": str(folder),
        "project": str(project or ""),
        "max_minutes": minutes,
        "queue_ahead": queue_ahead,
        "message_th": (f"รับงาน Qwen Codex แล้ว รหัส {job_id} ({mode_th}) ในโฟลเดอร์ {folder}.{queue_th} "
                       f"งานจะเริ่มเมื่อ Ollama ว่าง (ไม่มีการใช้โมเดลแชต {recent} นาที รอได้สูงสุด {idle_minutes} นาที) "
                       f"แล้วทำงานไม่เกิน {minutes} นาที (บน CPU งานหนึ่งมัก 10-30 นาที) {notify_th} "
                       f"ดูสถานะด้วย get_codex_job {job_id}"),
        "next_action": ("Tell the user the job_id and end the turn. The owner is notified when the job ends; "
                        "do NOT poll get_codex_job, call it only when the user asks."),
    }


# --------------------------------------------------------------------------------- get
def _oem_codepage() -> str:
    try:
        import ctypes

        name = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        "".encode(name)
        return name
    except Exception:
        return "cp874"


def _read_launch(record: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    path = Path(str(record.get("launch_log") or ""))
    try:
        raw = path.read_bytes()[:20_000]
    except OSError:
        return None, ""
    try:
        text = raw.decode("utf-8-sig")  # -JsonOut switches the launcher's console to UTF-8
    except UnicodeDecodeError:
        text = raw.decode(_oem_codepage(), errors="replace")  # e.g. a PowerShell error before -JsonOut took effect
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if line.startswith("{"):
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    return value, text
            except ValueError:
                continue
    return None, text


def _pid_running(pid: Any) -> bool:
    try:
        import psutil
    except ImportError:
        return False
    try:
        return psutil.pid_exists(int(pid)) and psutil.Process(int(pid)).is_running()
    except Exception:
        return False


def _not_queued(base: Path, job_id: str, cj: Any) -> dict[str, Any]:
    """The bridge has not written the job yet: starting, or the launch failed."""
    record_path = base / "logs" / "codex-bridge" / "dispatch" / f"{job_id}.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8-sig"))
    except Exception:
        raise FileNotFoundError(f"ไม่พบงาน Codex รหัส {job_id}") from None
    launch, raw = _read_launch(record)
    base_info = {"job_id": job_id, "mode": record.get("mode"), "cwd": record.get("cwd"), "source": record.get("source"),
                 "requested_at": record.get("requested_at")}
    if launch and launch.get("status") == "error":
        error = cj.redact_secrets(launch.get("error"))
        return {**base_info, "status": "failed_to_start", "exit_code": launch.get("exit_code"), "error": error,
                "message_th": f"งาน {job_id} ไม่ได้เริ่ม: {error}"}
    try:
        age = time.time() - datetime.fromisoformat(str(record.get("requested_at"))).timestamp()
    except Exception:
        age = 0.0
    if age > LAUNCH_GRACE_SECONDS and not _pid_running(record.get("launcher_pid")) and not launch:
        tail = cj.redact_secrets(raw.strip()[-300:]) if raw.strip() else ""
        return {**base_info, "status": "failed_to_start", "error": tail or "ตัวเริ่มงานจบโดยไม่ได้สร้างงาน",
                "message_th": f"งาน {job_id} ไม่ได้เริ่ม ดูรายละเอียดใน E:\\localllm\\logs\\codex-bridge\\bridge.log"}
    return {**base_info, "status": "starting", "message_th": f"งาน {job_id} กำลังเริ่ม (bridge ยังตรวจค่าอยู่)"}


def _answer_tail(cj: Any, run_dir: Any, hidden: bool, lines: int) -> str:
    if not run_dir:
        return ""
    path = Path(str(run_dir)) / "last.md"
    try:
        text = path.read_bytes()[-65_536:].decode("utf-8-sig", errors="replace")
    except OSError:
        return ""
    tail = "\n".join(text.strip().splitlines()[-max(1, lines):])
    return cj._safe_answer(tail, TAIL_CHARS, hidden)


def _progress(cj: Any, run_dir: Any, hidden: bool) -> dict[str, Any]:
    """What a running job has done so far (from its event stream)."""
    if not run_dir:
        return {}
    events = Path(str(run_dir)) / "events.jsonl"
    if not events.exists():
        return {}
    summary = cj.summarize_events(events)
    hidden = hidden or bool(summary.get("touched_sensitive_paths"))
    return {
        "turns": summary.get("turns"),
        "commands_total": summary.get("commands_total"),
        "commands_declined": summary.get("commands_declined"),
        "last_commands": [{"command": cj._clip(cj.redact_secrets(c.get("command")), 200), "status": c.get("status")}
                          for c in summary.get("last_commands") or [] if isinstance(c, dict)][-3:],
        "latest_message": cj._safe_answer(summary.get("final_message"), 800, hidden),
    }


STATUS_TH = {
    "queued": "รอคิว/รอให้ Ollama ว่าง",
    "running": "กำลังทำงาน",
    "completed": "เสร็จแล้ว",
    "failed": "ไม่สำเร็จ",
    "interrupted": "หยุดกลางคันโดยไม่ทราบผล (โปรเซสหายไป)",
}


def get_codex_job(job_id: str, *, root: str | Path | None = None, tail_lines: int = TAIL_LINES) -> dict[str, Any]:
    """Status of one bridge job for chat: status, minutes, the last lines of its answer (masked;
    hidden when the job touched a secret folder), changed files, blocked commands."""
    base = Path(root) if root else DEFAULT_ROOT
    jid = str(job_id or "").strip()
    if not JOB_ID_RE.match(jid):
        raise ValueError("รหัสงาน Codex ต้องเป็นรูปแบบ yyyyMMdd-HHmmss-xxxx (จาก start_codex_job หรือ list_codex_jobs)")
    cj = _codex_jobs()
    try:
        job = cj.list_jobs(base, job_id=jid)["job"]
    except FileNotFoundError:
        return _not_queued(base, jid, cj)
    hidden = bool(job.get("answer_hidden"))
    status = str(job.get("status") or "")
    out: dict[str, Any] = {"job_id": jid, "status": status}
    for key in ("kind", "mode", "source", "title", "cwd", "queued_at", "started_at", "finished_at", "minutes",
                "exit_code", "error", "policy_hook", "changed_files", "blocked_commands", "review_file",
                "review_docx", "answer_hidden"):
        if job.get(key) not in (None, "", [], {}):
            out[key] = job[key]
    if status in {"completed", "failed", "interrupted"}:
        tail = _answer_tail(cj, job.get("run_dir"), hidden, tail_lines)
        if not tail:
            tail = str(job.get("final_message") or "")
        if tail:
            out["answer_tail"] = tail
        summary = job.get("summary") if isinstance(job.get("summary"), dict) else {}
        if summary.get("last_commands"):
            out["last_commands"] = [{"command": cj._clip(c.get("command"), 200), "status": c.get("status")}
                                    for c in summary["last_commands"][-3:] if isinstance(c, dict)]
    elif status == "running":
        progress = _progress(cj, job.get("run_dir"), hidden)
        if progress:
            out["progress"] = progress
    minutes = job.get("minutes")
    text = STATUS_TH.get(status, status)
    out["message_th"] = f"งาน Codex {jid}: {text}" + (f" ({minutes} นาที)" if minutes is not None else "")
    if job.get("error"):
        out["message_th"] += f" - {job['error']}"
    return out
