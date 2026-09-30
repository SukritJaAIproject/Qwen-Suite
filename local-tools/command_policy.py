"""Parsed-command policy for the local-tools gateway.

The gateway's original guard, server.py:_blocked_command, ran a handful of regexes on the
single joined argv string. That was bypassable: wrapping a command in
`powershell -Command "..."`, quoting, backtick/caret escapes, base64 -EncodedCommand, or
PowerShell aliases (rm/ri/rd/spps...) all sailed past the `(^|\\s)keyword` anchors because
the keyword no longer sat at a token boundary in the joined string.

This module keeps those same regexes working (they run here, on the display string) and adds
a parsing layer: it pulls the real statement(s) out of powershell/cmd/python wrappers (also
when one wrapper is nested in another, or a python -c passes a command line as a string),
decodes -EncodedCommand, splits on separators, strips quotes/backticks/carets, treats
{ ( [ , as token boundaries, reads `-Switch:$true` as `-Switch`, maps the common destructive
aliases to their canonical cmdlet - only where a command can start, so `git rm -r` or a
commit message is not mistaken for Remove-Item - and runs every rule against each extracted
piece as well as the whole unsplit payload (so `Get-Process ollama | Stop-Process` is seen).

A powershell -File script is read (relative paths against the command's cwd) but only the
credential-store and disk-destruction rules run on its body, comment lines skipped: its own
cleanup code (Remove-Item -Recurse on a temp dir) is normal, and the same script started via
-Command "& 'x.ps1'" is never read anyway. It is a hard block-list, not a sandbox: a .py/.ps1
already on disk can still do anything. There is no 'ask' outcome here - the owner's standing
rule is that chat channels run without a confirmation prompt, so this layer only ever blocks
or passes.

The rules file is read as UTF-8 with or without a BOM, or UTF-16 (what Windows PowerShell
5.1 Set-Content/Out-File write). If it cannot be parsed, the last rules that did parse stay
in force (or the shipped defaults, on a fresh start) and one 'rules_error' row is written to
audit.jsonl, so a broken edit never silently drops back to the built-in rules alone.

policy_statements(argv, cwd=None) -> list[str]   the normalized statements pulled from argv
policy_check(argv, display, cwd=None) -> str|None   a Thai block reason, or None to allow
"""

from __future__ import annotations

import base64
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Built-in rules. The first four are the server.py:_blocked_command regexes, duplicated
# here verbatim (patterns unchanged so behaviour is preserved) with Thai reasons
# so server.py can delegate to this module. They ALWAYS run, in addition to the
# user-editable rules, so emptying command-rules.json cannot re-open them.
# ---------------------------------------------------------------------------
# Credential stores stay off-limits to automation (the owner's standing rule).
_CREDENTIAL_RULE = (
    r"(?i)\b(cmdkey|vaultcmd|keymgr)\b|get-storedcredential|credentialmanager|cryptunprotectdata|protecteddata\]::unprotect|[\\/]microsoft[\\/]+(protect|credentials|vault)\b|login data|\.openclaw[\\/]+credentials\b|[\\/]data[\\/]+credentials\b|\bkeyring\b|passwordvault",
    "เข้าถึงที่เก็บรหัสผ่าน/ข้อมูลลับ",
)
# Disk and backup destruction.
_DISK_DESTRUCTION_RULE = (
    r"(?i)\b(vssadmin|wbadmin)\b.*\bdelete\b|\b(clear-disk|remove-partition|format-volume|initialize-disk)\b",
    "ทำลายดิสก์หรือข้อมูลสำรอง",
)
BASE_RULES: list[tuple[str, str]] = [
    (r"(?i)(^|\s)(format|diskpart|bcdedit|cipher\s+/w)(\s|$)", "แก้ไขดิสก์/ระบบ (format/diskpart/bcdedit)"),
    (r"(?i)(^|\s)(shutdown|restart-computer|stop-computer)(\s|$)", "สั่งปิดหรือรีสตาร์ทเครื่อง"),
    (
        r"(?i)(^|\s)(reg\s+delete|remove-item\b.*-recurse|del\b.*?/s|rmdir\b.*?/s|rd\b.*?/s)(\s|$)",
        "ลบแบบซ้อนหรือลบระบบ",
    ),
    (r"(?i)(^|\s)(taskkill|stop-process)\b.*(ollama|open-webui|python)", "หยุดบริการ AI ในเครื่อง"),
    _CREDENTIAL_RULE,
    # Agent state folders hold tokens and full transcripts (~/.codex auth.json, openclaw.json's
    # gateway token, the bridge's codex-home); the file tools already refuse them via
    # server.py BLOCKED_PARTS, so a shell command must not be the way around that.
    (
        r"(?i)(^|[\\/\s\"'~=])\.(codex|openclaw|claude)([\\/\s\"']|$)|[\\/]codex-home([\\/\s\"']|$)|\.webui_secret_key",
        "เข้าถึงโฟลเดอร์ข้อมูลลับของ agent (.codex/.openclaw/.claude)",
    ),
    _DISK_DESTRUCTION_RULE,
    # The pipeline form names the process first: Get-Process ollama | Stop-Process,
    # gps python* | kill, Get-Process | Where-Object {...'node'} | Stop-Process,
    # (Get-Process ollama).Kill(). Checked on the unsplit payload; stays inside one statement.
    (
        r"(?i)\b(?:get-process|gps|ps)\b[^\n;]*?\b(?:ollama|open-?webui|python|pythonw|node|openclaw|cloudflared|llama-server)"
        r"(?:[^\n;]*?\|[^\n;]*?\b(?:stop-process|spps|kill)\b|[^\n;]*?\.kill\s*\()",
        "หยุดบริการ AI ในเครื่อง",
    ),
]
# The only rules run on the body of a powershell -File script (see module docstring).
SCRIPT_BODY_RULES: list[tuple[str, str]] = [_CREDENTIAL_RULE, _DISK_DESTRUCTION_RULE]

