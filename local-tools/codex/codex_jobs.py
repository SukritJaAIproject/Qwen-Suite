"""Codex-Qwen bridge helpers: job list, event summaries, output-schema check, notification,
policy-hook trust check (hooks/list) and the changed-files snapshot of write jobs.

Invoke-QwenCodex.ps1 calls the CLI at the bottom; the gateway's list_codex_jobs and
get_codex_job (codex_dispatch.py) import list_jobs(). Nothing here starts a Codex job or talks
to a model (hook_list starts `codex app-server` only to ask which hooks Codex sees).

Layout (ROOT = the suite folder, two levels above this file):
  ROOT\\logs\\codex-bridge\\jobs.jsonl     one line per job event (queued/started/finished)
  <run dir>\\result.json                   the summary Invoke-QwenCodex.ps1 writes at the end
  <run dir>\\policy-denials.jsonl          commands policy_hook.py blocked during the job
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent.parent
FINAL_CHARS_LIST = 600
FINAL_CHARS_DETAIL = 4000

# ---------------------------------------------------------------------------- secrets
# A read-only job can read any file the user can (Get-Content is one of Codex's known-safe
# reads; qwen-bridge.rules block credential TOOLS by name only), and list_codex_jobs hands
# the answer to Telegram/WebUI. So a job whose commands or answer touch a secret folder
# (the same names as $BlockedParts in Invoke-QwenCodex.ps1) shows no answer in chat, and
# every other answer is masked for secret-looking strings (suite_health._redact's patterns,
# line breaks kept). Best effort: the full answer stays in the run folder on disk.
_SENSITIVE_PATH = re.compile(
    r"(?i)(?<![\w.-])\.(?:codex|openclaw|ssh|aws|azure|gnupg|kube|claude)(?![\w-])"  # not .codex-runs
    r"|[\\/](?:credentials|login data|web data)(?=$|[\\/\s\"'`),;:]|\.(?!\w))")  # not credentials.py
_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S), "<กุญแจส่วนตัว ซ่อน>"),
    (re.compile(r"(?i)\bbot\d{5,}:[\w-]{20,}"), "bot<ซ่อน>"),
    (re.compile(r"\b\d{5,}:[\w-]{20,}"), "<ซ่อน>"),
    (re.compile(r"(?i)\b(bearer|basic)([ \t]+)[A-Za-z0-9+/=._~-]{12,}"), r"\1\2<ซ่อน>"),
    # key = value, key: value, "key": "value" (JSON/TOML/env files)
    (re.compile(r"(?i)(\b\w*(?:token|api[_-]?key|secret|password|passwd|authorization|cookie)\w*[\"']?[ \t]*[=:][ \t]*[\"']?)[^\s,;\"'}]{4,}"), r"\1<ซ่อน>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<อีเมล>"),
    (re.compile(r"@\w{3,}bot\b", re.I), "@<bot>"),
    (re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<id>"),
    (re.compile(r"\b(?:sha256[:-])?[0-9a-f]{32,}\b", re.I), "<hash>"),
]
_RE_LONG = re.compile(r"[A-Za-z0-9_\-+=]{24,}")  # keys, session tokens
# chat ids, phone numbers; not dates or bridge job ids (yyyyMMdd-HHmmss-xxxx)
_RE_DIGITS = re.compile(r"(?<!\d)(?!\d{4}-\d{2}-\d{2}(?!\d)|\d{8}-\d{6}-[0-9a-f]{4}\b)\+?\d[\d \-]{6,}\d")
ANSWER_HIDDEN = ("(ไม่แสดงคำตอบในแชต: งานนี้อ่านหรืออ้างถึงโฟลเดอร์ที่เก็บข้อมูลลับ เช่น .codex, .openclaw, .ssh "
                 "หรือข้อมูลล็อกอินของเบราว์เซอร์ - เปิดอ่านเองจาก last.md ในโฟลเดอร์งาน (run_dir) บนเครื่อง)")


def _mentions_secret_path(text: Any) -> bool:
    return bool(_SENSITIVE_PATH.search(str(text or "")))


def _long_repl(match: re.Match) -> str:
    text = match.group(0)
    mixed = any(c.islower() for c in text) and any(c.isupper() for c in text)
    return "<ซ่อน>" if any(c.isdigit() for c in text) or mixed else text


def _digits_repl(match: re.Match) -> str:
    return "<ตัวเลข>" if sum(ch.isdigit() for ch in match.group(0)) >= 8 else match.group(0)


def redact_secrets(text: Any) -> str:
    """Mask anything that looks like a secret or an id; keeps line breaks."""
    value = str(text or "")
    for pattern, repl in _SECRET_PATTERNS:
        value = pattern.sub(repl, value)
    value = _RE_LONG.sub(_long_repl, value)
    return _RE_DIGITS.sub(_digits_repl, value)


def _safe_answer(text: Any, limit: int, hidden: bool) -> str:
    """What chat may see of a model answer: nothing when the job touched a secret folder,
    otherwise the masked text, clipped."""
    value = str(text or "")
    if not value:
        return ""
    if hidden or _mentions_secret_path(value):
        return ANSWER_HIDDEN
    return _clip(redact_secrets(value), limit)


def _safe_error(text: Any, hidden: bool, exit_code: Any) -> str:
    """A job's error: the bridge's own words, but a schema failure (exit 6) quotes the
    answer and Codex errors may echo it."""
    if hidden and str(exit_code) == "6":
        return "คำตอบไม่ตรง schema " + ANSWER_HIDDEN
    return redact_secrets(text)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _clip(text: Any, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + " ..."


# ------------------------------------------------------------------------------- events
def summarize_events(path: str | Path) -> dict[str, Any]:
    """Condense a `codex exec --json` event stream into what a person needs to know."""
    commands: list[dict[str, Any]] = []
    files: dict[str, str] = {}
    errors: list[str] = []
    final = ""
    thread_id = ""
    usage = {"input_tokens": 0, "output_tokens": 0}
    turns = 0
    plan: list[dict[str, Any]] = []
    touched = False  # a command or changed file named a secret folder (see _SENSITIVE_PATH)
    for event in _read_jsonl(Path(path)):
        kind = event.get("type")
        any_item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if any_item.get("type") == "command_execution" and _mentions_secret_path(any_item.get("command")):
            touched = True  # item.started too: a command killed by the timeout never completes
        if kind == "thread.started":
            thread_id = str(event.get("thread_id") or "")
        elif kind == "turn.completed":
            turns += 1
            for key in usage:
                try:
                    usage[key] += int((event.get("usage") or {}).get(key) or 0)
                except (TypeError, ValueError):
                    pass
        elif kind == "turn.failed":
            errors.append(str((event.get("error") or {}).get("message") or "turn failed"))
        elif kind == "error":
            errors.append(str(event.get("message") or "error"))
        elif kind == "item.completed":
            item = event.get("item") or {}
            itype = item.get("type")
            if itype == "agent_message":
                final = str(item.get("text") or "")
            elif itype == "command_execution":
                commands.append({
                    "command": _clip(item.get("command"), 300),
                    "status": item.get("status"),
                    "exit_code": item.get("exit_code"),
                    # the rejection reason for a declined command (Thai text from the rules)
                    "note": _clip(item.get("aggregated_output"), 200) if item.get("status") == "declined" else "",
                })
            elif itype == "file_change":
                for change in item.get("changes") or []:
                    if isinstance(change, dict) and change.get("path"):
                        files[str(change["path"])] = str(change.get("kind") or "update")
                        touched = touched or _mentions_secret_path(change["path"])
            elif itype == "todo_list":
                plan = [{"text": _clip(t.get("text"), 120), "done": bool(t.get("completed"))}
                        for t in item.get("items") or [] if isinstance(t, dict)]
            elif itype == "error":
                errors.append(str(item.get("message") or "error"))
    return {
        "thread_id": thread_id,
        "turns": turns,
        "commands_total": len(commands),
        "commands_declined": sum(c["status"] == "declined" for c in commands),
        "commands_failed": sum(c["status"] == "failed" for c in commands),
        "last_commands": commands[-5:],
        "files_changed": [{"path": p, "kind": k} for p, k in files.items()],
        "plan": plan,
        "errors": errors[-5:],
        "usage": usage,
        "final_message": final,
        "touched_sensitive_paths": touched,
    }


# ------------------------------------------------------------------------ output schema
def validate_output(schema_path: str | Path, text: str) -> dict[str, Any]:
    """Codex 0.146 only forwards --output-schema to the server; it never checks the answer.
    Ollama usually constrains the output, but a wrong answer must fail the job, not pass."""
    try:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"ok": False, "error": f"อ่าน schema ไม่ได้: {exc}"}
    try:
        value = json.loads(text)
    except Exception as exc:
        return {"ok": False, "error": f"คำตอบสุดท้ายไม่ใช่ JSON: {exc}"}
    try:
        import jsonschema
    except ImportError:
        # Minimal fallback: the object has every required key.
        missing = [k for k in schema.get("required") or [] if not isinstance(value, dict) or k not in value]
        if missing:
            return {"ok": False, "error": "ขาดคีย์ที่ต้องมี: " + ", ".join(missing)}
        return {"ok": True, "checked_with": "required-keys"}
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path) or "(ราก)"
        return {"ok": False, "error": f"ไม่ตรง schema ที่ {where}: {exc.message}"}
    except jsonschema.SchemaError as exc:
        return {"ok": False, "error": f"schema ไม่ถูกต้อง: {exc.message}"}
    return {"ok": True, "checked_with": "jsonschema"}


# --------------------------------------------------------------------------- notify
CHAT_SOURCES = {"openclaw", "webui", "mcp", "web"}


def notify(tools_dir: str | Path, title: str, status: str, minutes: float, dry_run: bool = False,
           source: str = "", job_id: str = "") -> dict[str, Any]:
    """Hand the result to local-tools\\notifier.py when it is installed. Only the title and
    status leave the machine - never prompts, file contents or paths.

    A job started from chat (source openclaw/webui/mcp, set by codex_dispatch.py) goes where
    notifier.resolve_channels sends every chat-started task: Telegram + toast for OpenClaw,
    toast only for Open WebUI. A desktop job (no source) keeps the notifier's default channels."""
    path = Path(tools_dir) / "notifier.py"
    if not path.exists():
        return {"sent": False, "reason": "notifier.py is not installed"}
    if str(Path(tools_dir)) not in sys.path:
        sys.path.insert(0, str(Path(tools_dir)))
    spec = importlib.util.spec_from_file_location("qwen_bridge_notifier", path)
    if spec is None or spec.loader is None:
        return {"sent": False, "reason": "notifier.py cannot be loaded"}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    func = getattr(module, "notify", None)
    if not callable(func):
        return {"sent": False, "reason": "notifier.py has no notify()"}
    params = inspect.signature(func).parameters
    if dry_run and "dry_run" not in params:
        return {"sent": False, "reason": "dry run requested but notify() has no dry_run"}
    kwargs: dict[str, Any] = {"title": title, "status": status, "minutes": minutes}
    if "media_path" in params:
        kwargs["media_path"] = ""
    if "dry_run" in params:
        kwargs["dry_run"] = bool(dry_run)
    channels: list[str] | None = None
    normalized = str(source or "").strip().casefold()
    if normalized in CHAT_SOURCES and "channels" in params and callable(getattr(module, "resolve_channels", None)):
        channels = list(module.resolve_channels(None, normalized))
        kwargs["channels"] = channels
    clean_id = re.sub(r"[^0-9A-Za-z._-]", "", str(job_id or ""))[:80]
    if clean_id and "task_id" in params:
        # a failed job's message ends with "ดูรายละเอียดด้วย get_codex_job <id>"
        kwargs["task_id"] = clean_id
        if "lookup" in params:
            kwargs["lookup"] = "get_codex_job"
    result = func(**kwargs)
    # Only the shape goes back to the log: the notifier's own result may name the recipient.
    shape = sorted(result) if isinstance(result, dict) else type(result).__name__
    out: dict[str, Any] = {"sent": True, "dry_run": bool(dry_run), "result_keys": shape}
    if channels is not None:
        out["channels"] = channels
    if isinstance(result, dict) and result.get("status"):
        out["status"] = str(result.get("status"))
    return out


