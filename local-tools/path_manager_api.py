"""Back-end helpers for the Path Manager tabs added 2026-09-30.

- Recent changes: file_history.list_changes (with a fallback that merges its index.jsonl);
  "diff" opens VS Code through file_edit.open_in_editor on a read-only copy of the backup, and
  "revert" goes through the same function as the chat tool revert_file_change.
- Finished-work inbox: TaskMonitor.list_inbox / mark_read / archive / mark_all_read, with a
  plain list of finished tasks when the running TaskMonitor predates the inbox.
- Permission check for the page's own buttons.

These modules were written in parallel, so every call is made defensively (only the keyword
arguments a function accepts are passed). server.py keeps its routes thin and passes its own
guards in (_resolved_path, the VS Code path, the tool functions), so a button click obeys the
same checks as a chat request.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

HISTORY_ROOT_DEFAULT = Path(r"E:\qwen-projects\.file-history")
INDEX_TAIL_BYTES = 4 * 1024 * 1024
HASH_MAX_BYTES = 64 * 1024 * 1024
# Diff views are copies, so editing the left pane in VS Code can never alter a history backup.
DIFF_VIEW_ROOT = Path(os.environ.get("TEMP") or os.environ.get("TMP") or r"C:\Windows\Temp") / "qwen-diff"
DIFF_VIEW_MAX_AGE_SECONDS = 24 * 3600
FINISHED_STATES = {"completed", "failed", "cancelled", "timed_out", "interrupted"}
PAGE_SOURCE = "path-manager"
PAGE_PRINCIPAL = "local-owner"  # fast_server.py's default principal for the webui source
_CHANGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _as_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"status": "unknown", "raw": raw[:2000]}
        return parsed if isinstance(parsed, dict) else {"status": "unknown", "raw": parsed}
    return {"status": "unknown", "raw": str(raw)[:2000]}


def _rows(raw: Any, keys: tuple[str, ...]) -> list[dict[str, Any]] | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(raw, dict):
        raw = next((raw[key] for key in keys if isinstance(raw.get(key), list)), None)
    if not isinstance(raw, list):
        return None
    return [row for row in raw if isinstance(row, dict)]


def _call_supported(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call fn with only the keyword arguments its signature accepts."""
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args)
    if any(parameter.kind is parameter.VAR_KEYWORD for parameter in parameters.values()):
        return fn(*args, **kwargs)
    return fn(*args, **{key: value for key, value in kwargs.items() if key in parameters})


def _import(name: str) -> Any:
    try:
        return __import__(name)
    except Exception:
        return None


def policy_block(permissions: Any, categories: list[str], *, project: str = "") -> str | None:
    """Return a Thai reason when Permission Center denies one of the categories, else None.

    The Path Manager is the owner's own page on 127.0.0.1, so the click itself is the approval:
    an Ask policy adds no prompt here (the owner's rule: security is additive, never a new
    confirmation). Only an explicit Deny for the webui source blocks the button.
    """
    for category in categories:
        try:
            decision = permissions.get_policy("webui", category, principal=PAGE_PRINCIPAL, project=project.casefold())
        except Exception:
            return "ตรวจสิทธิ์ใน Permission Center ไม่สำเร็จ จึงยังไม่ทำรายการนี้"
        if decision == "deny":
            return f"Permission Center ตั้งหมวด {category} ของ webui เป็น Deny จึงสั่งจากหน้านี้ไม่ได้"
    return None


def audit(permissions: Any, action: str, result: str, category: str, *, project: str = "") -> None:
    try:
        permissions.audit(
            "webui", action, result, {"category": category, "via": PAGE_SOURCE},
            principal=PAGE_PRINCIPAL, project=project.casefold(),
        )
    except Exception:
        pass


# ----- file history -------------------------------------------------------------------------

def _history_root(module: Any) -> Path:
    value = getattr(module, "HISTORY_ROOT", None) if module is not None else None
    return Path(str(value)) if value else HISTORY_ROOT_DEFAULT


def _index_path(module: Any) -> Path:
    name = getattr(module, "INDEX_NAME", None) if module is not None else None
    return _history_root(module) / str(name or "index.jsonl")