_DATA_RULES_PATH = Path(r"E:\localllm\data\permission-center\command-rules.json")
_DEFAULT_RULES_PATH = Path(__file__).with_name("default-command-rules.json")
_ALLOWED_ROOTS_CONFIG = Path(__file__).with_name("allowed-roots.json")
_AUDIT_PATH = Path(r"E:\localllm\data\permission-center\audit.jsonl")

_PS1_MAX_BYTES = 64 * 1024
_MAX_NESTING = 3

_POWERSHELLS = {"powershell.exe", "powershell", "pwsh.exe", "pwsh"}
_CMDS = {"cmd.exe", "cmd"}
_NESTABLE = _POWERSHELLS | _CMDS | {"python", "python.exe", "pythonw.exe", "py", "py.exe"}

# Destructive aliases -> canonical form, so the recurse/service rules match the same way
# they would on a native cmdlet call. Only substituted where a command can start (statement
# start, after | ; & or an opening { / ( ), never inside arguments or message text.
_ALIASES = {
    "rm": "remove-item",
    "ri": "remove-item",
    "del": "remove-item",
    "erase": "remove-item",
    "rd": "remove-item",
    "rmdir": "remove-item",
    "kill": "stop-process",
    "spps": "stop-process",
}
_ALIAS_PATTERNS = [
    (re.compile(rf"((?:^|[{{(;&|\n])\s*){re.escape(a)}(?![\w-])", re.IGNORECASE), c) for a, c in _ALIASES.items()
]

# Statement separators inside one wrapped command line.
_SPLIT = re.compile(r"&&|\|\||[;&|\n\r]")
_STRIP_CHARS = str.maketrans("", "", "\"'`^")
# Script-block, sub-expression, array and argument-list punctuation: a keyword right after
# one of these is still at a token boundary ({Remove-Item ..}, (rmdir /s x), ['rd','/s']).
_BOUNDARY = re.compile(r"[{}()\[\],]")
# -Recurse:$true (or :1) is the same switch as -Recurse; -Recurse:$false is left alone.
_SWITCH_TRUE = re.compile(r"(\s-[a-z][\w-]*):(?:\$true|1)(?=[\s{}()\[\],;|&]|$)", re.IGNORECASE)
# Python string literals in a `python -c` payload; the command lines it hands to
# os.system/subprocess live inside them.
_PY_STRING = re.compile(
    r"""[rRbBuUfF]{0,2}("{3}[\s\S]*?"{3}|'{3}[\s\S]*?'{3}|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')"""
)
# Comment lines in a -File script body (PowerShell # and <# #>, cmd REM and ::).
_BLOCK_COMMENT = re.compile(r"<#[\s\S]*?#>")
_LINE_COMMENT = re.compile(r"^\s*(?:#|::|@?rem(?:\s|$))", re.IGNORECASE)
# powershell.exe / pwsh parameters that take a value (so the value is not read as the command).
_PS_VALUE_PARAMS = (
    "executionpolicy",
    "windowstyle",
    "outputformat",
    "inputformat",
    "version",
    "psconsolefile",
    "configurationname",
    "workingdirectory",
    "settingsfile",
    "custompipename",
    "encodedarguments",
)
_PS_VALUE_SHORT = {"ep", "ex", "w", "wd", "v", "of", "if"}

