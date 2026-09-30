from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil


TASK_ROOT = Path(r"E:\qwen-projects\.task-monitor")
TASK_FILE = TASK_ROOT / "tasks.json"
LOG_ROOT = TASK_ROOT / "logs"
FINISHED_STATES = {"completed", "failed", "cancelled", "timed_out", "interrupted"}
# Whoever stopped a task already knows it ended, so a stop sends no notification.
NOTIFY_SKIP_STATES = {"cancelled"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _tail_text(path: Path, max_bytes: int = 32_000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    with path.open("rb") as handle:
        size = path.stat().st_size
        handle.seek(max(0, size - max_bytes))
        raw = handle.read(max_bytes)
    return raw.decode("utf-8", errors="replace")


class TaskMonitor:
    def __init__(self) -> None:
        TASK_ROOT.mkdir(parents=True, exist_ok=True)
        LOG_ROOT.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._tasks = self._load()
        self._refresh_all()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not TASK_FILE.exists():
            return {}
        try:
            raw = json.loads(TASK_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(key): value for key, value in raw.items() if isinstance(value, dict)}
        except Exception:
            pass
        return {}

    def _save(self) -> None:
        _write_json(TASK_FILE, self._tasks)

    @staticmethod
    def _resolve_channels(notify: Any, source: str) -> list[str]:
        if notify is None and not source:
            return []  # internal callers (e.g. Video Studio workers) keep the old silent behaviour
        try:
            import notifier
        except Exception:
            return []  # a broken notifier must never stop a task from starting
        return notifier.resolve_channels(notify, source)  # ValueError on a malformed notify value

    @staticmethod
    def _same_process(task: dict[str, Any]) -> bool:
        pid = int(task.get("pid") or 0)
        if pid <= 0 or not psutil.pid_exists(pid):
            return False
        try:
            process = psutil.Process(pid)
            expected = float(task.get("process_create_time") or 0)
            return not expected or abs(process.create_time() - expected) < 1.0
        except (psutil.Error, ValueError, TypeError):
            return False

    @staticmethod
    def _elapsed_seconds(task: dict[str, Any]) -> float:
        try:
            started = datetime.fromisoformat(str(task["started_at"])).timestamp()
            if task.get("finished_at"):
                end = datetime.fromisoformat(str(task["finished_at"])).timestamp()
            else:
                end = time.time()
            return max(0.0, end - started)
        except Exception:
            return 0.0

    def _progress(self, task: dict[str, Any]) -> dict[str, Any] | None:
        stderr = _tail_text(Path(str(task.get("stderr_path", ""))), 64_000)
        stdout = _tail_text(Path(str(task.get("stdout_path", ""))), 32_000)
        expected = float(task.get("expected_seconds") or 0)
        media_times = re.findall(r"time=(\d{1,3}):(\d{2}):(\d{2}(?:\.\d+)?)", stderr)
        if expected > 0 and media_times:
            hours, minutes, seconds = media_times[-1]
            current = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            percent = min(99.5, max(0.0, current / expected * 100))
            if task.get("status") == "completed":
                percent = 100.0
            return {"percent": round(percent, 1), "current_seconds": round(current, 2), "expected_seconds": expected}

        percentages = re.findall(
            r"(?<![\d.])(100(?:\.0+)?|[1-9]?\d(?:\.\d+)?)\s*%",
            f"{stdout}\n{stderr}",
        )
        if percentages:
            percent = min(100.0, max(0.0, float(percentages[-1])))
            return {"percent": round(percent, 1), "source": "log"}
        if task.get("status") == "completed":
            return {"percent": 100.0, "source": "status"}
        return None

    def _public_task(self, task: dict[str, Any], include_logs: bool = False) -> dict[str, Any]:
        result = dict(task)
        result["elapsed_seconds"] = round(self._elapsed_seconds(task), 1)
        result["progress"] = self._progress(task)
        if include_logs:
            result["stdout"] = _tail_text(Path(str(task.get("stdout_path", ""))))
            result["stderr"] = _tail_text(Path(str(task.get("stderr_path", ""))))
        result.pop("process_create_time", None)
        return result

    def _refresh_task(self, task_id: str) -> bool:
        task = self._tasks[task_id]
        if task.get("status") != "running":
            return False
        process = self._processes.get(task_id)
        if process is not None:
            code = process.poll()
            if code is None:
                return False
            task["exit_code"] = code
            task["status"] = "completed" if code == 0 else "failed"
            task["finished_at"] = task.get("finished_at") or _now_iso()
            self._processes.pop(task_id, None)
            self._finalize(task)
            return True
        if self._same_process(task):
            return False
        task["status"] = "interrupted"
        task["finished_at"] = task.get("finished_at") or _now_iso()
        task["message"] = "Process ended while the monitor service was unavailable; exit code is unknown."
        # Also catches tasks that ended while local-tools was restarting.
        self._finalize(task)
        return True

    def _finalize(self, task: dict[str, Any]) -> None:
        """Run once when a task turns terminal: send its end notification.

        Called with the lock held from _watch, _refresh_task and stop(); idempotent through
        notified_at, which records when the send/skip decision was made (see notify_result).
        The send runs on a background thread because the Telegram CLI needs ~10 s to start.
        """
        if task.get("status") not in FINISHED_STATES or task.get("notified_at"):
            return
        if "notify_channels" not in task:
            return  # registry entry from before notifications existed
        task["notified_at"] = _now_iso()
        channels = [str(channel) for channel in task.get("notify_channels") or []]
        if not channels:
            task["notify_result"] = "off"
            return
        if task.get("status") in NOTIFY_SKIP_STATES:
            task["notify_result"] = f"skipped: {task.get('status')}"
            return
        try:
            import notifier
        except Exception as exc:
            task["notify_result"] = f"error: {type(exc).__name__}"
            return
        task_id = str(task.get("id", ""))
        media = str(task.get("notify_media_path") or "") if task.get("status") == "completed" else ""
        task["notify_result"] = "pending"

        def _record(result: dict[str, Any]) -> None:
            with self._lock:
                current = self._tasks.get(task_id)
                if current is not None:
                    current["notify_result"] = str(result.get("status") or "unknown")
                    self._save()

        notifier.notify_in_background(
            {
                "title": str(task.get("title") or ""),
                "status": str(task.get("status")),
                "minutes": self._elapsed_seconds(task) / 60,
                "media_path": media,
                "task_id": task_id,
                "channels": channels,
            },
            on_done=_record,
        )

    def _refresh_all(self) -> None:
        with self._lock:
            changed = False
            for task_id in list(self._tasks):
                changed = self._refresh_task(task_id) or changed
            if changed:
                self._save()

    def _terminate_tree(self, task: dict[str, Any]) -> None:
        pid = int(task.get("pid") or 0)
        if pid <= 0 or not self._same_process(task):
            return
        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                child.terminate()
            parent.terminate()
            _, alive = psutil.wait_procs([*children, parent], timeout=4)
            for process in alive:
                process.kill()
        except psutil.NoSuchProcess:
            return

    def _watch(self, task_id: str, max_runtime_seconds: int) -> None:
        with self._lock:
            process = self._processes.get(task_id)
        if process is None:
            return
        try:
            exit_code = process.wait(timeout=max_runtime_seconds)
            with self._lock:
                task = self._tasks.get(task_id)
                if task and task.get("status") == "running":
                    task["exit_code"] = exit_code
                    task["status"] = "completed" if exit_code == 0 else "failed"
                    task["finished_at"] = _now_iso()
                    self._finalize(task)
                    self._save()
        except subprocess.TimeoutExpired:
            with self._lock:
                task = self._tasks.get(task_id)
                if task and task.get("status") == "running":
                    self._terminate_tree(task)
                    task["status"] = "timed_out"
                    task["finished_at"] = _now_iso()
                    task["message"] = f"Stopped after reaching the {max_runtime_seconds}-second runtime limit."
                    self._finalize(task)
                    self._save()
        finally:
            with self._lock:
                self._processes.pop(task_id, None)

    def start(
        self,
        *,
        project: str,
        title: str,
        program_alias: str,
        executable: str,
        args: list[str],
        cwd: Path,
        path_alias: str,
        expected_seconds: float = 0,
        max_runtime_seconds: int = 3600,
        notify: bool | None = None,
        source: str = "",
        notify_media_path: str = "",
    ) -> dict[str, Any]:
        """notify: True/False forces the end notification on/off; None decides from source
        (openclaw -> Telegram + toast, webui/mcp -> toast, no source -> none, as before)."""
        title = title.strip() or f"{program_alias} task"
        max_runtime_seconds = max(10, min(int(max_runtime_seconds), 86_400))
        expected_seconds = max(0.0, min(float(expected_seconds), 86_400.0))
        source = str(source or "").strip().casefold()[:40]
        notify_channels = self._resolve_channels(notify, source)
        task_id = f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:7]}"
        stdout_path = LOG_ROOT / f"{task_id}.stdout.log"
        stderr_path = LOG_ROOT / f"{task_id}.stderr.log"
        argv = [executable, *[str(arg) for arg in args]]

        with stdout_path.open("ab") as stdout_handle, stderr_path.open("ab") as stderr_handle:
            process = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        try:
            process_create_time = psutil.Process(process.pid).create_time()
        except psutil.Error:
            process_create_time = 0

        task = {
            "id": task_id,
            "project": project,
            "title": title[:160],
            "program": program_alias,
            "command": subprocess.list2cmdline(argv),
            "cwd": str(cwd),
            "path_alias": path_alias,
            "pid": process.pid,
            "process_create_time": process_create_time,
            "status": "running",
            "started_at": _now_iso(),
            "finished_at": None,
            "exit_code": None,
            "expected_seconds": expected_seconds,
            "max_runtime_seconds": max_runtime_seconds,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "source": source,
            "notify": bool(notify_channels),
            "notify_channels": notify_channels,
            "notify_media_path": str(notify_media_path or ""),
            "notified_at": None,
            "read_at": None,
            "archived": False,
        }
        with self._lock:
            self._tasks[task_id] = task
            self._processes[task_id] = process
            self._save()
        watcher = threading.Thread(
            target=self._watch,
            args=(task_id, max_runtime_seconds),
            name=f"qwen-{task_id}",
            daemon=True,
        )
        watcher.start()
        return self._public_task(task)

    def list(self, project: str = "", include_completed: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        self._refresh_all()
        requested = project.strip().casefold()
        limit = max(1, min(int(limit), 500))
        with self._lock:
            tasks = list(self._tasks.values())
            if requested:
                tasks = [task for task in tasks if str(task.get("project", "")).casefold() == requested]
            if not include_completed:
                tasks = [task for task in tasks if task.get("status") == "running"]
            tasks.sort(key=lambda task: str(task.get("started_at", "")), reverse=True)
            return [self._public_task(task) for task in tasks[:limit]]

    def get(self, task_id: str, include_logs: bool = True) -> dict[str, Any]:
        self._refresh_all()
        with self._lock:
            if task_id not in self._tasks:
                raise FileNotFoundError(f"Task not found: {task_id}")
            return self._public_task(self._tasks[task_id], include_logs=include_logs)

    def stop(self, task_id: str) -> dict[str, Any]:
        self._refresh_all()
        with self._lock:
            if task_id not in self._tasks:
                raise FileNotFoundError(f"Task not found: {task_id}")
            task = self._tasks[task_id]
            if task.get("status") != "running":
                return self._public_task(task)
            self._terminate_tree(task)
            task["status"] = "cancelled"
            task["finished_at"] = _now_iso()
            task["message"] = "Stopped by user request."
            self._processes.pop(task_id, None)
            self._finalize(task)
            self._save()
            return self._public_task(task)

    # --- Inbox: finished tasks with read/archived flags --------------------------------

    @staticmethod
    def _is_unread(task: dict[str, Any]) -> bool:
        # Entries from before the inbox existed have no read_at key and count as read,
        # so deploying the inbox does not flood it with old tasks.
        return task.get("status") in FINISHED_STATES and "read_at" in task and not task.get("read_at")

    @staticmethod
    def _summary_line(task: dict[str, Any]) -> str:
        """Last non-empty log line: stdout for a success, stderr (then stdout) otherwise."""
        paths = [task.get("stdout_path")] if task.get("status") == "completed" else [task.get("stderr_path"), task.get("stdout_path")]
        for raw_path in paths:
            if not raw_path:
                continue
            for line in reversed(_tail_text(Path(str(raw_path)), 4_000).splitlines()):
                line = line.strip()
                if line:
                    return line[:200]
        return str(task.get("message") or "")[:200]

    def _inbox_item(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": task.get("id"),
            "project": task.get("project"),
            "title": task.get("title"),
            "program": task.get("program"),
            "status": task.get("status"),
            "source": task.get("source", ""),
            "started_at": task.get("started_at"),
            "finished_at": task.get("finished_at"),
            "elapsed_seconds": round(self._elapsed_seconds(task), 1),
            "exit_code": task.get("exit_code"),
            "message": task.get("message"),
            "summary": self._summary_line(task),
            "unread": self._is_unread(task),
            "read_at": task.get("read_at"),
            "archived": bool(task.get("archived", False)),
            "notified_at": task.get("notified_at"),
            "notify_result": task.get("notify_result"),
        }

    def _finished_task(self, task_id: str) -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        if task_id not in self._tasks:
            raise FileNotFoundError(f"Task not found: {task_id}")
        task = self._tasks[task_id]
        if task.get("status") not in FINISHED_STATES:
            raise ValueError(f"งาน {task_id} ยังไม่จบ จึงยังไม่อยู่ในกล่องงานเสร็จ")
        return task

    def list_inbox(
        self,
        limit: int = 50,
        *,
        project: str = "",
        unread_only: bool = False,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """Finished tasks, newest first, with unread/archived flags and a one-line summary."""
        self._refresh_all()
        requested = str(project or "").strip().casefold()
        limit = max(1, min(int(limit), 500))
        with self._lock:
            finished = [task for task in self._tasks.values() if task.get("status") in FINISHED_STATES]
            if requested:
                finished = [task for task in finished if str(task.get("project", "")).casefold() == requested]
            unread_count = sum(1 for task in finished if self._is_unread(task) and not task.get("archived"))
            visible = [task for task in finished if include_archived or not task.get("archived")]
            if unread_only:
                visible = [task for task in visible if self._is_unread(task)]
            visible.sort(key=lambda task: str(task.get("finished_at") or task.get("started_at") or ""), reverse=True)
            return {
                "unread_count": unread_count,
                "total": len(visible),
                "items": [self._inbox_item(task) for task in visible[:limit]],
            }

    def mark_read(self, task_id: str, read: bool = True) -> dict[str, Any]:
        with self._lock:
            task = self._finished_task(task_id)
            task["read_at"] = (task.get("read_at") or _now_iso()) if read else None
            self._save()
            return self._inbox_item(task)

    def archive(self, task_id: str, archived: bool = True) -> dict[str, Any]:
        with self._lock:
            task = self._finished_task(task_id)
            task["archived"] = bool(archived)
            task["archived_at"] = _now_iso() if archived else None
            if archived and not task.get("read_at"):
                task["read_at"] = _now_iso()
            self._save()
            return self._inbox_item(task)

    def mark_all_read(self, project: str = "", archive: bool = False) -> int:
        """Mark every unread finished task (optionally one project's) read; archive them too if asked."""
        requested = str(project or "").strip().casefold()
        changed = 0
        with self._lock:
            now = _now_iso()
            for task in self._tasks.values():
                if task.get("status") not in FINISHED_STATES:
                    continue
                if requested and str(task.get("project", "")).casefold() != requested:
                    continue
                touched = False
                if self._is_unread(task):
                    task["read_at"] = now
                    touched = True
                if archive and not task.get("archived"):
                    task["archived"] = True
                    task["archived_at"] = now
                    task.setdefault("read_at", now)
                    touched = True
                changed += touched
            if changed:
                self._save()
        return changed
