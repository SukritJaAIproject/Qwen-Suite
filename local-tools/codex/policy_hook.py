"""Codex PreToolUse hook for the Codex-Qwen bridge: the gateway's command policy, for Codex jobs.

Codex 0.146 runs this before every tool call of a bridge job (config.template.toml,
[[hooks.PreToolUse]] matcher "*"), with one JSON object on stdin, for a shell command:
  {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "<PowerShell>"},
   "cwd": "<job folder>", "session_id": ..., "turn_id": ..., "tool_use_id": ..., "permission_mode": ...}
The command is the text Codex hands to `powershell.exe -Command`, so it is checked exactly like
run_command(program="powershell", args=["-Command", command]): command_policy.policy_check with
the built-in rules plus the owner's data\\permission-center\\command-rules.json. Two bridge-only
rules are added (BRIDGE_RULES): a job must not touch the suite's own safety files.

Deny = one JSON object on stdout, exit 0 (the reason is what the model sees):
  {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                          "permissionDecisionReason": "<Thai reason>"}}
Allow = no output, exit 0.

Verified on 0.146 on 2026-09-30 with a mock model: only that JSON blocks. Exit code 2, any other
non-zero exit, output that is not JSON, a program that does not exist and a hook timeout all
let the command RUN (fail-open), and an untrusted hook is skipped without a word. So:
  - this script never exits non-zero during a job and denies whenever it cannot decide
    (unreadable input, command_policy cannot be imported, any exception) - fail-closed inside;
  - the bridge runs `policy_hook.py --selftest` and checks the hook's trust through
    `codex app-server` hooks/list before every job, and refuses write jobs when either fails.
It is a block-list like run_command's, not a sandbox: a script already on disk can do anything.

Blocked calls go to %QWEN_CODEX_RUN_DIR%\\policy-denials.jsonl (the bridge sets it per job), so
get_codex_job can list them: Codex's own event stream does not record a hook denial.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TOOLS_DIR = HERE.parent          # local-tools: command_policy.py lives here
SUITE_ROOT = TOOLS_DIR.parent    # E:\localllm
POWERSHELL = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_TEXT_CHARS = 200_000

SUFFIX = " - ห้ามเลี่ยงด้วยคำสั่งอื่น ถ้าจำเป็นจริงให้บอกผู้ใช้ให้ทำเองที่เครื่อง"
REASON_UNREADABLE = "ตรวจคำสั่งไม่ได้ (ข้อมูลจาก Codex อ่านไม่ออก) จึงไม่รัน"
REASON_INTERNAL = "ตรวจคำสั่งกับนโยบายความปลอดภัยไม่ได้ (policy_hook ขัดข้อง) จึงไม่รัน"

# Bridge-only rules, on top of command_policy (which the chat gateway shares):
#  - the suite's safety layer itself: a full-access job editing command_policy.py, the rules,
#    this hook or the bridge settings would switch the protection off for every later job;
#  - the secret folders that command_policy does not name (.ssh .aws ...; .codex/.openclaw/.claude
#    and codex-home are already a built-in command_policy rule).
BRIDGE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)command_policy|policy_hook|command-rules\.json|permission[-_]center|permission_manager"
                r"|codex-bridge\.json|qwen-bridge\.rules|config\.template\.toml|[\\/]local-tools[\\/]+codex\b"),
     "ห้ามแก้หรืออ่านไฟล์ระบบความปลอดภัยของ Qwen Suite (นโยบายคำสั่ง/การตั้งค่า bridge)"),
    # Code that Python would import ahead of the policy (defence in depth next to python -I -S).
    (re.compile(r"(?i)site-packages|\.pth|sitecustomize|usercustomize|[\/]local-tools[\/]+[^\/\s\"'`]+\.py"),
     "ห้ามแก้ไฟล์ Python ของ Qwen Suite หรือ site-packages (อาจปิดระบบตรวจคำสั่ง)"),
    (re.compile(r"(?i)(?<![\w.-])\.(?:ssh|aws|azure|gnupg|kube)(?![\w-])|[\\/](?:login data|web data)(?=$|[\\/\s\"'`),;:])"),
     "เข้าถึงโฟลเดอร์ข้อมูลลับ (.ssh/.aws/ข้อมูลล็อกอินของเบราว์เซอร์)"),
]


def _load_policy():
    # Loaded by file path, never by putting local-tools on sys.path: a job could otherwise drop a
    # base64.py / threading.py there and replace the stdlib modules command_policy imports, and
    # a hook that "succeeds" with no output lets every command run. The hook itself is started
    # with python -I -S (no script dir, no site-packages, no .pth) for the same reason.
    import importlib.util

    module = sys.modules.get("command_policy")
    if module is None:
        spec = importlib.util.spec_from_file_location("command_policy", TOOLS_DIR / "command_policy.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["command_policy"] = module
        spec.loader.exec_module(module)
    return module


def _strings(value: Any, out: list[str], budget: list[int]) -> None:
    """Every string inside a tool input (non-shell tools), capped."""
    if budget[0] <= 0:
        return
    if isinstance(value, str):
        out.append(value[: budget[0]])
        budget[0] -= len(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out, budget)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _strings(item, out, budget)


def check(payload: dict[str, Any]) -> str | None:
    """Thai block reason for one PreToolUse call, or None to let it run."""
    event = str(payload.get("hook_event_name") or "PreToolUse")
    if event != "PreToolUse":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {"input": tool_input} if tool_input is not None else {}
    cwd = str(payload.get("cwd") or "") or None
    policy = _load_policy()
    command = tool_input.get("command")
    if isinstance(command, list) and command and all(isinstance(c, str) for c in command):
        # an argv list (other Codex shell tools): checked as that exact argv
        argv = [str(c) for c in command]
        display = " ".join(argv)
    elif isinstance(command, str):
        # shell_command: Codex runs `powershell.exe -Command '<command>'`
        argv = [POWERSHELL, "-NoProfile", "-Command", command]
        display = command
    else:
        argv, display = [], ""
    if argv:
        display = display[:MAX_TEXT_CHARS]
        reason = policy.policy_check(argv, display, cwd)
        if reason:
            return reason
        for pattern, why in BRIDGE_RULES:
            if pattern.search(display):
                return why
        return None
    # Any other tool (view_image, update_plan, an MCP tool if one is ever added): its string
    # arguments must not name a secret folder or the suite's safety files.
    texts: list[str] = []
    _strings(tool_input, texts, [MAX_TEXT_CHARS])
    for text in texts:
        for pattern, why in BRIDGE_RULES:
            if pattern.search(text):
                return why
        agent = re.search(r"(?i)(^|[\\/\s\"'~=])\.(codex|openclaw|claude)([\\/\s\"']|$)|[\\/]codex-home([\\/\s\"']|$)", text)
        if agent:
            return "เข้าถึงโฟลเดอร์ข้อมูลลับของ agent (.codex/.openclaw/.claude)"
    return None


def _clip(text: Any, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + " ..."


def _record(payload: dict[str, Any], reason: str) -> None:
    """One line per blocked call in the job's run folder (best effort, never raises)."""
    try:
        run_dir = os.environ.get("QWEN_CODEX_RUN_DIR", "").strip()
        target = Path(run_dir) / "policy-denials.jsonl" if run_dir and Path(run_dir).is_dir() else \
            SUITE_ROOT / "logs" / "codex-bridge" / "policy-denials.jsonl"
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "job": os.environ.get("QWEN_CODEX_JOB_ID", ""),
            "tool": str(payload.get("tool_name") or ""),
            "command": _clip(tool_input.get("command") if isinstance(tool_input.get("command"), str)
                             else json.dumps(tool_input, ensure_ascii=False), 300),
            "reason": reason,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def deny_output(reason: str) -> str:
    """The exact stdout that makes Codex 0.146 refuse the call (ASCII-only JSON)."""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "นโยบายความปลอดภัยของเครื่องไม่อนุญาต: " + reason + SUFFIX,
    }}, ensure_ascii=True)