_lock = threading.Lock()
_cache: dict[str, object] = {"mtime": None, "path": None, "rules": None, "last_good": None}
_BASE_COMPILED = [(re.compile(p), r) for p, r in BASE_RULES]
_SCRIPT_BODY_COMPILED = [(re.compile(p), r) for p, r in SCRIPT_BODY_RULES]


def _normalize(text: str) -> str:
    """Lower-case and drop the quote/backtick/caret armour models hide keywords behind."""
    return text.translate(_STRIP_CHARS).lower()


def _alias_map(text: str) -> str:
    for pattern, canon in _ALIAS_PATTERNS:
        text = pattern.sub(lambda m, c=canon: m.group(1) + c, text)
    return text


def _variants(text: str) -> list[str]:
    """The text itself plus its boundary-spaced, -Switch:$true-folded form (when different)."""
    spaced = _BOUNDARY.sub(" ", _SWITCH_TRUE.sub(r"\1", text))
    return [text, spaced] if spaced != text else [text]


def _allowed_roots() -> list[Path]:
    """Roots a -File script may be read from. Mirrors server.py's list without importing it."""
    roots: list[Path] = []
    try:
        if _ALLOWED_ROOTS_CONFIG.exists():
            data = json.loads(_ALLOWED_ROOTS_CONFIG.read_text(encoding="utf-8-sig"))
            entries = data.get("roots") if isinstance(data, dict) else data
            if isinstance(entries, list):
                roots = [Path(str(e)) for e in entries if str(e).strip()]
    except Exception:
        roots = []
    if not roots:
        roots = [Path.home(), Path(r"E:\localllm"), Path(r"E:\qwen-projects"), Path(r"C:\Users\EGAT\Documents")]
    resolved: list[Path] = []
    for root in roots:
        try:
            candidate = root.resolve()
        except Exception:
            continue
        if candidate.exists():
            resolved.append(candidate)
    return resolved


def _read_ps1(raw_path: str, cwd: str | Path | None = None) -> str:
    """Best-effort read of a -File target, capped and confined to the allowed roots. Fail open.

    A relative path is resolved against the command's cwd (where PowerShell will look for it),
    not against this server process's working directory.
    """
    try:
        path = Path(raw_path.strip().strip('"').strip("'")).expanduser()
        if not path.is_absolute() and cwd:
            path = Path(str(cwd)) / path
        path = path.resolve(strict=False)
        if path.suffix.lower() not in {".ps1", ".psm1", ".cmd", ".bat"}:
            return ""
        roots = _allowed_roots()
        if not any(path == r or r in path.parents for r in roots):
            return ""
        if not path.exists():
            return ""
        with path.open("rb") as handle:
            raw = handle.read(_PS1_MAX_BYTES)
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16", errors="replace")
        return raw.decode("utf-8-sig", errors="replace")
    except Exception:
        return ""


def _strip_comments(body: str) -> str:
    body = _BLOCK_COMMENT.sub("\n", body)
    return "\n".join(line for line in body.splitlines() if not _LINE_COMMENT.match(line))


def _decode_encoded(value: str) -> str:
    try:
        return base64.b64decode(value.strip().strip('"')).decode("utf-16-le", errors="replace")
    except Exception:
        return ""


def _flag(token: str) -> str:
    """Classify a wrapper flag token by its (abbreviated) name. '' when it is not one we care about."""
    t = token.lstrip("-/").lower()
    if not t:
        return ""
    if t.startswith("encodedcommand") or t.startswith("encoded") or t in {"e", "en", "enc", "enco", "encod", "encode", "ec"}:
        return "encoded"
    if t.startswith("command") or t in {"c", "co", "com", "comm", "comma", "comman"}:
        return "command"
    if t.startswith("file") or t in {"f", "fi", "fil"}:
        return "file"
    return ""


def _takes_value(token: str) -> bool:
    t = token.lstrip("-/").lower()
    return t in _PS_VALUE_SHORT or (len(t) >= 3 and any(p.startswith(t) for p in _PS_VALUE_PARAMS))


def _py_literals(code: str) -> str:
    parts: list[str] = []
    for match in _PY_STRING.finditer(code):
        literal = match.group(1)
        q = 3 if literal[:3] in ('"""', "'''") else 1
        parts.append(literal[q:-q])
    return " ".join(parts)