def _merged_index(module: Any) -> list[dict[str, Any]]:
    """Applied changes from index.jsonl, oldest first (last 4 MB only).

    file_history writes up to three events per id (backup, after, failed); only a change whose
    write landed ("after") is a real change.
    """
    path = _index_path(module)
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - INDEX_TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if size > INDEX_TAIL_BYTES and lines:
        lines = lines[1:]  # the first line is probably cut in half
    entries: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or not row.get("id"):
            continue
        change_id = str(row["id"])
        event = row.get("event")
        if event in (None, "backup"):
            entry = dict(row)
            entry.pop("event", None)
            entry["state"] = "applied" if event is None else "pending"
            entries[change_id] = entry
        elif change_id in entries and event == "after":
            entries[change_id].update(sha_after=row.get("sha_after"), state="applied")
        elif change_id in entries and event == "failed":
            entries[change_id]["state"] = "failed"
    return [entry for entry in entries.values() if entry.get("state") == "applied"]


def _check_change_id(change_id: str) -> str:
    value = str(change_id or "").strip()
    if not _CHANGE_ID.fullmatch(value):
        raise ValueError("รหัสรายการแก้ไขไม่ถูกต้อง")
    return value


def _row_id(row: dict[str, Any]) -> str:
    return str(row.get("change_id") or row.get("id") or "")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _current_state(path: str, sha_after: str, sha_before: str, cache: dict[str, str]) -> str:
    """same = the file still holds this change's result; reverted = back to the state before it."""
    if not path:
        return "unknown"
    key = os.path.normcase(path)
    if key not in cache:
        target = Path(path)
        try:
            if not target.is_file():
                cache[key] = "missing"
            elif target.stat().st_size > HASH_MAX_BYTES:
                cache[key] = "unknown"
            else:
                cache[key] = _sha256(target)
        except OSError:
            cache[key] = "unknown"
    digest = cache[key]
    if digest in {"missing", "unknown"}:
        return digest
    if sha_after and digest == sha_after.casefold():
        return "same"
    if sha_before and digest == sha_before.casefold():
        return "reverted"
    return "changed" if sha_after else "unknown"


def _backup_file(row: dict[str, Any], module: Any) -> Path | None:
    """The change's backup copy, validated by file_history when it is installed."""
    change_id = _row_id(row)
    getter = getattr(module, "get_change", None) if module is not None else None
    locator = getattr(module, "backup_path", None) if module is not None else None
    if callable(getter) and callable(locator):
        try:
            return Path(locator(getter(change_id)))
        except Exception:
            return None
    relative = row.get("backup")
    root = _history_root(module).resolve()
    if relative:
        blob = (root / str(relative)).resolve()
        return blob if root in blob.parents and blob.is_file() else None
    return None


def _public_change(row: dict[str, Any], cache: dict[str, str]) -> dict[str, Any]:
    path = str(row.get("path") or "")
    sha_after = str(row.get("sha_after") or "")
    sha_before = str(row.get("sha_before") or "")
    current = _current_state(path, sha_after, sha_before, cache)
    if "has_backup" in row:
        has_backup = bool(row.get("has_backup"))
    else:
        has_backup = bool(row.get("backup"))
    if "revertable_now" in row:
        can_revert = bool(row.get("revertable_now"))
    else:
        can_revert = has_backup and current == "same"
    return {
        "id": _row_id(row),
        "time": str(row.get("time") or ""),
        "source": str(row.get("source") or ""),
        "principal": str(row.get("principal") or ""),
        "action": str(row.get("action") or ""),
        "path": path,
        "name": Path(path).name if path else "",
        "sha_before": sha_before,
        "sha_after": sha_after,
        "has_backup": has_backup,
        "backup_skipped": str(row.get("backup_skipped") or ""),
        "reverts": str(row.get("reverts") or ""),
        "current": current,
        "can_revert": can_revert,
        "can_diff": has_backup or current in {"same", "changed", "reverted", "unknown"},
    }


