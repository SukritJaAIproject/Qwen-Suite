from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from task_monitor import FINISHED_STATES, TaskMonitor


VIDEO_ROOT = Path(os.environ.get("QWEN_VIDEO_ROOT", r"E:\qwen-projects\.video-studio"))
JOBS_ROOT = VIDEO_ROOT / "jobs"
WORKER = Path(__file__).with_name("video_worker.py")
ASPECTS = {"9:16", "16:9", "1:1"}
STYLES = {"premium", "education", "cinematic", "corporate", "playful", "minimal"}
_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _safe_name(value: str, fallback: str = "qwen-video") -> str:
    cleaned = re.sub(r"[^0-9A-Za-zก-๙._-]+", "_", value.strip()).strip("._-")
    return (cleaned[:90] or fallback).rstrip(".")


class VideoOrchestrator:
    """Starts resumable one-command video builds and exposes their real state."""

    def __init__(self, tasks: TaskMonitor) -> None:
        self.tasks = tasks
        JOBS_ROOT.mkdir(parents=True, exist_ok=True)
        self._watchdog = threading.Thread(
            target=self._watchdog_loop,
            name="qwen-video-watchdog",
            daemon=True,
        )
        self._watchdog.start()

    @staticmethod
    def _age_seconds(value: object) -> float:
        try:
            return max(0.0, time.time() - datetime.fromisoformat(str(value)).timestamp())
        except Exception:
            return 0.0

    def _watchdog_loop(self) -> None:
        """Recover abandoned workers and retry transient failures without user prompts."""
        while True:
            time.sleep(45)
            try:
                for summary in self.list(include_completed=True, limit=200):
                    self._notify_if_final(summary)
                    if not summary.get("auto_retry", True):
                        continue
                    job_id = str(summary.get("id") or "")
                    attempt = int(summary.get("attempt") or 1)
                    max_attempts = int(summary.get("max_attempts") or 3)
                    task = summary.get("task") or {}
                    task_status = str(task.get("status") or "")
                    status = str(summary.get("status") or "")

                    if status == "running" and task_status in FINISHED_STATES and self._age_seconds(summary.get("updated_at")) > 30:
                        with _LOCK:
                            job = self._read(job_id)
                            if job.get("status") == "running":
                                job["status"] = "failed"
                                job["phase"] = "recovery"
                                job["error"] = f"Video worker ended with task status {task_status} before completion."
                                job["message"] = "Worker stopped unexpectedly; automatic resume is scheduled."
                                job["finished_at"] = _now_iso()
                                job["updated_at"] = _now_iso()
                                _atomic_json(self._job_path(job_id), job)
                        status = "failed"

                    if status == "failed" and attempt < max_attempts and self._age_seconds(summary.get("finished_at")) >= 45:
                        self.retry(job_id)
            except Exception:
                # The foreground API remains available even if one recovery pass fails.
                continue

    def _notify_if_final(self, summary: dict[str, Any]) -> None:
        """Send the one end notification for a job that will not be retried any more."""
        try:
            import notifier

            if summary.get("notified_at") or not summary.get("notify_channels") or not notifier.video_job_is_final(summary):
                return
            job_id = str(summary.get("id") or "")
            with _LOCK:
                job = self._read(job_id)
                if job.get("notified_at") or not notifier.video_job_is_final(job):
                    return
                job["notified_at"] = _now_iso()
                _atomic_json(self._job_path(job_id), job)
            notifier.notify_video_job_in_background(job)
        except Exception:
            # A notification problem must never stop the recovery pass.
            return

    @staticmethod
    def _job_path(job_id: str) -> Path:
        if not re.fullmatch(r"video-[0-9]{8}-[0-9]{6}-[0-9a-f]{7}", job_id):
            raise ValueError(f"Invalid video job ID: {job_id}")
        return JOBS_ROOT / job_id / "job.json"

    def _read(self, job_id: str) -> dict[str, Any]:
        path = self._job_path(job_id)
        if not path.is_file():
            raise FileNotFoundError(f"Video job not found: {job_id}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"Video job is damaged: {job_id}")
        return value

    def _launch(self, job: dict[str, Any]) -> dict[str, Any]:
        if not WORKER.is_file():
            raise FileNotFoundError(f"Video worker is missing: {WORKER}")
        project_root = Path(str(job["project_root"])).resolve()
        job_path = self._job_path(str(job["id"]))
        duration = int(job["duration_seconds"])
        task = self.tasks.start(
            project=str(job["project"]),
            title=f"Qwen Video Studio: {job['title']}",
            program_alias="qwen-video",
            executable=sys.executable,
            args=[str(WORKER), "--job", str(job_path)],
            cwd=project_root,
            path_alias="project",
            expected_seconds=max(60, min(duration * 2, 3600)),
            max_runtime_seconds=max(900, min(duration * 12 + 1800, 86_400)),
        )
        with _LOCK:
            current = self._read(str(job["id"]))
            current["task_id"] = task["id"]
            current["status"] = "running"
            current["phase"] = "planning"
            current["message"] = "Thinking: preparing storyboard and production plan"
            current["updated_at"] = _now_iso()
            _atomic_json(job_path, current)
        return {"status": "started", "job": self.get(str(job["id"])), "task": task}

    def start(
        self,
        *,
        project: str,
        project_root: Path,
        prompt: str,
        duration_seconds: int = 60,
        aspect_ratio: str = "9:16",
        style: str = "premium",
        language: str = "auto",
        voice: str = "auto",
        output_name: str = "",
        title: str = "",
        notify_channels: list[str] | None = None,
    ) -> dict[str, Any]:
        prompt = prompt.strip()
        if len(prompt) < 3:
            raise ValueError("Video prompt must contain at least 3 characters.")
        if len(prompt) > 12_000:
            raise ValueError("Video prompt exceeds the 12,000-character limit.")
        duration_seconds = max(8, min(int(duration_seconds), 3600))
        aspect_ratio = aspect_ratio.strip()
        if aspect_ratio not in ASPECTS:
            raise ValueError(f"aspect_ratio must be one of: {', '.join(sorted(ASPECTS))}")
        style = style.strip().casefold() or "premium"
        if style not in STYLES:
            raise ValueError(f"style must be one of: {', '.join(sorted(STYLES))}")
        language = language.strip().casefold() or "auto"
        if language not in {"auto", "thai", "english"}:
            raise ValueError("language must be auto, thai, or english")

        root = project_root.resolve()
        output_dir = root / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        inferred_title = title.strip() or re.split(r"[\r\n.!?]", prompt, maxsplit=1)[0].strip()
        inferred_title = inferred_title[:100] or "Qwen Video"
        stem = _safe_name(Path(output_name).stem if output_name else inferred_title)
        if not output_name:
            stem = f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        output_path = output_dir / f"{stem}.mp4"

        job_id = f"video-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:7]}"
        job_dir = JOBS_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        job = {
            "id": job_id,
            "project": project,
            "project_root": str(root),
            "title": inferred_title,
            "prompt": prompt,
            "duration_seconds": duration_seconds,
            "aspect_ratio": aspect_ratio,
            "style": style,
            "language": language,
            "voice": voice.strip() or "auto",
            "output_path": str(output_path),
            "work_dir": str(job_dir / "work"),
            "status": "queued",
            "phase": "queued",
            "progress_percent": 0.0,
            "message": "Queued for Video Studio",
            "attempt": 1,
            "auto_retry": True,
            "max_attempts": 3,
            "task_id": None,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "finished_at": None,
            "error": None,
            "report_path": None,
            "previews": [],
            "notify_channels": [str(channel) for channel in (notify_channels or [])],
            "notified_at": None,
        }
        _atomic_json(job_dir / "job.json", job)
        return self._launch(job)

    def get(self, job_id: str) -> dict[str, Any]:
        job = self._read(job_id)
        task_id = str(job.get("task_id") or "")
        task = None
        if task_id:
            try:
                task = self.tasks.get(task_id, include_logs=False)
            except FileNotFoundError:
                task = None
        result = dict(job)
        result.pop("project_root", None)
        result["task"] = task
        if task and task.get("status") in FINISHED_STATES and result.get("status") == "running":
            result["monitor_note"] = "Worker ended; Video Studio is finalizing its persisted state."
        return result

    def list(self, project: str = "", include_completed: bool = True, limit: int = 50) -> list[dict[str, Any]]:
        requested = project.strip().casefold()
        jobs: list[dict[str, Any]] = []
        for path in JOBS_ROOT.glob("video-*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if requested and str(job.get("project", "")).casefold() != requested:
                continue
            if not include_completed and job.get("status") in {"completed", "failed", "cancelled"}:
                continue
            jobs.append(job)
        jobs.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return [self.get(str(item["id"])) for item in jobs[: max(1, min(int(limit), 200))]]

    def retry(self, job_id: str) -> dict[str, Any]:
        with _LOCK:
            job = self._read(job_id)
            if job.get("status") == "completed" and Path(str(job.get("output_path", ""))).is_file():
                return {"status": "already_completed", "job": self.get(job_id)}
            task_id = str(job.get("task_id") or "")
            if task_id:
                try:
                    task = self.tasks.get(task_id, include_logs=False)
                    if task.get("status") == "running":
                        return {"status": "already_running", "job": self.get(job_id)}
                except FileNotFoundError:
                    pass
            job["attempt"] = int(job.get("attempt") or 1) + 1
            job["status"] = "queued"
            job["phase"] = "resume"
            job["progress_percent"] = 0.0
            job["message"] = "Retry queued; valid checkpoints will be reused"
            job["error"] = None
            job["finished_at"] = None
            job["notified_at"] = None  # a manual retry earns its own end notification
            job["updated_at"] = _now_iso()
            _atomic_json(self._job_path(job_id), job)
        return self._launch(job)
