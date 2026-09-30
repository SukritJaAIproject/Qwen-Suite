"""Owner-authored lifecycle hooks for the local-tools gateway (block-only, fail-open).

gateway_dispatch already funnels every local action through one choke point with the
Permission Center and audit.jsonl. This adds the one thing Codex has that the suite did not:
the owner's own Pre/Post scripts. A Pre hook may VETO an action (block); it can never grant one
and it cannot ask for confirmation - the owner's standing rule is that chat channels run without
a prompt, so security here is additive (hard block or nothing). A Post hook is fire-and-forget.

Config: E:\localllm\data\permission-center\hooks.json
    {"pre":  [{"match": "run_command|write_text_file", "script": "<abs path>.py"}],
     "post": [{"match": ".*", "script": "<abs path>.py"}]}
An absent file means no hooks. The file may be UTF-8 (with or without a BOM) or UTF-16; one
that cannot be parsed keeps the last config that did (none on a fresh start) and writes a
'hook_error' line to audit.jsonl. Each matching script is run with the suite's
Python; the JSON {"source","action","arguments"} is written to its stdin. A Pre script that
prints {"decision":"block","reason":"..."} vetoes the action; anything else allows it.

run_pre_hooks(source, action, arguments) -> {"decision":"block","reason":...} | None
run_post_hooks(source, action, arguments, result) -> None   (returns immediately)

Both fail open: a broken or slow hook must never take the gateway down, so on any error/timeout
the action is allowed and a 'hook_error' line is written to audit.jsonl.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

_HOOKS_CONFIG = Path(r"E:\localllm\data\permission-center\hooks.json")
_AUDIT_PATH = Path(r"E:\localllm\data\permission-center\audit.jsonl")
_PRE_TIMEOUT_SECONDS = 5
_POST_TIMEOUT_SECONDS = 15

_audit_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _audit(source: str, action: str, result: str, details: dict) -> None:
    """Append a hook_error/hook line to the same audit.jsonl the Permission Center uses.

    Deliberately never writes the action arguments - a hook payload may carry paths or text we
    do not want duplicated into the log - only the script name and error class.
    """
    row = {
        "timestamp": _now_iso(),
        "source": source,
        "principal": "",
        "project": "",
        "action": action,
        "result": result,
        "details": details,
    }
    try:
        with _audit_lock:
            _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _AUDIT_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


_config_lock = threading.Lock()
_config_state: dict[str, object] = {"last_good": None, "bad_key": None}


def _read_json_file(path: Path) -> object:
    """JSON from UTF-8 (BOM or not) or UTF-16 (Windows PowerShell 5.1 Out-File) text."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return json.loads(raw.decode("utf-16"))
    return json.loads(raw.decode("utf-8-sig"))


def _config() -> dict:
    """hooks.json, re-read on every call. Absent file -> no hooks.

    A file that exists but cannot be parsed keeps the last config that did parse (hooks only
    ever block, so that is the conservative choice) and writes one 'hook_error' audit row per
    broken version of the file - it no longer switches every hook off silently.
    """
    key = None
    try:
        if not _HOOKS_CONFIG.exists():
            return {}
        stat = _HOOKS_CONFIG.stat()
        key = (str(_HOOKS_CONFIG), stat.st_mtime_ns, stat.st_size)
        data = _read_json_file(_HOOKS_CONFIG)
        if not isinstance(data, dict):
            raise ValueError("hooks.json is not a JSON object")
    except Exception as exc:
        with _config_lock:
            last_good = _config_state["last_good"]
            if key is not None and key != _config_state["bad_key"]:
                _config_state["bad_key"] = key
                _audit(
                    "local-tools",
                    "hooks_config",
                    "hook_error",
                    {"hook": _HOOKS_CONFIG.name, "error": type(exc).__name__,
                     "fallback": "last_good" if last_good else "none"},
                )
        return last_good if isinstance(last_good, dict) else {}
    with _config_lock:
        _config_state["last_good"] = data
    return data


def _matches(entry: dict, action: str) -> bool:
    pattern = str(entry.get("match", "")).strip()
    if not pattern:
        return True
    try:
        return re.search(pattern, action) is not None
    except re.error:
        return False


def run_pre_hooks(source: str, action: str, arguments: dict) -> dict | None:
    """Run every matching Pre hook. Return the first block decision, or None to allow."""
    hooks = _config().get("pre")
    if not isinstance(hooks, list):
        return None
    payload = json.dumps(
        {"source": source, "action": action, "arguments": arguments}, ensure_ascii=False
    )
    for entry in hooks:
        if not isinstance(entry, dict) or not _matches(entry, action):
            continue
        script = str(entry.get("script", "")).strip()
        if not script:
            continue
        try:
            completed = subprocess.run(
                [sys.executable, script],
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_PRE_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            _audit(source, action, "hook_error", {"hook": Path(script).name, "error": "timeout"})
            continue  # fail open
        except Exception as exc:
            _audit(source, action, "hook_error", {"hook": Path(script).name, "error": type(exc).__name__})
            continue  # fail open
        try:
            decision = json.loads((completed.stdout or "").strip() or "{}")
        except json.JSONDecodeError:
            _audit(source, action, "hook_error", {"hook": Path(script).name, "error": "bad_output"})
            continue  # fail open
        if isinstance(decision, dict) and str(decision.get("decision", "")).lower() == "block":
            return {
                "decision": "block",
                "reason": str(decision.get("reason", "")).strip() or "ถูกบล็อกโดย hook ของเจ้าของเครื่อง",
                "hook": Path(script).name,
            }
    return None


def _fire_post(script: str, payload: str) -> None:
    try:
        subprocess.run(
            [sys.executable, script],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_POST_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass  # post hooks are advisory; their failure is never surfaced


def run_post_hooks(source: str, action: str, arguments: dict, result: object) -> None:
    """Fire matching Post hooks in the background and return at once (fire-and-forget)."""
    hooks = _config().get("post")
    if not isinstance(hooks, list):
        return
    payload = json.dumps(
        {"source": source, "action": action, "arguments": arguments, "result": result},
        ensure_ascii=False,
        default=str,
    )
    for entry in hooks:
        if not isinstance(entry, dict) or not _matches(entry, action):
            continue
        script = str(entry.get("script", "")).strip()
        if not script:
            continue
        threading.Thread(target=_fire_post, args=(script, payload), daemon=True).start()