def _payloads(argv: list[str], cwd: str | Path | None = None, depth: int = 0) -> list[tuple[str, str]]:
    """Pull the payload string(s) out of a wrapper call, before splitting/normalizing.

    Returns (kind, text) pairs. kind is 'shell' (a powershell/cmd command line: aliases apply),
    'literal' (the string literals of a python -c payload), 'code' (python -c/-m text),
    'script' (a -File body: only SCRIPT_BODY_RULES apply) or 'args' (the joined arguments).
    """
    if not argv:
        return []
    program = Path(str(argv[0]).strip().strip('"').strip("'")).name.lower()
    args = [str(a) for a in argv[1:]]
    out: list[tuple[str, str]] = []

    if program in _POWERSHELLS:
        i = 0
        while i < len(args):
            tok = args[i]
            kind = _flag(tok) if tok.startswith(("-", "/")) else ""
            if kind == "encoded" and i + 1 < len(args):
                out.append(("shell", _decode_encoded(args[i + 1])))
                i += 2
                continue
            if kind == "command":
                # everything after -Command is the script
                out.append(("shell", " ".join(args[i + 1 :])))
                break
            if kind == "file":
                # everything after -File <path> is that script's own arguments
                if i + 1 < len(args):
                    out.append(("script", _read_ps1(args[i + 1], cwd)))
                break
            if tok.startswith(("-", "/")):
                i += 2 if _takes_value(tok) else 1
                continue
            # No -Command flag: PowerShell treats the first bare token and the rest as -Command.
            out.append(("shell", " ".join(args[i:])))
            break
    elif program in _CMDS:
        for i, tok in enumerate(args):
            if tok.lstrip("-/").lower() in {"c", "k"}:
                out.append(("shell", " ".join(args[i + 1 :])))
                break
    elif program.startswith("python") or program in {"py.exe", "py"}:
        for i, tok in enumerate(args):
            if tok in {"-c", "-m"} and i + 1 < len(args):
                code = " ".join(args[i + 1 :])
                out.append(("code", code))
                if tok == "-c":
                    out.append(("literal", _py_literals(code)))
                break

    # A wrapper inside the payload (cmd /c powershell -e ..., subprocess.run(['cmd','/c',...]))
    # is unwrapped the same way, a few levels deep.
    if depth < _MAX_NESTING:
        nested: list[tuple[str, str]] = []
        for kind, text in out:
            if kind not in {"shell", "literal"}:
                continue
            for part in _SPLIT.split(text):
                tokens = [t.strip("\"'") for t in part.split()]
                if tokens and Path(tokens[0]).name.lower() in _NESTABLE:
                    nested.extend(_payloads(tokens, cwd, depth + 1))
        out.extend(nested)

    # Always include the joined arguments as a fallback statement (covers git, ffmpeg, etc.).
    out.append(("args", " ".join(args)))
    return [(k, s) for k, s in out if s]


def _statements(kind: str, text: str) -> list[str]:
    """Normalized forms of one payload: the whole thing plus each separator-split piece."""
    raws = [text] + _SPLIT.split(text)
    result: list[str] = []
    for raw in raws:
        norm = _normalize(raw).strip()
        if not norm:
            continue
        forms = [norm]
        if kind == "shell":
            mapped = _alias_map(norm)
            if mapped != norm:
                forms.append(mapped)
        for form in forms:
            result.extend(_variants(form) if kind in {"shell", "literal"} else [form])
    return result