def list_changes(limit: int = 100, path_filter: str = "") -> dict[str, Any]:
    module = _import("file_history")
    limit = max(1, min(int(limit or 100), 200))
    rows: list[dict[str, Any]] | None = None
    source = "file_history"
    lister = getattr(module, "list_changes", None) if module is not None else None
    if callable(lister):
        try:
            rows = _rows(_call_supported(lister, path="", limit=200), ("changes", "items", "entries"))
        except Exception:
            rows = None
    if rows is None:
        rows = list(reversed(_merged_index(module)))
        source = "index"
    needle = str(path_filter or "").strip().casefold()
    if needle:
        rows = [row for row in rows if needle in str(row.get("path") or "").casefold()]
    cache: dict[str, str] = {}
    changes = [_public_change(row, cache) for row in rows]
    changes = [change for change in changes if change["id"]]
    changes.sort(key=lambda change: change["time"], reverse=True)
    return {
        "available": module is not None or _index_path(module).exists(),
        "source": source,
        "history_root": str(_history_root(module)),
        "changes": changes[:limit],
    }


def find_change(change_id: str) -> dict[str, Any]:
    change_id = _check_change_id(change_id)
    module = _import("file_history")
    getter = getattr(module, "get_change", None) if module is not None else None
    if callable(getter):
        # Its own errors (bad id, pruned) are already Thai and specific.
        return _as_dict(getter(change_id))
    for row in reversed(_merged_index(module)):
        if _row_id(row) == change_id:
            return row
    raise FileNotFoundError(f"ไม่พบรายการแก้ไข {change_id}")


def _prune_views() -> None:
    try:
        entries = list(DIFF_VIEW_ROOT.iterdir())
    except OSError:
        return
    cutoff = time.time() - DIFF_VIEW_MAX_AGE_SECONDS
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                os.chmod(entry, stat.S_IWRITE)
                entry.unlink()
        except OSError:
            continue


def _readonly_copy(backup: Path, change_id: str) -> Path:
    DIFF_VIEW_ROOT.mkdir(parents=True, exist_ok=True)
    _prune_views()
    name = backup.name
    if name.startswith(f"{change_id}-"):
        name = name[len(change_id) + 1:]
    target = DIFF_VIEW_ROOT / f"{change_id}-before-{name}"
    if target.exists():
        os.chmod(target, stat.S_IWRITE)
        target.unlink()
    shutil.copyfile(backup, target)
    os.chmod(target, stat.S_IREAD)
    return target