# ------------------------------------------------------------------- policy hook trust
_HOOK_FIELDS = ("key", "eventName", "handlerType", "matcher", "command", "timeoutSec", "sourcePath", "source",
                "enabled", "isManaged", "currentHash", "trustStatus")


def _codex_env(codex_home: str | Path) -> dict[str, str]:
    """The environment Invoke-WithCodexEnv gives codex.exe: its own CODEX_HOME, a dead proxy
    for everything but 127.0.0.1, and no OpenAI keys."""
    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_home)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[name] = "http://127.0.0.1:9"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY"):
        env.pop(name, None)
    return env


def hook_list(codex_exe: str | Path, codex_home: str | Path, cwd: str | Path, timeout: float = 25.0) -> dict[str, Any]:
    """Which hooks Codex sees for `cwd`, with their trust status and current hash.

    Asks `codex app-server` (JSON-RPC over stdio: initialize, hooks/list) - Codex computes the
    hash itself, so the bridge never has to know the algorithm, and a Codex update that changes
    it shows up as "untrusted" here instead of a hook that silently stops running. No model is
    contacted; the process is killed right after the answer (~0.4 s)."""
    try:
        proc = subprocess.Popen(
            [str(codex_exe), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=str(cwd), env=_codex_env(codex_home),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        return {"ok": False, "error": f"เปิด codex app-server ไม่ได้: {type(exc).__name__}: {exc}"}
    responses: dict[int, dict[str, Any]] = {}

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                message = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(message, dict) and isinstance(message.get("id"), int):
                responses[message["id"]] = message

    threading.Thread(target=reader, daemon=True).start()

    def send(obj: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def call(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if request_id in responses:
                return responses[request_id]
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        raise TimeoutError(f"codex app-server ไม่ตอบ {method} ภายใน {timeout:.0f} วินาที")

    try:
        init = call(1, "initialize", {"clientInfo": {"name": "qwen-codex-bridge", "version": "1"}})
        if "error" in init:
            return {"ok": False, "error": "initialize: " + json.dumps(init["error"], ensure_ascii=False)[:300]}
        send({"method": "initialized"})
        answer = call(2, "hooks/list", {"cwds": [str(cwd)]})
        if "error" in answer:
            return {"ok": False, "error": "hooks/list: " + json.dumps(answer["error"], ensure_ascii=False)[:300]}
        hooks: list[dict[str, Any]] = []
        warnings: list[str] = []
        errors: list[str] = []
        for entry in (answer.get("result") or {}).get("data") or []:
            if not isinstance(entry, dict):
                continue
            hooks.extend({k: h.get(k) for k in _HOOK_FIELDS} for h in entry.get("hooks") or [] if isinstance(h, dict))
            warnings.extend(str(w)[:300] for w in entry.get("warnings") or [])
            errors.extend(str(e)[:300] for e in entry.get("errors") or [])
        return {"ok": True, "hooks": hooks, "warnings": warnings, "errors": errors}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


# --------------------------------------------------------------- changed-files snapshot
# Write jobs change files with shell commands (Set-Content, python ...): Codex 0.146 has no
# apply_patch here, so its event stream has no file_change items. The bridge compares a
# size+mtime listing of the job folders from before and after the run instead.
SNAPSHOT_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".codex-runs", "__pycache__", ".venv", "venv",
                      ".mypy_cache", ".pytest_cache", ".tox", ".idea", ".vs"}
SNAPSHOT_MAX_FILES = 20_000
CHANGES_MAX_LISTED = 200


def _walk(root: Path, limit: int) -> tuple[dict[str, list[int]], bool]:
    files: dict[str, list[int]] = {}
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue  # junctions/symlinks: never followed out of the job folder
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.casefold() not in SNAPSHOT_SKIP_DIRS:
                        stack.append(Path(entry.path))
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            files[os.path.relpath(entry.path, root)] = [int(stat.st_size), int(stat.st_mtime_ns)]
            if len(files) >= limit:
                return files, True
    return files, False


def snapshot(out_file: str | Path, roots: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {"taken_at": time.time(), "roots": {}, "truncated": False}
    for root in dict.fromkeys(str(r) for r in roots if str(r).strip()):
        files, truncated = _walk(Path(root), SNAPSHOT_MAX_FILES)
        data["roots"][root] = files
        data["truncated"] = data["truncated"] or truncated
    Path(out_file).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "roots": len(data["roots"]), "files": sum(len(v) for v in data["roots"].values()),
            "truncated": data["truncated"]}


def changes(snapshot_file: str | Path) -> dict[str, Any]:
    """Files added, modified or deleted under the snapshot's roots since it was taken."""
    data = json.loads(Path(snapshot_file).read_text(encoding="utf-8-sig"))
    listed: list[dict[str, str]] = []
    counts = {"added": 0, "modified": 0, "deleted": 0}
    truncated = bool(data.get("truncated"))
    for root, before in (data.get("roots") or {}).items():
        after, cut = _walk(Path(root), SNAPSHOT_MAX_FILES)
        truncated = truncated or cut
        for rel in sorted(set(before) | set(after)):
            if rel not in before:
                kind = "added"
            elif rel not in after:
                kind = "deleted"
                if cut:
                    continue  # a truncated walk cannot tell "deleted" from "not reached"
            elif list(before[rel]) != list(after[rel]):
                kind = "modified"
            else:
                continue
            counts[kind] += 1
            if len(listed) < CHANGES_MAX_LISTED:
                listed.append({"path": str(Path(root) / rel), "kind": kind})
    return {"ok": True, "counts": counts, "total": sum(counts.values()), "files": listed,
            "listed": len(listed), "truncated_walk": truncated}


# ----------------------------------------------------------------------------- docx
def make_docx(tools_dir: str | Path, markdown_path: str | Path, out_dir: str | Path, title: str) -> dict[str, Any]:
    if str(Path(tools_dir)) not in sys.path:
        sys.path.insert(0, str(Path(tools_dir)))
    import document_builder  # the suite's own builder (Thai font, never overwrites)

    text = Path(markdown_path).read_text(encoding="utf-8-sig")
    return document_builder.make(Path(out_dir), "docx", title=title, content_markdown=text, output_name=Path(markdown_path).stem)


# ------------------------------------------------------------------------------ jobs
def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        import psutil
    except ImportError:
        return True  # cannot tell; do not call a live job dead
    try:
        proc = psutil.Process(pid)
        return "powershell" in (proc.name() or "").lower() or "pwsh" in (proc.name() or "").lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def blocked_commands(run_dir: Any, limit: int = 10) -> list[dict[str, Any]]:
    """The last commands policy_hook.py refused in one job (masked, clipped)."""
    if not run_dir:
        return []
    rows = _read_jsonl(Path(str(run_dir)) / "policy-denials.jsonl")
    return [{"ts": r.get("ts"), "command": _clip(redact_secrets(r.get("command")), 200), "reason": str(r.get("reason") or "")[:200]}
            for r in rows[-limit:]]


def list_jobs(root: str | Path | None = None, job_id: str = "", limit: int = 10) -> dict[str, Any]:
    """Read-only view of bridge jobs, newest first. With job_id: one job in more detail."""
    base = Path(root) if root else DEFAULT_ROOT
    index = base / "logs" / "codex-bridge" / "jobs.jsonl"
    jobs: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in _read_jsonl(index):
        jid = str(row.get("id") or "")
        if not jid:
            continue
        if jid not in jobs:
            jobs[jid] = {"id": jid}
            order.append(jid)
        job = jobs[jid]
        job.update({k: v for k, v in row.items() if k not in {"event", "ts"}})
        job["status"] = {"queued": "queued", "started": "running"}.get(str(row.get("event")), row.get("status") or job.get("status"))
        job.setdefault("queued_at", row.get("ts"))
        if row.get("event") == "started":
            job["started_at"] = row.get("ts")
        if row.get("event") == "finished":
            job["finished_at"] = row.get("ts")
    for job in jobs.values():
        if job.get("status") in {"queued", "running"} and not _pid_alive(job.get("pid")):
            job["status"] = "interrupted"
    wanted = str(job_id or "").strip()
    if wanted:
        if wanted not in jobs:
            raise FileNotFoundError(f"ไม่พบงาน Codex รหัส {wanted}")
        job = dict(jobs[wanted])
        hidden = bool(job.get("touched_sensitive_paths"))
        result_path = Path(str(job.get("run_dir") or "")) / "result.json"
        if job.get("run_dir") and result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8-sig"))
            except Exception:
                result = {}
            summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
            commands = [c for c in summary.get("last_commands") or [] if isinstance(c, dict)]
            hidden = (hidden or bool(summary.get("touched_sensitive_paths"))
                      or _mentions_secret_path(summary.get("final_message"))
                      or any(_mentions_secret_path(c.get("command")) for c in commands))
            job["summary"] = {
                **summary,
                "final_message": _safe_answer(summary.get("final_message"), FINAL_CHARS_DETAIL, hidden),
                "last_commands": [{**c, "command": redact_secrets(c.get("command")), "note": redact_secrets(c.get("note"))}
                                  for c in commands],
                "errors": [redact_secrets(e) for e in summary.get("errors") or []],
                "plan": [{**p, "text": redact_secrets(p.get("text"))} for p in summary.get("plan") or [] if isinstance(p, dict)],
            }
            for key in ("schema_check", "review_file", "review_docx", "exit_code", "error", "policy_hook"):
                if key in result:
                    job[key] = result[key]
            changed = result.get("changed_files") if isinstance(result.get("changed_files"), dict) else None
            if changed:
                job["changed_files"] = {
                    **{k: changed.get(k) for k in ("counts", "total", "listed", "truncated_walk") if k in changed},
                    "files": [{**f, "path": "<ซ่อน: โฟลเดอร์ข้อมูลลับ>" if _mentions_secret_path(f.get("path")) else f.get("path")}
                              for f in changed.get("files") or [] if isinstance(f, dict)][:50],
                }
        denials = blocked_commands(job.get("run_dir"))
        if denials:
            job["blocked_commands"] = denials
            if isinstance(job.get("schema_check"), dict) and job["schema_check"].get("error"):
                # jsonschema quotes the offending value, which is model output
                job["schema_check"] = {**job["schema_check"], "error": _safe_answer(job["schema_check"]["error"], 600, hidden)}
        job["final_message"] = _safe_answer(job.get("final_message"), FINAL_CHARS_DETAIL, hidden)
        if job.get("error"):
            job["error"] = _safe_error(job["error"], hidden, job.get("exit_code"))
        if hidden:
            job["answer_hidden"] = True
        return {"job": job}
    selected = [jobs[j] for j in reversed(order)][: max(1, min(int(limit or 10), 50))]
    out = []
    for job in selected:
        item = {k: job.get(k) for k in ("id", "kind", "status", "title", "mode", "model", "cwd", "source", "queued_at",
                                        "started_at", "finished_at", "minutes", "exit_code", "run_dir", "error",
                                        "files_changed_count")}
        hidden = bool(job.get("touched_sensitive_paths")) or _mentions_secret_path(job.get("final_message"))
        item["final_message"] = _safe_answer(job.get("final_message"), FINAL_CHARS_LIST, hidden)
        if item.get("error"):
            item["error"] = _safe_error(item["error"], hidden, item.get("exit_code"))
        if hidden and item["final_message"]:
            item["answer_hidden"] = True
        out.append({k: v for k, v in item.items() if v not in (None, "")})
    running = sum(j.get("status") in {"queued", "running"} for j in jobs.values())
    return {"running_count": running, "jobs": out, "log_dir": str(index.parent)}


# ------------------------------------------------------------------------------- CLI
def _main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    out_file = ""
    if "--out" in argv:
        # PowerShell reads the answer from this UTF-8 file; its console code page would
        # otherwise mangle Thai on stdout.
        at = argv.index("--out")
        out_file = argv[at + 1] if at + 1 < len(argv) else ""
        argv = argv[:at] + argv[at + 2:]
    if not argv:
        print("usage: codex_jobs.py summarize|validate|notify|docx|list|hooks|snapshot|changes ... [--out FILE]", file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]

    def emit(value: Any) -> None:
        text = json.dumps(value, ensure_ascii=False, indent=1, default=str)
        if out_file:
            Path(out_file).write_text(text, encoding="utf-8")
        else:
            print(text)

    try:
        if cmd == "summarize":
            out: Any = summarize_events(args[0])
        elif cmd == "validate":
            out = validate_output(args[0], Path(args[1]).read_text(encoding="utf-8-sig"))
        elif cmd == "notify":
            spec = json.loads(Path(args[0]).read_text(encoding="utf-8-sig"))
            out = notify(spec["tools_dir"], spec["title"], spec["status"], float(spec.get("minutes") or 0),
                         bool(spec.get("dry_run")), str(spec.get("source") or ""), str(spec.get("job_id") or ""))
        elif cmd == "hooks":
            # hooks CODEX_EXE CODEX_HOME CWD
            out = hook_list(args[0], args[1], args[2])
        elif cmd == "snapshot":
            # snapshot OUT_FILE ROOT [ROOT ...]  (roots may also come ;-joined in one argument)
            roots = [r for a in args[1:] for r in a.split(";") if r.strip()]
            out = snapshot(args[0], roots)
        elif cmd == "changes":
            out = changes(args[0])
        elif cmd == "docx":
            out = make_docx(args[0], args[1], args[2], args[3] if len(args) > 3 else "")
        elif cmd == "list":
            # list [ROOT] [--job ID] [--limit N]
            opts = {"--job": "", "--limit": "10"}
            rest: list[str] = []
            i = 0
            while i < len(args):
                if args[i] in opts and i + 1 < len(args):
                    opts[args[i]] = args[i + 1]
                    i += 2
                    continue
                rest.append(args[i])
                i += 1
            out = list_jobs(rest[0] if rest else None, opts["--job"], int(opts["--limit"]))
        else:
            print(f"unknown command: {cmd}", file=sys.stderr)
            return 2
    except Exception as exc:  # the caller logs it; a helper failure must not look like success
        emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    emit(out)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
