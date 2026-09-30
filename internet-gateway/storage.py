from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class QueueLimitError(RuntimeError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


class GatewayStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    principal TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    web_search INTEGER NOT NULL DEFAULT 0,
                    parent_job_id TEXT,
                    segment_index INTEGER NOT NULL DEFAULT 0,
                    request_hash TEXT NOT NULL,
                    idempotency_key TEXT,
                    webhook_event_id TEXT,
                    notify_target TEXT,
                    status TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    done_reason TEXT,
                    prompt_eval_count INTEGER,
                    eval_count INTEGER,
                    delivery_status TEXT,
                    delivery_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    UNIQUE(principal, idempotency_key),
                    UNIQUE(webhook_event_id)
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                    ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_principal_created
                    ON jobs(principal, created_at);

                CREATE TABLE IF NOT EXISTS sessions (
                    principal TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(principal, session_id)
                );
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)")
            }
            if "web_search" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN web_search INTEGER NOT NULL DEFAULT 0"
                )
            migrations = {
                "parent_job_id": "ALTER TABLE jobs ADD COLUMN parent_job_id TEXT",
                "segment_index": (
                    "ALTER TABLE jobs ADD COLUMN segment_index INTEGER NOT NULL DEFAULT 0"
                ),
                "done_reason": "ALTER TABLE jobs ADD COLUMN done_reason TEXT",
                "prompt_eval_count": (
                    "ALTER TABLE jobs ADD COLUMN prompt_eval_count INTEGER"
                ),
                "eval_count": "ALTER TABLE jobs ADD COLUMN eval_count INTEGER",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs(parent_job_id, segment_index)"
            )
            connection.execute(
                "UPDATE jobs SET status='queued', started_at=NULL, updated_at=? WHERE status='running'",
                (time.time(),),
            )

    @staticmethod
    def _job(row: sqlite3.Row | None, include_prompt: bool = False) -> dict[str, Any] | None:
        if row is None:
            return None
        keys = {
            "id",
            "principal",
            "source_type",
            "source_id",
            "session_id",
            "web_search",
            "parent_job_id",
            "segment_index",
            "status",
            "result",
            "error",
            "done_reason",
            "prompt_eval_count",
            "eval_count",
            "delivery_status",
            "delivery_error",
            "created_at",
            "updated_at",
            "started_at",
            "finished_at",
        }
        if include_prompt:
            keys.add("prompt")
            keys.add("notify_target")
        result = {key: row[key] for key in keys}
        result["web_search"] = bool(result["web_search"])
        return result

    def enqueue(
        self,
        *,
        principal: str,
        source_type: str,
        source_id: str,
        session_id: str,
        prompt: str,
        web_search: bool,
        idempotency_key: str | None,
        webhook_event_id: str | None,
        notify_target: str | None,
        max_active: int,
        max_per_hour: int,
        parent_job_id: str | None = None,
        segment_index: int = 0,
    ) -> tuple[dict[str, Any], bool]:
        now = time.time()
        request_values = {
            "principal": principal,
            "session_id": session_id,
            "prompt": prompt,
            "web_search": bool(web_search),
            "parent_job_id": parent_job_id,
            "segment_index": int(segment_index),
        }
        request_hash = hashlib.sha256(
            json.dumps(request_values, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        # Releases before web-search support used the same hash without this field.
        legacy_request_hash = hashlib.sha256(
            json.dumps(
                {
                    "principal": principal,
                    "session_id": session_id,
                    "prompt": prompt,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if webhook_event_id:
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE webhook_event_id=?", (webhook_event_id,)
                ).fetchone()
                if existing:
                    connection.commit()
                    return self._job(existing) or {}, False
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE principal=? AND idempotency_key=?",
                    (principal, idempotency_key),
                ).fetchone()
                if existing:
                    legacy_match = (
                        not web_search and existing["request_hash"] == legacy_request_hash
                    )
                    if existing["request_hash"] != request_hash and not legacy_match:
                        raise IdempotencyConflictError(
                            "Idempotency-Key was already used with a different request"
                        )
                    if legacy_match:
                        connection.execute(
                            "UPDATE jobs SET request_hash=? WHERE id=?",
                            (request_hash, existing["id"]),
                        )
                    connection.commit()
                    return self._job(existing) or {}, False

            active = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE principal=? AND status IN ('queued','running')",
                (principal,),
            ).fetchone()[0]
            hourly = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE principal=? AND created_at>=?",
                (principal, now - 3600),
            ).fetchone()[0]
            if active >= max_active:
                raise QueueLimitError("Too many active jobs for this sender")
            if hourly >= max_per_hour:
                raise QueueLimitError("Hourly prompt limit reached")

            job_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO jobs (
                    id, principal, source_type, source_id, session_id, prompt, web_search,
                    parent_job_id, segment_index,
                    request_hash, idempotency_key, webhook_event_id, notify_target,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    job_id,
                    principal,
                    source_type,
                    source_id,
                    session_id,
                    prompt,
                    int(bool(web_search)),
                    parent_job_id,
                    max(0, int(segment_index)),
                    request_hash,
                    idempotency_key,
                    webhook_event_id,
                    notify_target,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            connection.commit()
            return self._job(row) or {}, True

    def claim_next(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            now = time.time()
            connection.execute(
                "UPDATE jobs SET status='running', started_at=?, updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            connection.commit()
            return self._job(row, include_prompt=True)

    def complete(
        self,
        job_id: str,
        result: str,
        *,
        done_reason: str | None = None,
        prompt_eval_count: int | None = None,
        eval_count: int | None = None,
    ) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status='completed', result=?, error=NULL,
                    done_reason=?, prompt_eval_count=?, eval_count=?,
                    updated_at=?, finished_at=? WHERE id=?
                """,
                (
                    result,
                    done_reason,
                    prompt_eval_count,
                    eval_count,
                    now,
                    now,
                    job_id,
                ),
            )

    def fail(self, job_id: str, error: str) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status='failed', error=?, updated_at=?, finished_at=?
                WHERE id=?
                """,
                (error[:2000], now, now, job_id),
            )

    def set_delivery(self, job_id: str, status: str, error: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET delivery_status=?, delivery_error=?, updated_at=? WHERE id=?",
                (status, error[:2000] or None, time.time(), job_id),
            )

    def get_job(self, job_id: str, principal: str | None = None) -> dict[str, Any] | None:
        with self._connect() as connection:
            if principal:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id=? AND principal=?", (job_id, principal)
                ).fetchone()
            else:
                row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self._job(row)

    def latest_for_principal(self, principal: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE principal=? ORDER BY created_at DESC LIMIT 1",
                (principal,),
            ).fetchone()
            return self._job(row)

    def queue_position(self, job_id: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, created_at FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row or row["status"] != "queued":
                return None
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='queued' AND created_at<=?",
                    (row["created_at"],),
                ).fetchone()[0]
            )

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            result = {"queued": 0, "running": 0}
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs WHERE status IN ('queued','running') GROUP BY status"
            ):
                result[row["status"]] = int(row["count"])
            return result

    def load_history(self, principal: str, session_id: str) -> list[dict[str, str]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT messages_json FROM sessions WHERE principal=? AND session_id=?",
                (principal, session_id),
            ).fetchone()
            if not row:
                return []
            value = json.loads(row["messages_json"])
            return value if isinstance(value, list) else []

    def append_turn(
        self,
        principal: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
        max_messages: int,
    ) -> None:
        history = self.load_history(principal, session_id)
        history.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
        )
        history = history[-max(2, max_messages) :]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions(principal, session_id, messages_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(principal, session_id) DO UPDATE SET
                    messages_json=excluded.messages_json,
                    updated_at=excluded.updated_at
                """,
                (principal, session_id, json.dumps(history, ensure_ascii=False), time.time()),
            )

    def clear_session(self, principal: str, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE principal=? AND session_id=?",
                (principal, session_id),
            )

    def prune(self, retention_days: int, session_retention_days: int | None = None) -> None:
        cutoff = time.time() - max(1, retention_days) * 86400
        session_cutoff = time.time() - max(
            1, session_retention_days if session_retention_days is not None else retention_days
        ) * 86400
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM jobs WHERE created_at<? AND status IN ('completed','failed')",
                (cutoff,),
            )
            connection.execute(
                "DELETE FROM sessions WHERE updated_at<?",
                (session_cutoff,),
            )