def diff_plan(change_id: str, *, code_exe: str | None, path_guard: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Decide what to open: a VS Code diff (backup -> current), or just one file."""
    change_id = _check_change_id(change_id)
    module = _import("file_history")
    row = find_change(change_id)
    path = str(row.get("path") or "")
    current = Path(path) if path else None
    if current is not None and path_guard is not None:
        guarded = path_guard(str(current))  # raises when the path is blocked
        current = Path(guarded) if guarded else current
    backup = _backup_file(row, module)
    if backup is not None and not backup.is_file():
        backup = None
    if backup is not None and path_guard is not None:
        path_guard(str(backup))
    current_exists = current is not None and current.is_file()
    if backup is not None and current_exists:
        view = _readonly_copy(backup, change_id)
        return {"mode": "diff", "path": str(current), "before": str(view), "code_exe": code_exe}
    if current_exists:
        # No before-state (the backup was skipped or pruned): open the file itself.
        return {"mode": "open", "path": str(current), "before": "", "code_exe": code_exe}
    if backup is not None:
        view = _readonly_copy(backup, change_id)
        return {"mode": "open_backup", "path": str(view), "before": "", "code_exe": code_exe}
    raise FileNotFoundError("ไม่มีทั้งไฟล์ปัจจุบันและไฟล์สำรองของรายการนี้")


def launch_plan(plan: dict[str, Any], *, opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Open the plan in VS Code, through file_edit.open_in_editor when it is installed."""
    if opener is None:
        module = _import("file_edit")
        opener = getattr(module, "open_in_editor", None) if module is not None else None
    if callable(opener):
        kwargs: dict[str, Any] = {"line": 1}
        if plan.get("code_exe"):
            kwargs["program"] = plan["code_exe"]
        if plan["mode"] == "diff":
            kwargs["diff_with"] = plan["before"]
        result = _as_dict(_call_supported(opener, plan["path"], **kwargs))
        return {"status": str(result.get("status") or "launched"), "mode": plan["mode"], "path": plan["path"], "result": result}
    exe = plan.get("code_exe")
    if not exe:
        raise RuntimeError("ไม่พบ VS Code ในเครื่องนี้ จึงเปิดไฟล์ไม่ได้")
    if plan["mode"] == "diff":
        argv = [exe, "--diff", plan["before"], plan["path"]]
    elif plan["mode"] == "open":
        argv = [exe, "-g", f"{plan['path']}:1"]
    else:
        argv = [exe, plan["path"]]
    # Set when the server was started from a VS Code terminal; Code.exe would then run as Node.
    env = {key: value for key, value in os.environ.items() if key.upper() != "ELECTRON_RUN_AS_NODE"}
    process = subprocess.Popen(
        argv,
        cwd=str(Path(plan["path"]).parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        env=env,
    )
    return {"status": "launched", "mode": plan["mode"], "path": plan["path"], "pid": process.pid, "command": subprocess.list2cmdline(argv)}


def revert_change(
    change_id: str,
    *,
    path_guard: Callable[..., Any] | None = None,
    revert_tool: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Revert through the chat tool (revert_file_change) when given, else file_edit/file_history.

    The history row is labelled source=path-manager so the owner can tell a click from chat.
    """
    change_id = _check_change_id(change_id)
    row = find_change(change_id)
    path = str(row.get("path") or "")
    if path and path_guard is not None:
        path_guard(path)
    history = _import("file_history")
    token = None
    setter = getattr(history, "set_caller", None) if history is not None else None
    if callable(setter):
        token = setter(PAGE_SOURCE, PAGE_PRINCIPAL)
    try:
        if callable(revert_tool):
            return _as_dict(_call_supported(revert_tool, change_id))
        path_check = (lambda target: path_guard(str(target))) if path_guard is not None else None
        for module_name, function_name in (("file_edit", "revert_change"), ("file_history", "revert")):
            module = _import(module_name)
            function = getattr(module, function_name, None) if module is not None else None
            if callable(function):
                return _as_dict(
                    _call_supported(function, change_id, path_check=path_check, source=PAGE_SOURCE, principal=PAGE_PRINCIPAL)
                )
        raise RuntimeError("ย้อนกลับไม่ได้: ยังไม่ได้ติดตั้งระบบประวัติการแก้ไขไฟล์ (file_history.py)")
    finally:
        if token is not None:
            try:
                history.CALLER.reset(token)
            except Exception:
                pass


# ----- finished-work inbox ------------------------------------------------------------------

def _outputs(row: dict[str, Any]) -> list[dict[str, str]]:
    found: list[Any] = []
    for key in ("outputs", "output_paths", "files", "links", "output", "output_path", "result_path"):
        value = row.get(key)
        if isinstance(value, list):
            found.extend(value)
        elif value:
            found.append(value)
    outputs: list[dict[str, str]] = []
    for item in found:
        if isinstance(item, dict):
            path = str(item.get("path") or "")
            url = str(item.get("url") or "")
            label = str(item.get("label") or item.get("name") or Path(path).name or url)
        else:
            text = str(item)
            is_url = text.startswith(("http://", "https://"))
            path, url = ("", text) if is_url else (text, "")
            label = text if is_url else (Path(text).name or text)
        if (path or url) and not any(entry["path"] == path and entry["url"] == url for entry in outputs):
            outputs.append({"label": label[:120], "path": path, "url": url})
        if len(outputs) >= 10:
            break
    return outputs


def _inbox_item(row: dict[str, Any]) -> dict[str, Any]:
    item_id = str(row.get("id") or row.get("task_id") or row.get("job_id") or row.get("run_id") or "")
    kind = str(row.get("kind") or row.get("type") or "")
    if not kind:
        kind = "video" if item_id.startswith("video-") else "task"
    # TaskMonitor counts a task from before the inbox (no read_at key) as read; honour its flag.
    unread = bool(row["unread"]) if "unread" in row else not row.get("read_at")
    return {
        "id": item_id,
        "kind": kind,
        "project": str(row.get("project") or ""),
        "title": str(row.get("title") or row.get("name") or item_id)[:200],
        "status": str(row.get("status") or ""),
        "source": str(row.get("source") or ""),
        "program": str(row.get("program") or ""),
        "time": str(row.get("finished_at") or row.get("ended_at") or row.get("completed_at") or row.get("time") or row.get("started_at") or ""),
        "started_at": row.get("started_at"),
        "elapsed_seconds": row.get("elapsed_seconds"),
        "summary": str(row.get("summary") or row.get("message") or row.get("result_summary") or "")[:600],
        "outputs": _outputs(row),
        "exit_code": row.get("exit_code"),
        "unread": unread,
        "read_at": row.get("read_at") or None,
        "archived": bool(row.get("archived") or row.get("archived_at")),
        "has_log": bool(row.get("stdout_path") or row.get("stderr_path")) or kind == "task",
    }


def list_inbox(tasks: Any, *, limit: int = 100, include_archived: bool = False, unread_only: bool = False) -> dict[str, Any]:
    limit = max(1, min(int(limit or 100), 500))
    lister = getattr(tasks, "list_inbox", None)
    raw: Any = None
    rows: list[dict[str, Any]] | None = None
    if callable(lister):
        raw = _call_supported(lister, limit=limit, include_archived=include_archived, unread_only=unread_only)
        rows = _rows(raw, ("items", "inbox", "tasks"))
    native = rows is not None
    if rows is None:
        # TaskMonitor from before the inbox: finished tasks, all shown as read, no marking.
        rows = [dict(task, unread=False) for task in tasks.list(include_completed=True, limit=500) if task.get("status") in FINISHED_STATES]
    items = [_inbox_item(row) for row in rows]
    items = [item for item in items if item["id"]]
    if not include_archived:
        items = [item for item in items if not item["archived"]]
    if unread_only:
        items = [item for item in items if item["unread"]]
    items.sort(key=lambda item: item["time"], reverse=True)
    items = items[:limit]
    unread_count = raw.get("unread_count") if isinstance(raw, dict) and isinstance(raw.get("unread_count"), int) else None
    if unread_count is None:
        unread_count = sum(1 for item in items if item["unread"] and not item["archived"])
    return {
        "items": items,
        "unread_count": unread_count,
        "supports_marking": native and callable(getattr(tasks, "mark_read", None)),
        "supports_archive": native and callable(getattr(tasks, "archive", None)),
    }


def mark_inbox(tasks: Any, ids: list[Any], op: str) -> dict[str, Any]:
    if op == "read_all":
        bulk = getattr(tasks, "mark_all_read", None)
        if callable(bulk):
            return {"status": "updated", "op": op, "count": int(bulk() or 0), "errors": []}
        op = "read"
    ids = [str(item).strip() for item in (ids or []) if str(item).strip()][:200]
    if not ids:
        raise ValueError("ยังไม่ได้เลือกรายการ")
    if op not in {"read", "unread", "archive", "unarchive"}:
        raise ValueError("op ต้องเป็น read, unread, archive, unarchive หรือ read_all")
    positive = op in {"read", "archive"}
    dedicated = {"unread": ("mark_unread",), "unarchive": ("unarchive", "restore")}.get(op, ())
    function = next((getattr(tasks, name) for name in dedicated if callable(getattr(tasks, name, None))), None)
    extra: dict[str, Any] = {}
    if function is None:
        function = getattr(tasks, "mark_read" if op in {"read", "unread"} else "archive", None)
        if not callable(function):
            raise RuntimeError("Task Monitor ที่รันอยู่ยังไม่รองรับกล่องงานเสร็จ (ติดตั้งเวอร์ชันใหม่แล้วรีสตาร์ต local-tools)")
        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            parameters = {}
        flags = ("read", "is_read", "value", "state") if op in {"read", "unread"} else ("archived", "archive", "value", "state")
        flag = next((name for name in flags if name in parameters), None)
        if flag:
            extra[flag] = positive
        elif not positive:
            raise RuntimeError("Task Monitor รุ่นนี้ยังยกเลิกสถานะนี้ไม่ได้")
    try:
        first = next(iter(inspect.signature(function).parameters), "")
    except (TypeError, ValueError):
        first = ""
    errors: list[str] = []
    done = 0
    if first.endswith("ids"):
        function(ids, **extra)
        done = len(ids)
    else:
        for item_id in ids:
            try:
                function(item_id, **extra)
                done += 1
            except Exception as exc:  # one stale id must not abort a bulk action
                errors.append(f"{item_id}: {exc}")
    if errors and not done:
        raise RuntimeError("; ".join(errors[:5]))
    return {"status": "updated", "op": op, "count": done, "errors": errors[:20]}
