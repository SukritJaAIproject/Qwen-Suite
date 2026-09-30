from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal


Decision = Literal["allow", "ask", "deny"]


PERMISSION_ROOT = Path(
    os.environ.get("QWEN_PERMISSION_ROOT", r"E:\localllm\data\permission-center")
)

CATEGORIES: dict[str, dict[str, str]] = {
    "project_read": {
        "name": "Project read",
        "description": "Read project files, memory, task status, and project context.",
    },
    "computer_read": {
        "name": "Computer read",
        "description": "Read files and computer information outside an isolated project.",
    },
    "memory_write": {
        "name": "Memory write",
        "description": "Store durable memory for one project.",
    },
    "project_write": {
        "name": "Project write",
        "description": "Create or change project documents, attachments, and configured paths.",
    },
    "computer_write": {
        "name": "Computer write",
        "description": "Create or overwrite files outside project storage.",
    },
    "system_execute": {
        "name": "Run programs",
        "description": "Run commands or launch local applications.",
    },
    "task_control": {
        "name": "Task control",
        "description": "Start or stop monitored background tasks.",
    },
    "media_write": {
        "name": "Media editing",
        "description": "Create or overwrite video and audio outputs.",
    },
}

DEFAULT_POLICIES: dict[str, dict[str, Decision]] = {
    "webui": {
        "project_read": "allow",
        "computer_read": "allow",
        "memory_write": "allow",
        "project_write": "allow",
        "computer_write": "allow",
        "system_execute": "allow",
        "task_control": "allow",
        "media_write": "allow",
    },
    "openclaw": {
        "project_read": "allow",
        "computer_read": "allow",
        "memory_write": "allow",
        "project_write": "allow",
        "computer_write": "allow",
        "system_execute": "allow",
        "task_control": "allow",
        "media_write": "allow",
    },
}

ACTION_CATEGORIES: dict[str, str] = {
    "computer_overview": "computer_read",
    "list_files": "computer_read",
    "read_text_file": "computer_read",
    "list_projects": "project_read",
    "list_project_paths": "project_read",
    "project_list_files": "project_read",
    "project_read_text_file": "project_read",
    "inspect_project_document": "project_read",
    "get_project_context": "project_read",
    "recall_project_memory": "project_read",
    "list_project_tasks": "project_read",
    "get_project_task": "project_read",
    "list_video_jobs": "project_read",
    "get_video_job": "project_read",
    "probe_media": "computer_read",
    "remember_project": "memory_write",
    "create_project": "project_write",
    "configure_project_path": "project_write",
    "remove_project_path": "project_write",
    "project_write_text_file": "project_write",
    "attach_file_to_project": "project_write",
    "write_project_document": "project_write",
    "write_text_file": "computer_write",
    "run_command": "system_execute",
    "project_run_command": "system_execute",
    "launch_program": "system_execute",
    "start_project_task": "task_control",
    "stop_project_task": "task_control",
    "edit_video": "media_write",
    "assemble_video": "media_write",
    "generate_video": "media_write",
    "retry_video_job": "media_write",
    # Processing unit for the local LLM (added 2026-09-29): reading the list is computer_read,
    # changing it restarts Ollama when idle, like any other command -> system_execute.
    "list_compute_devices": "computer_read",
    "compute_device": "system_execute",
    # Added 2026-09-30: web search (read-only, network) and document creation (a new file in a
    # project's outputs folder, never an overwrite) - both under existing allow categories.
    "web_search": "computer_read",
    "make_document": "project_write",
    # Added 2026-09-30 (Codex-parity items). Every entry reuses an existing category, so no new
    # policy rows are needed and each tool is allowed/denied exactly like its older siblings.
    # Partial file edits with history: edits/reverts write files like write_text_file /
    # project_write_text_file; the change list is a read; opening VS Code launches a GUI program.
    "edit_text_file": "computer_write",
    "project_edit_text_file": "project_write",
    "revert_file_change": "computer_write",
    "list_file_changes": "computer_read",
    "open_in_editor": "system_execute",
    # Inbox of finished background tasks (listing = project_read, flags = task_control).
    "inbox_list": "project_read",
    "inbox_mark": "task_control",
    # Whole-suite health report (read-only; statuses and counts only).
    "suite_status": "computer_read",
    # Reminders from chat. The OpenClaw automation's fixed argv only prints the stored text to the
    # owner's Telegram; creating/cancelling is task_control, listing is a read.
    "schedule_reminder": "task_control",
    "list_reminders": "project_read",
    "cancel_reminder": "task_control",
    # Documents/PDF/charts: new files only in a project's outputs folder; edit_document in_place
    # backs the original up first (file_history).
    "pdf_tools": "project_write",
    "pdf_render_page": "project_read",
    "pdf_list_form_fields": "project_read",
    "edit_document": "project_write",
    "render_document_preview": "project_read",
    "save_template": "project_write",
    "list_templates": "project_read",
    "make_chart": "project_write",
    # Saved one-click project actions and AGENTS.md generation. Running an action is a command run;
    # a background one is also checked against task_control in gateway_dispatch (strictest wins).
    "configure_project_action": "project_write",
    "list_project_actions": "project_read",
    "run_project_action": "system_execute",
    "init_agents_md": "project_write",
    # Read-only list of the desktop-launched Qwen Codex jobs (logs\codex-bridge\jobs.jsonl).
    "list_codex_jobs": "project_read",
    # Deep research job (background task) and its status (read).
    "start_research": "task_control",
    "get_research": "project_read",
}

