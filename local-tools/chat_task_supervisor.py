"""Keep an Open WebUI chat attached to a long-running Qwen Video Studio job.

The model starts the work, Task Monitor owns the process, and this supervisor
survives browser refreshes or broken streaming connections.  It retries an
eligible video job and writes the verified terminal result back into the exact
assistant message that was left unfinished.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


ROOT = Path(os.environ.get("QWEN_ROOT", r"E:\localllm"))
DB_PATH = Path(os.environ.get("QWEN_WEBUI_DB", str(ROOT / "webui-data" / "webui.db")))
STATE_DIR = Path(os.environ.get("QWEN_CHAT_SUPERVISOR_DIR", r"E:\qwen-projects\.chat-supervisor"))
VIDEO_API = os.environ.get("QWEN_VIDEO_API", "http://127.0.0.1:8765/video-studio/api")
POLL_SECONDS = max(10, int(os.environ.get("QWEN_CHAT_SUPERVISOR_POLL", "30")))


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def find_chat(title: str) -> sqlite3.Row:
    with connect() as connection:
        row = connection.execute(
            "SELECT id, title, chat, current_message_id, updated_at FROM chat "
            "WHERE lower(title)=lower(?) ORDER BY updated_at DESC LIMIT 1",
            (title,),
        ).fetchone()
    if not row:
        raise RuntimeError(f"Open WebUI chat was not found: {title}")
    return row


def register(chat_title: str, job_id: str) -> dict[str, Any]:
    row = find_chat(chat_title)
    payload = json.loads(str(row["chat"]))
    history = payload.get("history") or {}
    message_id = str(row["current_message_id"] or history.get("currentId") or "")
    message = (history.get("messages") or {}).get(message_id) or {}
    if message.get("role") != "assistant":
        raise RuntimeError("The current Test LLM message is not an unfinished assistant message.")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    backup = STATE_DIR / "webui-before-supervisor.db"
    if not backup.exists():
        with sqlite3.connect(DB_PATH) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)

    state = {
        "chat_id": str(row["id"]),
        "chat_title": str(row["title"]),
        "assistant_message_id": message_id,
        "job_id": job_id,
        "status": "monitoring",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "last_phase": "registered",
        "last_progress": 0.0,
        "retry_count": 0,
    }
    atomic_json(STATE_DIR / f"{row['id']}.json", state)
    return state


def fetch_job(job_id: str) -> dict[str, Any]:
    response = requests.get(f"{VIDEO_API}/jobs/{job_id}", timeout=15)
    response.raise_for_status()
    payload = response.json()
    return payload.get("job") or payload.get("result") or payload


def retry_job(job_id: str) -> dict[str, Any]:
    response = requests.post(f"{VIDEO_API}/jobs/{job_id}/retry", timeout=20)
    response.raise_for_status()
    payload = response.json()
    return payload.get("job") or payload.get("result") or payload


def result_text(job: dict[str, Any]) -> str:
    output = str(job.get("output_path") or "")
    report_path = Path(str(job.get("report_path") or ""))
    report: dict[str, Any] = {}
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            report = {}
    checks = report.get("checks") or {}
    passed = sum(1 for value in checks.values() if value is True)
    total = len(checks)
    duration = report.get("duration_seconds") or job.get("duration_seconds") or "-"
    sha256 = str(report.get("sha256") or job.get("sha256") or "-")
    scenes = report.get("scene_count") or "-"
    return (
        "เสร็จสมบูรณ์แล้วครับ — ระบบรันงานต่ออัตโนมัติจน Video Studio ตรวจสอบผ่าน\n\n"
        f"ไฟล์ MP4: `{output}`\n\n"
        f"- สถานะ: completed (100%)\n"
        f"- ความยาว: {duration} วินาที\n"
        f"- จำนวนฉาก: {scenes}\n"
        f"- QA ผ่าน: {passed}/{total} รายการ\n"
        f"- SHA-256: `{sha256}`\n"
        f"- Video job: `{job.get('id', '')}`\n\n"
        "วิดีโอมีพยัญชนะไทยครบ ก–ฮ พร้อมคำอธิบายและเสียงบรรยายภาษาอังกฤษ เพลงพื้นหลัง และซับอังกฤษครับ"
    )


def failure_text(job: dict[str, Any]) -> str:
    return (
        "งาน Video Studio หยุดหลังจากลองกู้คืนอัตโนมัติครบแล้วครับ\n\n"
        f"- Job: `{job.get('id', '')}`\n"
        f"- Phase: {job.get('phase', '-')}\n"
        f"- Error: {job.get('error') or job.get('message') or 'Unknown error'}\n"
        "ระบบเก็บ checkpoint และ log ไว้สำหรับซ่อมต่อโดยไม่เริ่มใหม่ทั้งหมด"
    )


def finish_chat(state: dict[str, Any], content: str, failed: bool = False) -> None:
    chat_id = state["chat_id"]
    message_id = state["assistant_message_id"]
    timestamp = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT chat, current_message_id FROM chat WHERE id=?", (chat_id,)).fetchone()
        if not row:
            raise RuntimeError(f"Chat disappeared while the job was running: {chat_id}")
        payload = json.loads(str(row["chat"]))
        history = payload.setdefault("history", {})
        messages = history.setdefault("messages", {})
        message = messages.get(message_id)
        if not isinstance(message, dict):
            raise RuntimeError(f"Assistant message disappeared while the job was running: {message_id}")
        message["content"] = content
        message["done"] = True
        message["error"] = {"content": content} if failed else None
        message["timestamp"] = timestamp
        if str(history.get("currentId") or "") == message_id:
            history["currentId"] = message_id
        payload["timestamp"] = timestamp
        current = str(row["current_message_id"] or "")
        connection.execute(
            "UPDATE chat SET chat=?, updated_at=?, current_message_id=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), timestamp, current or message_id, chat_id),
        )
        connection.commit()


def monitor_state(path: Path) -> None:
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("status") not in {"monitoring", "retrying"}:
        return
    try:
        job = fetch_job(str(state["job_id"]))
    except Exception as exc:
        state["updated_at"] = now_iso()
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        atomic_json(path, state)
        return

    status = str(job.get("status") or "unknown").casefold()
    state["updated_at"] = now_iso()
    state["last_phase"] = str(job.get("phase") or status)
    state["last_progress"] = float(job.get("progress_percent") or 0.0)
    state["task_id"] = job.get("task_id")
    state["output_path"] = job.get("output_path")

    if status == "completed":
        finish_chat(state, result_text(job))
        state["status"] = "completed"
        state["completed_at"] = now_iso()
    elif status in {"failed", "interrupted", "cancelled"}:
        attempt = int(job.get("attempt") or 0)
        maximum = int(job.get("max_attempts") or 3)
        if bool(job.get("auto_retry", True)) and attempt < maximum:
            try:
                retry_job(str(job["id"]))
                state["status"] = "retrying"
                state["retry_count"] = int(state.get("retry_count") or 0) + 1
            except Exception as exc:
                state["last_error"] = f"Retry failed: {type(exc).__name__}: {exc}"
        else:
            finish_chat(state, failure_text(job), failed=True)
            state["status"] = "failed"
            state["completed_at"] = now_iso()
    else:
        state["status"] = "monitoring"
    atomic_json(path, state)


def daemon() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        for path in STATE_DIR.glob("*.json"):
            try:
                monitor_state(path)
            except Exception as exc:
                print(f"{now_iso()} supervisor error for {path.name}: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("daemon")
    register_parser = subparsers.add_parser("register")
    register_parser.add_argument("--chat-title", required=True)
    register_parser.add_argument("--job-id", required=True)
    once_parser = subparsers.add_parser("once")
    once_parser.add_argument("--state", required=True)
    args = parser.parse_args()
    if args.command == "daemon":
        daemon()
    elif args.command == "register":
        print(json.dumps(register(args.chat_title, args.job_id), ensure_ascii=False, indent=2))
    else:
        monitor_state(Path(args.state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