def policy_statements(argv: list[str], cwd: str | Path | None = None) -> list[str]:
    """Normalized, separator-split statements extracted from an argv list."""
    pieces: list[str] = []
    for kind, raw in _payloads(argv, cwd):
        if kind == "script":
            pieces.extend(_normalize(line).strip() for line in _strip_comments(raw).splitlines())
            continue
        for part in _SPLIT.split(raw):
            norm = _normalize(part).strip()
            if norm:
                pieces.append(norm)
    # de-dup while keeping order
    seen: set[str] = set()
    result: list[str] = []
    for p in pieces:
        if p and p not in seen:
            seen.add(p)
            result.append(p)
    return result


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _audit_rules_error(path: Path, error: str, fallback: str, count: int = 0) -> None:
    """One audit.jsonl row per broken rules-file version (never the file's content)."""
    details: dict[str, object] = {"file": path.name, "error": error, "fallback": fallback}
    if count:
        details["count"] = count
    row = {
        "timestamp": _now_iso(),
        "source": "local-tools",
        "principal": "",
        "project": "",
        "action": "command_rules",
        "result": "rules_error",
        "details": details,
    }
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_json_file(path: Path) -> object:
    """JSON from UTF-8 (BOM or not) or UTF-16 (PowerShell 5.1 Out-File) text."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return json.loads(raw.decode("utf-16"))
    return json.loads(raw.decode("utf-8-sig"))


def _compile_user_rules(path: Path) -> tuple[list[tuple[re.Pattern[str], str]], int]:
    """The file's forbidden rules; raises when the file itself cannot be read or parsed."""
    data = _read_json_file(path)
    if not isinstance(data, dict):
        raise ValueError("rules file is not a JSON object")
    compiled: list[tuple[re.Pattern[str], str]] = []
    bad = 0
    for row in data.get("forbidden", []) or []:
        if not isinstance(row, dict):
            bad += 1
            continue
        pattern = str(row.get("pattern", "")).strip()
        reason = str(row.get("reason", "")).strip() or "นโยบายความปลอดภัย"
        if not pattern:
            continue
        try:
            compiled.append((re.compile(pattern), reason))
        except re.error:
            bad += 1  # a malformed user rule is skipped, not fatal
    return compiled, bad


def _load_rules() -> list[tuple[re.Pattern[str], str]]:
    """BASE_RULES plus the user-editable forbidden list, recompiled when the file changes."""
    path = _DATA_RULES_PATH if _DATA_RULES_PATH.exists() else _DEFAULT_RULES_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    with _lock:
        if _cache["rules"] is not None and _cache["mtime"] == mtime and _cache["path"] == str(path):
            return _cache["rules"]  # type: ignore[return-value]
        compiled: list[tuple[re.Pattern[str], str]] = list(_BASE_COMPILED)
        try:
            user, bad = _compile_user_rules(path)
            _cache["last_good"] = user
            if bad:
                _audit_rules_error(path, "invalid_pattern", "skipped", bad)
        except Exception as exc:
            # A broken file (BOM-less UTF-16 junk, a JSON syntax error such as a single-backslash
            # regex escape) keeps the last rules that parsed, else the shipped defaults - never
            # just BASE_RULES - and says so once in audit.jsonl.
            if _cache["last_good"] is not None:
                user, fallback = _cache["last_good"], "last_good"  # type: ignore[assignment]
            else:
                user, fallback = [], "base_only"
                if path != _DEFAULT_RULES_PATH:
                    try:
                        user, _ = _compile_user_rules(_DEFAULT_RULES_PATH)
                        fallback = "defaults"
                    except Exception:
                        pass
            _audit_rules_error(path, type(exc).__name__, fallback)
        compiled.extend(user)  # type: ignore[arg-type]
        _cache["rules"] = compiled
        _cache["mtime"] = mtime
        _cache["path"] = str(path)
        return compiled


def policy_check(argv: list[str], display: str, cwd: str | Path | None = None) -> str | None:
    """Return a Thai block reason if the command is forbidden, else None.

    Every rule runs against the raw and normalized display string and against every payload
    extracted from the argv (whole and split, with the alias/boundary forms for shell
    payloads). A -File script body gets SCRIPT_BODY_RULES only. Pass `cwd` (the command's
    working directory) so a relative -File path is found where PowerShell will find it.
    """
    rules = _load_rules()
    targets: list[str] = [display, _normalize(display)]
    bodies: list[str] = []
    for kind, raw in _payloads(argv, cwd):
        if kind == "script":
            body = _normalize(_strip_comments(raw))
            if body.strip():
                bodies.append(body)
            continue
        targets.extend(_statements(kind, raw))
    seen: set[str] = set()
    unique = [t for t in targets if not (t in seen or seen.add(t))]

    for pattern, reason in rules:
        for target in unique:
            if pattern.search(target):
                return reason
    for pattern, reason in _SCRIPT_BODY_COMPILED:
        for body in bodies:
            if pattern.search(body):
                return reason
    return None


_CMDLINE_TOKEN = re.compile(r'"[^"]*"|\S+')


def policy_check_text(text: str) -> str | None:
    """String-only variant for callers that only have a joined command line (e.g. a delegating
    server.py:_blocked_command). The line is split back into an argv (list2cmdline quoting)
    and checked by policy_check."""
    argv = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t for t in _CMDLINE_TOKEN.findall(text)]
    return policy_check(argv, text)