def decide(raw: bytes) -> tuple[str | None, dict[str, Any]]:
    """(reason or None, payload) for raw hook stdin; never raises."""
    try:
        payload = json.loads(raw[:MAX_INPUT_BYTES].decode("utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("not an object")
    except Exception:
        return REASON_UNREADABLE, {}
    try:
        return check(payload), payload
    except Exception:
        return REASON_INTERNAL, payload


SELFTEST_CASES = [
    # (tool_input command, must be blocked) - strings only, nothing is executed
    ("Get-ChildItem -Force", False),
    ("git status", False),
    ("python -m pytest -q", False),
    ("cmdkey /list", True),
    ("Get-Content C:\\Users\\EGAT\\.codex\\auth.json", True),
    ("Remove-Item -Recurse -Force .\\build", True),
    ("Get-Process ollama | Stop-Process", True),
    ("Set-Content E:\\localllm\\local-tools\\command_policy.py ''", True),
]


def selftest() -> dict[str, Any]:
    results = []
    ok = True
    for command, blocked in SELFTEST_CASES:
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command},
                   "cwd": str(SUITE_ROOT)}
        reason, _ = decide(json.dumps(payload).encode("utf-8"))
        good = bool(reason) == blocked and reason not in (REASON_INTERNAL, REASON_UNREADABLE)
        ok = ok and good
        results.append({"command": command, "blocked": bool(reason), "expected_blocked": blocked, "ok": good})
    return {"ok": ok, "python": sys.executable, "cases": results}


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        result = selftest()
        sys.stdout.write(json.dumps(result, ensure_ascii=True))
        return 0 if result["ok"] else 1
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    except Exception:
        raw = b""
    reason, payload = decide(raw)
    if reason:
        _record(payload, reason)
        sys.stdout.write(deny_output(reason))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except Exception:  # last resort: still a deny, still exit 0 (non-zero would let it run)
        sys.stdout.write(deny_output(REASON_INTERNAL))
        code = 0
    sys.exit(code)