SENSITIVE_WORDS = ("password", "token", "secret", "cookie", "authorization", "api_key", "apikey")


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _safe_value(value: Any, key: str = "") -> Any:
    lowered = key.casefold()
    if any(word in lowered for word in SENSITIVE_WORDS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _safe_value(v, str(k)) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return [_safe_value(item) for item in value[:50]]
    if isinstance(value, str):
        if len(value) > 500:
            return value[:500] + "... [truncated]"
        return value
    return value


class PermissionManager:
    def __init__(self, root: Path = PERMISSION_ROOT) -> None:
        self.root = root
        self.policy_path = root / "policies.json"
        self.scoped_policy_path = root / "scoped-policies.json"
        self.pending_path = root / "pending.json"
        self.audit_path = root / "audit.jsonl"
        self._lock = threading.RLock()
        root.mkdir(parents=True, exist_ok=True)
        if not self.policy_path.exists():
            _atomic_json(self.policy_path, DEFAULT_POLICIES)
        if not self.pending_path.exists():
            _atomic_json(self.pending_path, [])
        if not self.scoped_policy_path.exists():
            _atomic_json(self.scoped_policy_path, [])

    def _policies(self) -> dict[str, dict[str, Decision]]:
        try:
            raw = json.loads(self.policy_path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        policies: dict[str, dict[str, Decision]] = {}
        for source, defaults in DEFAULT_POLICIES.items():
            supplied = raw.get(source) if isinstance(raw.get(source), dict) else {}
            policies[source] = {
                category: supplied.get(category, decision)
                if supplied.get(category, decision) in {"allow", "ask", "deny"}
                else decision
                for category, decision in defaults.items()
            }
        return policies

    def _pending(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.pending_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, list) else []
        except Exception:
            return []

    def _scoped_policies(self) -> list[dict[str, str]]:
        try:
            raw = json.loads(self.scoped_policy_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return []
            return [row for row in raw if isinstance(row, dict)]
        except Exception:
            return []

    def get_policy(
        self,
        source: str,
        category: str,
        principal: str = "",
        project: str = "",
    ) -> Decision:
        normalized_source = source if source in DEFAULT_POLICIES else "webui"
        principal = principal.strip().casefold()
        project = project.strip().casefold()
        best: tuple[int, Decision] | None = None
        for row in self._scoped_policies():
            if row.get("source") != normalized_source or row.get("category") != category:
                continue
            row_principal = str(row.get("principal") or "").casefold()
            row_project = str(row.get("project") or "").casefold()
            if row_principal and row_principal != principal:
                continue
            if row_project and row_project != project:
                continue
            decision = str(row.get("decision") or "")
            if decision not in {"allow", "ask", "deny"}:
                continue
            score = (2 if row_principal else 0) + (1 if row_project else 0)
            if best is None or score > best[0]:
                best = (score, decision)  # type: ignore[assignment]
        if best is not None:
            return best[1]
        return self._policies()[normalized_source].get(category, "deny")

    def update_scoped_policy(
        self,
        source: str,
        category: str,
        decision: str,
        principal: str = "",
        project: str = "",
    ) -> dict[str, Any]:
        if source not in DEFAULT_POLICIES:
            raise ValueError(f"Unknown source: {source}")
        if category not in CATEGORIES:
            raise ValueError(f"Unknown permission category: {category}")
        if decision not in {"allow", "ask", "deny", "inherit"}:
            raise ValueError("Decision must be allow, ask, deny, or inherit.")
        principal = principal.strip().casefold()[:120]
        project = project.strip().casefold()[:120]
        if not principal and not project:
            raise ValueError("A principal/account or project scope is required.")
        with self._lock:
            rows = self._scoped_policies()
            rows = [
                row for row in rows
                if not (
                    row.get("source") == source
                    and row.get("category") == category
                    and str(row.get("principal") or "").casefold() == principal
                    and str(row.get("project") or "").casefold() == project
                )
            ]
            if decision != "inherit":
                rows.append(
                    {
                        "source": source,
                        "principal": principal,
                        "project": project,
                        "category": category,
                        "decision": decision,
                        "updated_at": _now_iso(),
                    }
                )
            _atomic_json(self.scoped_policy_path, rows[-1000:])
            self.audit(
                source,
                "policy.scope.update",
                decision,
                {"category": category},
                principal=principal,
                project=project,
            )
        return {"source": source, "principal": principal, "project": project, "category": category, "decision": decision}

    def update_policy(self, source: str, category: str, decision: str) -> dict[str, Any]:
        if source not in DEFAULT_POLICIES:
            raise ValueError(f"Unknown source: {source}")
        if category not in CATEGORIES:
            raise ValueError(f"Unknown permission category: {category}")
        if decision not in {"allow", "ask", "deny"}:
            raise ValueError("Decision must be allow, ask, or deny.")
        with self._lock:
            policies = self._policies()
            policies[source][category] = decision  # type: ignore[assignment]
            _atomic_json(self.policy_path, policies)
            self.audit(source, "policy.update", decision, {"category": category})
        return {"source": source, "category": category, "decision": decision}

    def cleanup(self) -> None:
        with self._lock:
            changed = False
            now = _now()
            rows = self._pending()
            for row in rows:
                if row.get("status") != "pending":
                    continue
                try:
                    expires = datetime.fromisoformat(str(row["expires_at"]))
                except Exception:
                    expires = now - timedelta(seconds=1)
                if expires < now:
                    row["status"] = "expired"
                    row["decided_at"] = _now_iso()
                    changed = True
            if changed:
                _atomic_json(self.pending_path, rows[-500:])

    def request(
        self,
        source: str,
        action: str,
        category: str,
        arguments: dict[str, Any],
        preview: Any = None,
        principal: str = "",
        project: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            self.cleanup()
            rows = self._pending()
            safe_arguments = _safe_value(arguments)
            fingerprint = json.dumps(
                {"source": source, "principal": principal, "project": project, "action": action, "arguments": safe_arguments},
                ensure_ascii=False,
                sort_keys=True,
            )
            for row in reversed(rows):
                if row.get("status") == "pending" and row.get("fingerprint") == fingerprint:
                    return row
            created = _now()
            row = {
                "id": secrets.token_urlsafe(12),
                "source": source,
                "principal": principal,
                "project": project,
                "action": action,
                "category": category,
                "arguments": safe_arguments,
                "preview": _safe_value(preview),
                "fingerprint": fingerprint,
                "status": "pending",
                "created_at": created.isoformat(timespec="seconds"),
                "expires_at": (created + timedelta(minutes=15)).isoformat(timespec="seconds"),
            }
            rows.append(row)
            _atomic_json(self.pending_path, rows[-500:])
            self.audit(source, action, "approval_required", {"approval_id": row["id"], "category": category}, principal=principal, project=project)
            return row

    def decide(self, approval_id: str, decision: Literal["approved", "denied"]) -> dict[str, Any]:
        if decision not in {"approved", "denied"}:
            raise ValueError("Decision must be approved or denied.")
        with self._lock:
            self.cleanup()
            rows = self._pending()
            match = next((row for row in rows if row.get("id") == approval_id), None)
            if not match:
                raise FileNotFoundError(f"Approval request not found: {approval_id}")
            if match.get("status") != "pending":
                return match
            match["status"] = decision
            match["decided_at"] = _now_iso()
            _atomic_json(self.pending_path, rows[-500:])
            self.audit(str(match.get("source", "unknown")), str(match.get("action", "unknown")), decision, {"approval_id": approval_id}, principal=str(match.get("principal") or ""), project=str(match.get("project") or ""))
            return match

    def consume(
        self,
        approval_id: str,
        source: str,
        action: str,
        arguments: dict[str, Any] | None = None,
        principal: str = "",
        project: str = "",
    ) -> bool:
        if not approval_id:
            return False
        with self._lock:
            self.cleanup()
            rows = self._pending()
            match = next((row for row in rows if row.get("id") == approval_id), None)
            if not match or match.get("status") != "approved":
                return False
            if match.get("source") != source or match.get("action") != action:
                return False
            if str(match.get("principal") or "") != principal or str(match.get("project") or "") != project:
                return False
            if arguments is not None:
                fingerprint = json.dumps(
                    {"source": source, "principal": principal, "project": project, "action": action, "arguments": _safe_value(arguments)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if match.get("fingerprint") != fingerprint:
                    return False
            match["status"] = "used"
            match["used_at"] = _now_iso()
            _atomic_json(self.pending_path, rows[-500:])
            return True

    def audit(
        self,
        source: str,
        action: str,
        result: str,
        details: dict[str, Any] | None = None,
        *,
        principal: str = "",
        project: str = "",
    ) -> None:
        row = {
            "timestamp": _now_iso(),
            "source": source,
            "principal": principal,
            "project": project,
            "action": action,
            "result": result,
            "details": _safe_value(details or {}),
        }
        with self._lock:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def audit_rows(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        lines = self.audit_path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(limit, 1000)) :]
        rows = []
        for line in reversed(lines):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def state(self) -> dict[str, Any]:
        self.cleanup()
        pending = self._pending()
        return {
            "categories": CATEGORIES,
            "policies": self._policies(),
            "scoped_policies": list(reversed(self._scoped_policies())),
            "pending": [row for row in reversed(pending) if row.get("status") == "pending"],
            "recent_requests": list(reversed(pending[-50:])),
            "audit": self.audit_rows(100),
            "root": str(self.root),
        }
