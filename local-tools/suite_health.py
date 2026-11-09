"""Read-only health report for the whole Qwen Suite - added 2026-09-30.

One module serves three front ends so they always agree: the suite_status gateway action
(Telegram /qstatus, Open WebUI), the /status page on :8765 and Test-QwenSuite.ps1
(which runs this file directly, so it still works when :8765 is down).

Rules every check follows:
- read-only: no restarts, no writes, nothing that makes Ollama load or run a model;
- its own time limit; a failure becomes status "unknown" instead of an exception, and the
  whole report is cut off at DEADLINE_S so a caller never waits longer than ~15 s;
- a slow OpenClaw CLI call (Node loads the whole CLI: ~3 s warm, 6 s+ cold, over 10 s beside
  the other heavy checks) keeps running in the background up to OPENCLAW_CLI_TIMEOUT_S; the
  report waits only until its deadline and otherwise shows the last good answer with its time,
  and the late answer is kept for the next report instead of being thrown away;
- the output carries statuses, counts and times only - never tokens, keys, phone numbers,
  chat/account IDs or free config values (free text from logs passes through _redact).

Usage:  python suite_health.py [--json] [--only ports,ollama,...] [--fresh]
"""
from __future__ import annotations

import argparse
import ast
import ctypes
import json
import os
import re
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

SUITE_ROOT = Path(__file__).resolve().parent.parent
LOGS = SUITE_ROOT / "logs"
OLLAMA_LOG = LOGS / "ollama-server.log"
OLLAMA = "http://127.0.0.1:11434"
LOCAL_TOOLS_MCP = "http://127.0.0.1:8765/mcp"
STATUS_PAGE = "http://127.0.0.1:8765/status"
OPENCLAW_RUNTIME = SUITE_ROOT / "openclaw-runtime"
WEBUI_DB = SUITE_ROOT / "webui-data" / "webui.db"
README = SUITE_ROOT / "README.md"
PYTHON = SUITE_ROOT / "webui-venv" / "Scripts" / "python.exe"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
PARITY_SCRIPT = Path(os.environ.get(
    "QWEN_PARITY_SCRIPT",
    r"C:\Users\EGAT\Documents\Codex\2026-08-30\ll\installer\QwenSuite\Check-PayloadParity.py",
))

# What the suite is supposed to run with. Path B (chosen 2026-09-29): the latest GeForce
# driver for the RTX eGPU and the Quadro P620 disabled, so without the eGPU Ollama is
# CPU-only; one loaded model at a time, because switching models must not keep two 18 GB
# models competing for the same CPU.
EXPECTED_MAX_LOADED = int(os.environ.get("QWEN_EXPECTED_MAX_LOADED", "2") or 2)  # README Ollama table: 2 on purpose (small model beside the main one)
TARGET_GPU_PATH = (os.environ.get("QWEN_GPU_PATH", "B") or "B").strip().upper()

DEADLINE_S = 13.0
SCRIPT_DEADLINE_S = 30.0  # console run (Test-QwenSuite.ps1): nothing is kept between runs, so wait longer
OPENCLAW_CLI_TIMEOUT_S = 45.0  # hard limit of one OpenClaw CLI call (the process tree is killed after it)
OPENCLAW_STALE_S = 20 * 60  # a last good OpenClaw answer older than this is not shown any more
DISK_WARN_GB = 10.0
DISK_BAD_GB = 3.0
OAUTH_WARN_DAYS = 14
NODE_REPL_WARN = 5

PORTS = [
    (3000, "Open WebUI", True),
    (8765, "Local Tools (MCP เต็ม)", True),
    (8766, "Gateway ของ Open WebUI", True),
    (8767, "Gateway ของ OpenClaw", True),
    (8770, "Model Router", True),
    (8771, "Reranker", False),
    (8780, "Internet Gateway", False),
    (11434, "Ollama", True),
    (18789, "OpenClaw Gateway", True),
]

OK, WARN, BAD, UNKNOWN = "ok", "warn", "bad", "unknown"
RANK = {OK: 0, UNKNOWN: 1, WARN: 2, BAD: 3}
STATUS_TH = {OK: "ปกติ", WARN: "ควรดู", BAD: "มีปัญหา", UNKNOWN: "ตรวจไม่ได้"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- helpers

_REDACTIONS = [
    (re.compile(r"(?i)\bbot\d{5,}:[\w-]{20,}"), "bot<ซ่อน>"),
    (re.compile(r"\b\d{5,}:[\w-]{20,}"), "<ซ่อน>"),
    (re.compile(r"(?i)\b(bearer|basic)\s+\S+"), r"\1 <ซ่อน>"),
    (re.compile(r"(?i)\b(token|api[_-]?key|secret|password|passwd|authorization|cookie)\b(\s*[=:]\s*)[^\s,;\"']+"), r"\1=<ซ่อน>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<อีเมล>"),
    (re.compile(r"@\w{3,}bot\b", re.I), "@<bot>"),
    (re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<id>"),
    (re.compile(r"\b(?:sha256[:-])?[0-9a-f]{32,}\b", re.I), "<hash>"),
]
# long random-looking runs (keys, session tokens); a long lowercase name such as
# "skill-collection-review-qwenlocal" has no digit and no mixed case, so it stays readable
_RE_LONG = re.compile(r"[A-Za-z0-9_\-+=]{24,}")


def _long_repl(match: re.Match) -> str:
    text = match.group(0)
    mixed = any(c.islower() for c in text) and any(c.isupper() for c in text)
    return "<ซ่อน>" if any(c.isdigit() for c in text) or mixed else text
# phone numbers, Telegram/WhatsApp ids and jids: 8+ digits, optionally with separators
_RE_DIGITS = re.compile(r"\+?\d[\d \-]{6,}\d")


def _digits_repl(match: re.Match) -> str:
    text = match.group(0)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) or sum(ch.isdigit() for ch in text) < 8:
        return text  # a date or a short number is not an identifier
    return "<ตัวเลข>"


def _redact(text: Any, limit: int = 180) -> str:
    """Free text from logs/CLI output: strip anything that could be a secret or an id."""
    s = " ".join(str(text or "").split())
    for pattern, repl in _REDACTIONS:
        s = pattern.sub(repl, s)
    s = _RE_LONG.sub(_long_repl, s)
    s = _RE_DIGITS.sub(_digits_repl, s)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _now() -> float:
    return time.time()


def _fmt_time(ts: float | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return None


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "ไม่ทราบ"
    seconds = int(max(0, round(seconds)))
    if seconds < 60:
        return f"{seconds} วินาที"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} นาที" + (f" {sec} วินาที" if sec and minutes < 10 else "")
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} ชม." + (f" {minutes} นาที" if minutes else "")
    return f"{hours // 24} วัน"


def _iso_to_ts(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _http_json(url: str, timeout: float, body: dict | None = None, headers: dict | None = None) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})},
                                 method="POST" if data is not None else "GET")
    # never pick up a system proxy for loopback
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _port_open(port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _run(argv: list[str], timeout: float, cwd: str | None = None) -> tuple[int | None, str, str]:
    """Run a child with its output in files, not pipes, so a grandchild that inherited the
    handle can never keep us waiting past the timeout; kill the whole tree on timeout."""
    with tempfile.TemporaryDirectory(prefix="qwen-health-", ignore_cleanup_errors=True) as tmp:
        out_path, err_path = Path(tmp) / "out.txt", Path(tmp) / "err.txt"
        with out_path.open("wb") as out, err_path.open("wb") as err:
            env = dict(os.environ, PYTHONIOENCODING="utf-8", NO_COLOR="1", FORCE_COLOR="0")
            proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    env=env, creationflags=NO_WINDOW)
            try:
                code: int | None = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                               creationflags=NO_WINDOW)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                code = None
        return (code, out_path.read_bytes().decode("utf-8", "replace"),
                err_path.read_bytes().decode("utf-8", "replace"))


def _json_from_output(text: str) -> Any:
    """CLI output may carry a banner before the JSON document."""
    text = text.lstrip("\ufeff")
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in output")
    return json.loads(text[min(starts):])


def _openclaw_argv(*args: str) -> list[str]:
    node = OPENCLAW_RUNTIME / "node.exe"
    entry = OPENCLAW_RUNTIME / "node_modules" / "openclaw" / "openclaw.mjs"
    if node.is_file() and entry.is_file():
        # same program openclaw.cmd starts, minus the cmd.exe wrapper (one process to kill)
        return [str(node), str(entry), *args]
    return [str(OPENCLAW_RUNTIME / "openclaw.cmd"), *args]


def _result(status: str, summary: str, **details: Any) -> dict[str, Any]:
    return {"status": status, "status_th": STATUS_TH[status], "summary": summary, "details": details}


class _Shared:
    """Values two checks need (Ollama /api/ps, the parsed server log), computed once per run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {}

    def get(self, key: str, fn: Callable[[], Any]) -> Any:
        with self._lock:
            if key not in self._values:
                try:
                    self._values[key] = (True, fn())
                except Exception as exc:  # the caller decides what a failure means
                    self._values[key] = (False, exc)
            ok, value = self._values[key]
        if not ok:
            raise value
        return value


class _SlowCall:
    """One run per key at a time, in its own thread. A run outlives the report that started
    it, so an answer that comes after the report's deadline is kept for the next report."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[str, threading.Event] = {}
        self._last: dict[str, tuple[float, bool, Any]] = {}  # key -> (finished at, ok, value or error)
        self._good: dict[str, tuple[float, Any]] = {}  # key -> (finished at, value) of the last success

    def _work(self, key: str, fn: Callable[[], Any], done: threading.Event) -> None:
        try:
            ok, value = True, fn()
        except Exception as exc:  # kept and raised to whoever asks
            ok, value = False, exc
        with self._lock:
            stamp = _now()
            self._last[key] = (stamp, ok, value)
            if ok:
                self._good[key] = (stamp, value)
            if self._running.get(key) is done:
                del self._running[key]
        done.set()

    def start(self, key: str, fn: Callable[[], Any]) -> threading.Event:
        with self._lock:
            run = self._running.get(key)
            if run is None:
                run = self._running[key] = threading.Event()
                threading.Thread(target=self._work, args=(key, fn, run), name="suite-health-" + key,
                                 daemon=True).start()
            return run

    def get(self, key: str, fn: Callable[[], Any], wait_s: float, label: str,
            reuse_s: float = 0.0) -> tuple[Any, float, bool]:
        """(value, finished at, live). live=False means the run is still going and this is the
        last good answer (at most OPENCLAW_STALE_S old); with nothing to show the call raises
        the newest error or a TimeoutError. reuse_s > 0 accepts an answer that finished that
        recently instead of starting a run."""
        with self._lock:
            last = self._last.get(key)
            if reuse_s > 0 and last and last[1] and _now() - last[0] <= reuse_s:
                return last[2], last[0], True
        run = self.start(key, fn)
        if run.wait(max(0.0, wait_s)):
            with self._lock:
                stamp, ok, value = self._last[key]
            if ok:
                return value, stamp, True
            raise value
        with self._lock:
            last, good = self._last.get(key), self._good.get(key)
        if last and last[1] and good and _now() - good[0] <= OPENCLAW_STALE_S:
            return good[1], good[0], False
        if last and not last[1]:
            raise TimeoutError(f"{label} ยังไม่ตอบภายใน {wait_s:.0f} วินาที; ครั้งก่อนเสีย: "
                               + _redact(f"{type(last[2]).__name__}: {last[2]}", 100))
        raise TimeoutError(f"{label} ยังไม่ตอบภายใน {wait_s:.0f} วินาที "
                           "(ยังถามต่อเบื้องหลัง ผลจะขึ้นในการตรวจรอบถัดไป)")

    def wait_running(self, timeout: float) -> None:
        """Script mode: let runs still going finish (their own timeout bounds this) so no Node
        process is left behind when the interpreter exits."""
        end = time.monotonic() + timeout
        with self._lock:
            runs = list(self._running.values())
        for run in runs:
            run.wait(max(0.0, end - time.monotonic()))


_SLOW = _SlowCall()


def _wait_left(ctx: dict, reserve: float = 1.0) -> float:
    """How long a check may still wait for a slow helper and finish before the report's deadline."""
    deadline_at = ctx.get("deadline_at")
    if deadline_at is None:
        return DEADLINE_S - reserve
    return max(0.0, deadline_at - time.monotonic() - reserve)


def _clock(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "?"


def _late_note(as_of: float, live: bool) -> str:
    if live:
        return ""
    return (f" · ข้อมูลเมื่อ {_clock(as_of)} ({_fmt_duration(_now() - as_of)}ก่อน)"
            " — gateway ตอบช้า กำลังอ่านค่าใหม่เบื้องหลัง")


# --------------------------------------------------------------------------- ports

def check_ports(ctx: dict) -> dict:
    rows, closed_core, closed_opt = [], [], []
    for port, name, core in PORTS:
        is_open = _port_open(port)
        rows.append({"port": port, "name": name, "open": is_open, "core": core})
        if not is_open:
            (closed_core if core else closed_opt).append(f"{name} ({port})")
    open_count = sum(r["open"] for r in rows)
    if closed_core:
        return _result(BAD, f"เปิด {open_count}/{len(rows)} · ปิดอยู่: " + ", ".join(closed_core + closed_opt), ports=rows)
    if closed_opt:
        return _result(WARN, f"เปิด {open_count}/{len(rows)} · ปิดอยู่: " + ", ".join(closed_opt), ports=rows)
    return _result(OK, f"บริการเปิดครบ {open_count}/{len(rows)} พอร์ต", ports=rows)


# --------------------------------------------------------------------------- ollama log

_RE_TIME = re.compile(r"^time=(\S+)")
_RE_CONFIG = re.compile(r'msg="server config" env="map\[(.*)\]"')
_RE_START = re.compile(r'msg="starting llama-server" cmd="(.*)"')
_RE_TEMPLATE = re.compile(r'msg="template selection" model=(\S+)')
_RE_STARTED = re.compile(r'msg="llama-server started in ([\d.]+) seconds"')
_RE_DEVICE = re.compile(r"using device (\w+) \(([^)]+)\)")
_RE_OFFLOAD = re.compile(r"offloaded (\d+)/(\d+) layers to GPU")
_RE_TASK = re.compile(r"\| task (-?\d+) \| (.*)$")
_RE_NEW = re.compile(r"new prompt, .*task\.n_tokens = (\d+)")
_RE_CACHED = re.compile(r"^cached n_tokens = (\d+)")
_RE_PROGRESS = re.compile(r"prompt processing, n_tokens =\s*(\d+), progress = ([\d.]+), t =\s*([\d.]+) s / ([\d.]+) tokens per second")
_RE_PEVAL = re.compile(r"^prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens .*?([\d.]+) tokens per second")
_RE_EVAL = re.compile(r"^eval time =\s*([\d.]+) ms /\s*(\d+) tokens .*?([\d.]+) tokens per second")
_RE_TASK_END = re.compile(r"(?:cancel task, id_task|send_error: task id) = (\d+)")
_CONFIG_KEYS = ("OLLAMA_MAX_LOADED_MODELS", "OLLAMA_NUM_PARALLEL", "OLLAMA_CONTEXT_LENGTH",
                "OLLAMA_FLASH_ATTENTION", "OLLAMA_LLM_LIBRARY", "OLLAMA_VULKAN", "CUDA_VISIBLE_DEVICES")


def _read_log_text(path: Path) -> str:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size <= 24 * 2**20:
            raw = fh.read()
        else:  # the launchers truncate this log per start, so the config line is near the top
            head = fh.read(512 * 1024)
            fh.seek(size - 16 * 2**20)
            raw = head + b"\n" + fh.read()
    return raw.decode("utf-8", "replace")


def _model_name(value: str) -> str:
    for prefix in ("registry.ollama.ai/library/", "registry.ollama.ai/"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def parse_ollama_log(path: Path = OLLAMA_LOG) -> dict[str, Any]:
    """State of the llama-server runner from the Ollama server log (stderr of `ollama serve`).

    llama-server lines carry no timestamps, so "now" comes from the file's mtime; every
    format assumption is optional - an unknown line is skipped, never an error."""
    text = _read_log_text(path)
    mtime = path.stat().st_mtime
    config: dict[str, str] = {}
    server_started = None
    runner: dict[str, Any] | None = None
    tasks: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for line in text.splitlines():
        if line.startswith("time="):
            m = _RE_CONFIG.search(line)
            if m:
                env = m.group(1)
                config = {}
                for key in _CONFIG_KEYS:
                    km = re.search(r"(?:^| )" + key + r":(\S*)", env)
                    if km:
                        config[key] = km.group(1)
                tm = _RE_TIME.match(line)
                server_started = _iso_to_ts(tm.group(1)) if tm else None
                continue
            m = _RE_START.search(line)
            if m:
                tm = _RE_TIME.match(line)
                port = re.search(r"--port (\d+)", m.group(1))
                blob = re.search(r"--model (\S+)", m.group(1))
                ctx = re.search(r"(?:^| )-c (\d+)", m.group(1))
                runner = {"started_at": _iso_to_ts(tm.group(1)) if tm else None,
                          "port": int(port.group(1)) if port else None,
                          "blob": Path(blob.group(1).replace("\\\\", "\\")).name if blob else None,
                          "num_ctx": int(ctx.group(1)) if ctx else None,
                          "ready": False, "load_s": None, "model": None, "devices": [], "offload": None,
                          "terminated": False}
                # task ids restart with every runner, and an unfinished task of the old one
                # is history, not the current state
                tasks, order = {}, []
                continue
            if runner is not None:
                if 'msg="llama-server terminated"' in line:
                    runner["terminated"] = True
                    continue
                m = _RE_STARTED.search(line)
                if m and not runner["ready"]:
                    runner["ready"], runner["load_s"] = True, float(m.group(1))
                    continue
                m = _RE_TEMPLATE.search(line)
                if m and not runner["model"]:
                    runner["model"] = _model_name(m.group(1))
            continue
        if runner is not None and ("using device" in line or "offloaded" in line):
            m = _RE_DEVICE.search(line)
            if m:
                dev = f"{m.group(1)} ({m.group(2)})"
                if dev not in runner["devices"]:
                    runner["devices"].append(dev)
            m = _RE_OFFLOAD.search(line)
            if m:
                runner["offload"] = f"{m.group(1)}/{m.group(2)}"
            continue
        if line.startswith("srv "):
            # "stop: cancel task, id_task = N" (client went away) / "send_error: task id = N"
            m = _RE_TASK_END.search(line)
            if m and int(m.group(1)) in tasks:
                tasks[int(m.group(1))]["done"] = True
            continue
        if not line.startswith("slot "):
            continue
        m = _RE_TASK.search(line)
        if not m:
            continue
        tid, rest = int(m.group(1)), m.group(2).strip()
        if tid < 0:
            continue
        if rest.startswith("new prompt"):
            nm = _RE_NEW.search(rest)
            tasks[tid] = {"total": int(nm.group(1)) if nm else None, "cached": None, "processed": 0,
                          "progress": None, "rate": None, "t": None, "sampling": False, "done": False,
                          "prompt_tps": None, "prompt_n": None, "gen_tps": None, "gen_n": None,
                          "model": (runner or {}).get("model")}
            order.append(tid)
            continue
        task = tasks.get(tid)
        if task is None:
            continue
        cm = _RE_CACHED.match(rest)
        if cm:
            if task["cached"] is None:
                task["cached"] = int(cm.group(1))
            continue
        pm = _RE_PROGRESS.search(rest)
        if pm:
            task.update(processed=int(pm.group(1)), progress=float(pm.group(2)), t=float(pm.group(3)),
                        rate=float(pm.group(4)))
            if task["progress"] >= 1.0:
                task["sampling"] = True
            continue
        if rest.startswith("init sampler"):
            task["sampling"] = True
            continue
        em = _RE_PEVAL.match(rest)
        if em:
            task.update(prompt_n=int(em.group(2)), prompt_tps=float(em.group(3)), sampling=True)
            continue
        em = _RE_EVAL.match(rest)
        if em:
            task.update(gen_n=int(em.group(2)), gen_tps=float(em.group(3)), done=True)
            continue
        if rest.startswith("stop processing") or rest.startswith("release"):
            task["done"] = True
    return {"mtime": mtime, "config": config, "server_started": server_started, "runner": runner,
            "tasks": [dict(tasks[t], id=t) for t in order[-40:]]}


def _speeds(tasks: list[dict]) -> dict[str, Any]:
    done = [t for t in tasks if t.get("done")]
    prompt = [t["prompt_tps"] for t in done if t.get("prompt_tps") and (t.get("prompt_n") or 0) >= 64][-8:]
    gen = [t["gen_tps"] for t in done if t.get("gen_tps") and (t.get("gen_n") or 0) >= 8][-8:]
    last = done[-1] if done else {}
    return {
        "last_prompt_tps": last.get("prompt_tps"), "last_prompt_tokens": last.get("prompt_n"),
        "last_gen_tps": last.get("gen_tps"), "last_gen_tokens": last.get("gen_n"),
        "median_prompt_tps": round(statistics.median(prompt), 2) if prompt else None,
        "median_gen_tps": round(statistics.median(gen), 2) if gen else None,
        "samples": len(done[-8:]),
    }


def _slots(port: int | None) -> dict[str, Any] | None:
    """llama-server's /slots: exact in-flight counts. It is answered between batches, so it
    is only asked while an answer is being generated (a prompt batch takes ~30 s on CPU)."""
    if not port:
        return None
    try:
        data = _http_json(f"http://127.0.0.1:{port}/slots", timeout=1.5)
    except Exception:
        return None
    slot = data[0] if isinstance(data, list) and data else None
    if not isinstance(slot, dict):
        return None
    nxt = (slot.get("next_token") or [{}])[0] if isinstance(slot.get("next_token"), list) else {}
    return {"is_processing": bool(slot.get("is_processing")), "prompt_tokens": slot.get("n_prompt_tokens"),
            "decoded": nxt.get("n_decoded") if isinstance(nxt, dict) else None}


def _ollama_state(log: dict[str, Any], ps_models: list[dict]) -> dict[str, Any]:
    now = _now()
    since_write = max(0.0, now - log["mtime"])
    runner = log.get("runner") or {}
    tasks = log.get("tasks") or []
    speeds = _speeds(tasks)
    current = tasks[-1] if tasks and not tasks[-1].get("done") else None
    state: dict[str, Any] = {"phase": "idle", "log_age_s": round(since_write)}
    if runner and not runner.get("ready") and not runner.get("terminated"):
        started = runner.get("started_at")
        elapsed = now - started if started else None
        # a load that never finished and left nothing in /api/ps is over, not still running
        if not (elapsed and elapsed > 900 and not ps_models):
            state.update(phase="loading", elapsed_s=round(elapsed) if elapsed is not None else None)
            return {"state": state, "speeds": speeds}
    if runner.get("terminated") and not ps_models:
        return {"state": state, "speeds": speeds}
    if current is None:
        return {"state": state, "speeds": speeds}
    model = current.get("model") or runner.get("model")
    total, cached = current.get("total") or 0, current.get("cached") or 0
    state.update(model=model, prompt_tokens=total, cached_tokens=cached)
    if not current.get("sampling"):
        processed = current.get("processed") or 0
        rate = current.get("rate") or speeds.get("median_prompt_tps")
        remaining = max(0, total - cached - processed)
        eta = (remaining / rate - since_write) if rate else None
        state.update(phase="reading", processed_tokens=cached + processed, remaining_tokens=remaining,
                     progress_pct=round(100 * (cached + processed) / total, 1) if total else None,
                     rate_tps=round(rate, 2) if rate else None,
                     eta_s=round(max(0.0, eta)) if eta is not None else None,
                     eta_overdue=bool(eta is not None and eta < 0))
        if since_write > 120:
            # a 512-token batch takes ~30 s here, so a longer silence may be a request that
            # ended without a log line; /slots answers at once when the runner is idle
            slot = _slots(runner.get("port"))
            if slot is not None and not slot.get("is_processing"):
                state["phase"] = "idle"
    else:
        state.update(phase="generating", rate_tps=speeds.get("median_gen_tps"))
        slot = _slots(runner.get("port"))
        if slot is not None:
            state["generated_tokens"] = slot.get("decoded")
            if not slot.get("is_processing"):
                state["phase"] = "idle"
    # llama-server writes nothing while it generates, but a runner Ollama already unloaded
    # cannot be working: then the half-finished task in the log is history, not state.
    if state["phase"] in ("reading", "generating") and not ps_models:
        state["phase"] = "idle"
    elif state["phase"] in ("reading", "generating") and since_write > 3600 and "generated_tokens" not in state:
        state["phase"] = "unknown"
    return {"state": state, "speeds": speeds}


def _ps(timeout: float = 3.0) -> list[dict[str, Any]]:
    data = _http_json(OLLAMA + "/api/ps", timeout=timeout)
    models = []
    for m in data.get("models") or []:
        size, vram = int(m.get("size") or 0), int(m.get("size_vram") or 0)
        exp = _iso_to_ts(str(m.get("expires_at") or ""))
        models.append({"name": m.get("name"), "size_gb": round(size / 2**30, 1), "gpu_mb": round(vram / 2**20),
                       "gpu_percent": round(100 * vram / size) if size else 0,
                       "context": m.get("context_length"),
                       "unload_in_min": round((exp - _now()) / 60) if exp else None})
    return models


def check_ollama(ctx: dict) -> dict:
    shared: _Shared = ctx["shared"]
    try:
        models = shared.get("ps", _ps)
    except Exception:
        if not _port_open(11434):
            return _result(BAD, "Ollama ไม่ได้รัน (พอร์ต 11434 ปิด)")
        return _result(BAD, "Ollama ไม่ตอบ /api/ps ภายใน 3 วินาที (อาจค้างหรือกำลังโหลดโมเดลหนัก)")
    try:
        version = _http_json(OLLAMA + "/api/version", timeout=2).get("version")
    except Exception:
        version = None
    try:
        log = shared.get("log", parse_ollama_log)
        info = _ollama_state(log, models)
    except Exception as exc:
        info = {"state": {"phase": "unknown", "error": _redact(exc, 120)}, "speeds": {}}
    state, speeds = info["state"], info["speeds"]
    phase = state.get("phase")
    loaded = ", ".join(f"{m['name']} ({m['size_gb']} GB)" for m in models) or "ไม่มีโมเดลโหลดอยู่"
    status = OK
    if phase == "reading":
        eta = state.get("eta_s")
        done_txt = f"{state.get('processed_tokens', 0):,}/{state.get('prompt_tokens', 0):,} tokens"
        if state.get("progress_pct") is not None:
            done_txt += f" ({state['progress_pct']}%)"
        if state.get("eta_overdue"):
            eta_txt = "เกินเวลาที่ประเมินแล้ว"
        else:
            eta_txt = f"เหลือราว {_fmt_duration(eta)}" if eta is not None else "ยังประเมินเวลาไม่ได้"
        now_txt = f"กำลังอ่าน prompt {done_txt} · {eta_txt}"
        if state.get("rate_tps"):
            now_txt += f" ({state['rate_tps']} tok/s)"
        if eta is not None and eta > 1200:
            status = WARN
            now_txt += " — prompt ยาวมาก งานอื่นต้องรอ"
    elif phase == "generating":
        now_txt = "กำลังสร้างคำตอบ"
        if state.get("generated_tokens") is not None:
            now_txt += f" ได้ {state['generated_tokens']} tokens แล้ว"
        if state.get("rate_tps"):
            now_txt += f" (~{state['rate_tps']} tok/s)"
    elif phase == "loading":
        now_txt = "กำลังโหลดโมเดล" + (f" มา {_fmt_duration(state.get('elapsed_s'))}" if state.get("elapsed_s") else "")
    elif phase == "unknown":
        now_txt = "ไม่แน่ใจว่าทำอะไรอยู่ (ล็อกไม่ขยับนาน)"
    else:
        now_txt = "ว่าง"
    speed_bits = []
    if speeds.get("median_prompt_tps"):
        speed_bits.append(f"อ่าน prompt ~{speeds['median_prompt_tps']} tok/s")
    if speeds.get("median_gen_tps"):
        speed_bits.append(f"ตอบ ~{speeds['median_gen_tps']} tok/s")
    summary = f"{loaded} · {now_txt}"
    if speed_bits:
        summary += " · วัดล่าสุด: " + ", ".join(speed_bits)
    return _result(status, summary, version=version, models=models, state=state, speeds=speeds)


def _registry_user_env(name: str) -> str | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _kind = winreg.QueryValueEx(key, name)
            return str(value)
    except (OSError, ImportError):
        return None


def check_ollama_config(ctx: dict) -> dict:
    """The running server's settings (from its own 'server config' log line) against what
    the suite expects: MAX_LOADED_MODELS and, on path B, no Quadro P620."""
    shared: _Shared = ctx["shared"]
    try:
        log = shared.get("log", parse_ollama_log)
    except Exception as exc:
        return _result(UNKNOWN, "อ่านล็อก Ollama ไม่ได้: " + _redact(exc, 100))
    try:
        models = shared.get("ps", _ps)
    except Exception:
        models = []
    config = log.get("config") or {}
    runner = log.get("runner") or {}
    problems: list[str] = []
    running = config.get("OLLAMA_MAX_LOADED_MODELS")
    registry = _registry_user_env("OLLAMA_MAX_LOADED_MODELS")
    if running and running != str(EXPECTED_MAX_LOADED):
        msg = f"Ollama ที่รันอยู่ตั้ง MAX_LOADED_MODELS={running} (ควรเป็น {EXPECTED_MAX_LOADED})"
        if registry and registry != str(EXPECTED_MAX_LOADED):
            msg += f" และค่า User ในรีจิสทรีก็เป็น {registry} — รีสตาร์ทอย่างเดียวไม่หาย ต้องแก้ค่าก่อน"
        else:
            msg += " — ต้องรีสตาร์ท Ollama ให้ค่าใหม่มีผล"
        problems.append(msg)
    elif not running:
        problems.append("ไม่พบบรรทัด server config ในล็อก Ollama")
    devices = runner.get("devices") or []
    vram_mb = sum(m.get("gpu_mb") or 0 for m in models)
    p620 = [d for d in devices if "P620" in d or "Quadro" in d]
    rtx = [d for d in devices if "RTX" in d]
    if TARGET_GPU_PATH == "B" and (p620 or (vram_mb > 0 and not rtx)):
        if models:
            problems.append(f"Ollama ยังใช้ Quadro P620 (VRAM ~{vram_mb} MB) — ทาง B ต้องปิดการ์ดนี้แล้วรีสตาร์ท Ollama")
        else:  # nothing loaded right now, but the last load used it and the next one will too
            problems.append("Ollama ยังเห็น Quadro P620 (โมเดลที่โหลดครั้งล่าสุดใช้การ์ดนี้) — ทาง B ต้องปิดการ์ดนี้แล้วรีสตาร์ท Ollama")
    if rtx:
        gpu_txt = "ใช้ eGPU " + ", ".join(d.split("(", 1)[-1].rstrip(")") for d in rtx)
    elif devices or vram_mb:
        gpu_txt = "ใช้ GPU ภายใน " + ", ".join(d.split("(", 1)[-1].rstrip(")") for d in devices)
    else:
        gpu_txt = "CPU อย่างเดียว"
    details = {
        "max_loaded_running": running, "max_loaded_registry": registry, "max_loaded_expected": EXPECTED_MAX_LOADED,
        "num_parallel": config.get("OLLAMA_NUM_PARALLEL"), "context_length": config.get("OLLAMA_CONTEXT_LENGTH"),
        "llm_library": config.get("OLLAMA_LLM_LIBRARY") or None,
        "vulkan": config.get("OLLAMA_VULKAN"),
        "cuda_visible_devices": "ตั้งไว้" if config.get("CUDA_VISIBLE_DEVICES") else "ว่าง",
        "server_started": _fmt_time(log.get("server_started")),
        "runner_devices": devices, "runner_offload": runner.get("offload"), "vram_mb": vram_mb,
        "runner_num_ctx": runner.get("num_ctx"), "target_path": TARGET_GPU_PATH,
    }
    started = _fmt_time(log.get("server_started"))
    if problems:
        return _result(WARN, " · ".join(problems), **details)
    return _result(OK, f"MAX_LOADED_MODELS={running} · {gpu_txt}" + (f" · เริ่มเมื่อ {started}" if started else ""),
                   **details)


# --------------------------------------------------------------------------- referenced models (added 2026-10-06)

# The model download queue of the 1.6.1 pipeline (Run-ModelJob.ps1 writes <job>.json there).
MODEL_QUEUE = SUITE_ROOT / "data" / "model-queue"
# job name -> the Ollama tag it installs; a job's note does not always name its model
QUEUE_JOB_MODELS = {"muse": "muse-glimmer:30b", "granite": "granite4.2:8b"}
_QUEUE_FINAL = ("done", "failed", "error")
# A model tag such as qwen3.8:27b or hf.co/org/name:Q4_K_M. The letter required before the
# colon keeps host:port pairs (127.0.0.1:11434) and clock times out.
_TAG = r"(?=[^:\s'\"]*[A-Za-z])[A-Za-z0-9][\w.\-/]*:[\w.\-]+"
_RE_TAG = re.compile(_TAG)
_RE_ROUTER_CONST = re.compile(r"[A-Z][A-Z0-9_]*_MODEL")
# PowerShell one token at a time: a quoted string, a <# block #> or # line comment, a line break, any other character
_RE_PS_TOKEN = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"`]|`.)*\"|<#.*?#>|#[^\n]*|\n|\S", re.S)
_RE_ROUTER_PROFILE = re.compile(
    r"[\"'](qwen-[\w.\-]+?)(?::latest)?[\"']\s*:\s*\{[^{}]*?[\"']target[\"']\s*:\s*([A-Z][A-Z0-9_]*)\b")
MODEL_STATE_TH = {"installed": "มีแล้ว", "downloading": "กำลังดาวน์โหลด", "missing": "ไม่มีใน Ollama",
                  "optional_missing": "ไม่มี (ไม่บังคับ)"}


def _norm_tag(name: Any) -> str:
    name = _model_name(str(name or "").strip()).lower()
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


def _py_assigned(nodes: Any) -> dict[str, list[ast.expr]]:
    """Name -> every expression assigned to it (NAME = ... or NAME: type = ...) among these nodes."""
    names: dict[str, list[ast.expr]] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                names.setdefault(target.id, []).append(value)
    return names


def _py_tags(node: ast.expr, names: dict[str, list[ast.expr]], seen: set[str] | None = None) -> list[str]:
    """The model tags an expression can evaluate to: string literals, both branches of an
    if-else, `a or b`, tuple/list items, the default of os.environ.get()/os.getenv(), and
    names assigned in the same file. Comments and docstrings are never read; anything
    computed (f-strings, other calls) gives nothing, so the caller can report it unreadable."""
    seen = set() if seen is None else seen
    if isinstance(node, ast.Constant):
        value = node.value.strip() if isinstance(node.value, str) else ""
        return [value] if _RE_TAG.fullmatch(value) else []
    if isinstance(node, ast.Name):
        if node.id in seen:
            return []
        seen.add(node.id)
        return [tag for value in names.get(node.id, ()) for tag in _py_tags(value, names, seen)]
    if isinstance(node, ast.IfExp):
        parts = [node.body, node.orelse]
    elif isinstance(node, ast.BoolOp):
        parts = list(node.values)
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        parts = list(node.elts)
    elif isinstance(node, ast.Starred):
        parts = [node.value]
    elif isinstance(node, ast.Call):
        func = node.func
        owner = func.value if isinstance(func, ast.Attribute) else None
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        environ = (isinstance(owner, ast.Attribute) and owner.attr == "environ") or (
            isinstance(owner, ast.Name) and owner.id == "environ")
        if not (name == "getenv" or (name == "get" and environ)):
            return []
        parts = list(node.args[1:2]) + [kw.value for kw in node.keywords if kw.arg == "default"]
    else:
        return []
    return [tag for part in parts for tag in _py_tags(part, names, seen)]


def _refs_router(text: str) -> list[tuple[str, str]]:
    """GENERAL_MODEL, CODE_MODEL, AGENT_MODEL, STRUCTURED_MODEL ... (module level) - not the
    LEGACY_* fallbacks, which are optional by design. A constant whose value cannot be read
    comes back with an empty tag, so the check says so instead of skipping it."""
    profiles: dict[str, list[str]] = {}
    for m in _RE_ROUTER_PROFILE.finditer(text):
        profiles.setdefault(m.group(2), []).append(m.group(1))
    names = _py_assigned(ast.parse(text).body)
    refs = []
    for const in names:
        if const.startswith("LEGACY_") or not _RE_ROUTER_CONST.fullmatch(const):
            continue
        uses = profiles.get(const)
        label = f"router {const}" + (f" ({', '.join(uses)})" if uses else "")
        tags = _py_tags(ast.Name(id=const, ctx=ast.Load()), names)
        refs += [(tag, label) for tag in tags] or [("", label)]
    return refs


def _refs_video(text: str) -> list[tuple[str, str]]:
    # every `storyboard_model = ...` in the file; names are looked up anywhere in the file
    names = _py_assigned(ast.walk(ast.parse(text)))
    return [(tag, "Video Studio storyboard") for tag in _py_tags(ast.Name(id="storyboard_model", ctx=ast.Load()), names)]


def _refs_research(text: str) -> list[tuple[str, str]]:
    names = _py_assigned(ast.parse(text).body)
    return [(tag, "Research PREFERRED_MODELS") for tag in _py_tags(ast.Name(id="PREFERRED_MODELS", ctx=ast.Load()), names)]


def _refs_codex_known(text: str) -> list[tuple[str, str]]:
    """$KnownModels = @('a', 'b') or $KnownModels = 'a', 'b' (also over several lines);
    commented-out entries are not counted."""
    m = re.search(r"^[ \t]*\$KnownModels[ \t]*=", text, re.M | re.I)
    if not m:
        return []
    refs, depth, prev = [], 0, None
    for tok in _RE_PS_TOKEN.finditer(text, m.end()):
        t = tok.group(0)
        if t.startswith("#") or t.startswith("<#"):
            continue
        if t == "\n":
            if depth == 0 and prev not in (None, ","):
                break  # end of the statement (a line ending in a comma continues the array)
            continue
        if t[0] in "'\"" and len(t) > 1:
            if _RE_TAG.fullmatch(t[1:-1].strip()):
                refs.append((t[1:-1].strip(), "Codex bridge $KnownModels"))
        elif t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
            if depth <= 0:
                break
        prev = t
    return refs


def _refs_codex_config(text: str) -> list[tuple[str, str]]:
    data = json.loads(text)
    refs = []
    for key in ("default_model", "review_model"):  # the only keys used; no other value is reported
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str) and _RE_TAG.fullmatch(value.strip()):
            refs.append((value.strip(), f"Codex bridge {key}"))
    return refs


def _refs_embedding_env(text: str) -> list[tuple[str, str]]:
    m = re.search(r"\$env:RAG_EMBEDDING_MODEL\s*=\s*[\"'](" + _TAG + r")[\"']", text, re.I)
    return [(m.group(1), "Knowledge/RAG ของ Open WebUI (embedding)")] if m else []


def _refs_embedding_config(text: str) -> list[tuple[str, str]]:
    m = re.search(r"[\"']rag\.embedding_model[\"']\s*:\s*[\"'](" + _TAG + r")[\"']", text)
    return [(m.group(1), "Knowledge/RAG ของ Open WebUI (embedding)")] if m else []


# (file under the suite root, reader, "all" = every name must be installed, "any" = one is enough)
MODEL_SOURCES: tuple[tuple[str, Callable[[str], list[tuple[str, str]]], str], ...] = (
    ("ai-services/router.py", _refs_router, "all"),
    ("local-tools/video_worker.py", _refs_video, "all"),
    ("local-tools/research_worker.py", _refs_research, "any"),
    ("local-tools/codex/Invoke-QwenCodex.ps1", _refs_codex_known, "all"),
    ("data/codex-bridge.json", _refs_codex_config, "all"),
    ("start-webui.ps1", _refs_embedding_env, "all"),
    ("Configure-Qwen-WebUI.py", _refs_embedding_config, "all"),
)


def _read_shared(path: Path, limit: int = 64 * 1024) -> bytes:
    """Read a small file opened with FILE_SHARE_DELETE, so a writer that swaps it in with
    File.Replace (the queue jobs do) can never fail because this check had it open."""
    if os.name != "nt":
        with path.open("rb") as fh:
            return fh.read(limit)
    import msvcrt
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    # GENERIC_READ, FILE_SHARE_READ|WRITE|DELETE, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0x80, None)
    if not handle or handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as fh:  # closing the file closes the handle
        return fh.read(limit)


def _queue_job_alive(pid: Any, updated: float | None) -> bool | None:
    """True only while the pid is still the PowerShell that wrote the job file. Windows reuses
    PIDs, so after a crash or a reboot an unrelated process can hold the same number: it must
    be powershell.exe/pwsh.exe and must have started before the file's last update."""
    if not pid:
        return None
    try:
        import psutil
        proc = psutil.Process(int(pid))
        if proc.name().lower() not in ("powershell.exe", "pwsh.exe"):
            return False
        # 'updated' has whole seconds; the slack also covers a small clock correction
        return updated is None or proc.create_time() <= updated + 300
    except Exception as exc:
        return False if type(exc).__name__ in ("NoSuchProcess", "ZombieProcess") else None


def _model_queue(unreadable: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Normalised model tag -> its job in the download queue. Read-only; job, state and times only.
    The name of each job file that could not be read or parsed is added to `unreadable`."""
    jobs: dict[str, dict[str, Any]] = {}
    try:
        paths = sorted(MODEL_QUEUE.glob("*.json"))[:20]
    except OSError:
        return jobs
    for path in paths:
        try:
            data = json.loads(_read_shared(path).decode("utf-8-sig", "replace"))
        except Exception:
            if unreadable is not None:
                unreadable.append(path.stem.lower())
            continue  # being swapped in this instant, or not a job file
        if not isinstance(data, dict):
            continue
        job = str(data.get("job") or path.stem)
        tags = [QUEUE_JOB_MODELS[k] for k in {job.lower(), path.stem.lower()} if k in QUEUE_JOB_MODELS]
        tags += _RE_TAG.findall(str(data.get("note") or ""))
        if not tags:
            continue  # e.g. the Qwen-Image weights job: not an Ollama model
        updated = _iso_to_ts(str(data.get("updated") or ""))
        info = {"job": _redact(job, 30), "state": _redact(str(data.get("state") or "?"), 30).lower(),
                "updated": _fmt_time(updated), "process_alive": _queue_job_alive(data.get("pid"), updated)}
        for tag in tags:
            jobs.setdefault(_norm_tag(tag), info)
    return jobs


def check_models(ctx: dict) -> dict:
    """Every model the suite names in its code and config against Ollama's /api/tags (which
    lists what is on disk and loads nothing). A model whose queue job is still running is
    'downloading' (warn), not missing."""
    try:
        return _check_models()
    except Exception as exc:  # this check must never break the page
        return _result(UNKNOWN, "ตรวจรายชื่อโมเดลไม่สำเร็จ: " + _redact(f"{type(exc).__name__}: {exc}", 100))


def _tags_error(exc: Exception) -> str:
    """Why /api/tags gave no list - a timeout, a closed port and a bad answer read differently."""
    reason = getattr(exc, "reason", None)
    if isinstance(exc, urllib.error.HTTPError):
        return f"Ollama ตอบ /api/tags ผิดปกติ (HTTP {exc.code})"
    if isinstance(exc, ValueError):
        return f"Ollama ตอบ /api/tags ผิดรูปแบบ ({type(exc).__name__})"
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
        return "Ollama ไม่ตอบ /api/tags ภายใน 3 วินาที"
    if isinstance(reason, ConnectionRefusedError) or not _port_open(11434):
        return "Ollama ไม่ได้รัน (พอร์ต 11434 ปิด)"
    return f"เรียก Ollama /api/tags ไม่สำเร็จ ({type(reason or exc).__name__})"


# Optional source: missing, or holding no model tag (its values may be words such as "auto"), is fine.
_OPTIONAL_MODEL_SOURCES = {"data/codex-bridge.json"}
# str(path) -> ((mtime_ns, size), names read): parsing research_worker.py alone takes ~50 ms
_MODEL_SOURCE_CACHE: dict[str, tuple[tuple[int, int], list[tuple[str, str]]]] = {}


def _check_models() -> dict:
    refs: dict[str, dict[str, Any]] = {}
    groups: list[tuple[str, list[str]]] = []
    sources: list[dict[str, Any]] = []
    for rel, reader, mode in MODEL_SOURCES:
        path = SUITE_ROOT / rel
        item: dict[str, Any] = {"file": rel, "found": 0}
        sources.append(item)
        try:
            if not path.is_file():
                item["missing"] = True
                if rel not in _OPTIONAL_MODEL_SOURCES:
                    item["error"] = "ไม่พบไฟล์"
                continue
            st = path.stat()
            if st.st_size > 4 * 2**20:
                raise ValueError("file too large")
            stamp = (st.st_mtime_ns, st.st_size)
            cached = _MODEL_SOURCE_CACHE.get(str(path))
            if cached and cached[0] == stamp:
                found = cached[1]
            else:
                found = reader(path.read_text(encoding="utf-8-sig", errors="replace"))
                _MODEL_SOURCE_CACHE[str(path)] = (stamp, found)
        except Exception as exc:
            item["error"] = type(exc).__name__
            continue
        unresolved = [label for tag, label in found if not tag]
        found = [(tag, label) for tag, label in found if tag]
        item["found"] = len(found)
        if unresolved:
            item["error"] = "อ่านค่าไม่ได้: " + ", ".join(unresolved)
        elif not found and rel not in _OPTIONAL_MODEL_SOURCES:
            item["error"] = "ไม่พบชื่อโมเดล"
        keys = []
        for tag, label in found:
            key = _norm_tag(tag)
            row = refs.setdefault(key, {"model": tag[:80], "used_by": [], "required": False})
            if label not in row["used_by"]:
                row["used_by"].append(label)
            row["required"] = row["required"] or mode == "all"
            keys.append(key)
        if mode == "any" and keys:
            groups.append((found[0][1], keys))
    if not refs:
        return _result(UNKNOWN, "ไม่พบชื่อโมเดลในไฟล์ของชุด (รูปแบบไฟล์อาจเปลี่ยนไป)", sources=sources)
    try:
        data = _http_json(OLLAMA + "/api/tags", timeout=3)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except Exception as exc:
        return _result(UNKNOWN, _tags_error(exc) + " — เทียบรายชื่อโมเดลไม่ได้",
                       models=[{"model": r["model"], "used_by": r["used_by"]} for r in refs.values()], sources=sources)
    installed: dict[str, str] = {}
    for m in data.get("models") or []:
        if isinstance(m, dict):
            for field in ("name", "model"):
                if m.get(field):
                    installed.setdefault(_norm_tag(m[field]), str(m.get("name") or m[field])[:80])

    def read_queue() -> tuple[dict[str, dict[str, Any]], list[str]]:
        unreadable: list[str] = []
        try:
            return _model_queue(unreadable), unreadable
        except Exception:
            return {}, unreadable

    # Run-ModelJob.ps1 swaps a job file in with File.Replace: for about a millisecond the file can
    # be absent or locked. Look again (twice, 0.1 s apart) before calling a model missing.
    queue, unreadable = read_queue()
    job_tags = {_norm_tag(tag) for tag in QUEUE_JOB_MODELS.values()}
    for _ in range(2):
        if not any(k not in installed and k not in queue and (k in job_tags or unreadable) for k in refs):
            break
        time.sleep(0.1)
        more, unreadable = read_queue()
        for k, v in more.items():
            queue.setdefault(k, v)
    for stem in unreadable:  # a known job whose file still cannot be read: its state is unknown, not "missing"
        if stem in QUEUE_JOB_MODELS:
            queue.setdefault(_norm_tag(QUEUE_JOB_MODELS[stem]),
                             {"job": stem, "state": "อ่านไฟล์คิวไม่ได้", "updated": None, "process_alive": None})
    rows: list[dict[str, Any]] = []
    for key, ref in refs.items():
        job = queue.get(key)
        if key in installed:
            state = "installed"
        elif job and job["state"] not in _QUEUE_FINAL:
            state = "downloading"
        else:
            state = "missing" if ref["required"] else "optional_missing"
        row = {"model": ref["model"], "state": state, "state_th": MODEL_STATE_TH[state],
               "used_by": ref["used_by"], "required": ref["required"]}
        if job and state != "installed":
            row.update(queue_job=job["job"], queue_state=job["state"], queue_updated=job["updated"],
                       queue_process_alive=job["process_alive"])
        rows.append(row)
    order = {"missing": 0, "downloading": 1, "optional_missing": 2, "installed": 3}
    rows.sort(key=lambda r: (order[r["state"]], r["model"]))

    def who(row: dict[str, Any]) -> str:
        used = row["used_by"]
        return "; ".join(used[:2]) + (" …" if len(used) > 2 else "")

    status, bits = OK, []
    missing = [r for r in rows if r["state"] == "missing"]
    pending = [r for r in rows if r["state"] == "downloading" and r["required"]]
    if missing:
        status = BAD
        bits.append("ไม่มีใน Ollama: " + ", ".join(
            f"{r['model']} ({who(r)})" + (" คิวดาวน์โหลดล้มเหลว" if r.get("queue_state") in ("failed", "error") else "")
            for r in missing) + " — ต้อง ollama pull หรือแก้ชื่อโมเดลในไฟล์ที่อ้างถึง")
    if pending:
        status = WARN if status == OK else status
        txt = "กำลังดาวน์โหลด: " + ", ".join(f"{r['model']} ({who(r)})" for r in pending)
        if any(r.get("queue_process_alive") is False for r in pending):
            txt += " — แต่โปรเซสของคิวไม่อยู่แล้ว ดาวน์โหลดอาจหยุดไป"
        else:
            txt += " — ใช้ได้เมื่อโหลดเสร็จ"
        bits.append(txt)
    for label, keys in groups:
        if any(k in installed for k in keys):
            continue
        if any(queue.get(k) and queue[k]["state"] not in _QUEUE_FINAL for k in keys):
            status = WARN if status == OK else status
            bits.append(f"{label}: ยังไม่มีตัวไหนติดตั้งเสร็จ (รอดาวน์โหลด)")
        else:
            status = BAD
            bits.append(f"{label}: ไม่มีตัวไหนติดตั้งเลย ต้องมีอย่างน้อย 1 ตัว")
    unread = [s for s in sources if s.get("error")]
    if unread:  # a source that stops matching must not drop out silently and leave the check "ok"
        status = WARN if status == OK else status
        bits.append("อ่านชื่อโมเดลจาก " + ", ".join(f"{s['file']} ({s['error']})" for s in unread)
                    + " ไม่ได้ (รูปแบบไฟล์อาจเปลี่ยน) จึงยังไม่ได้ตรวจโมเดลที่ไฟล์นั้นอ้างถึง")
    required = [r for r in rows if r["required"]]
    have = sum(1 for r in required if r["state"] == "installed")
    names = sorted(set(installed.values()))
    details = {"models": rows, "installed": names, "required_installed": have, "required_total": len(required),
               "sources": sources}
    if status == OK:
        return _result(OK, f"ติดตั้งครบ {len(required)} โมเดลที่ชุดเรียกใช้ · ใน Ollama มี {len(names)} โมเดล", **details)
    return _result(status, f"ติดตั้งแล้ว {have}/{len(required)} โมเดลที่ชุดเรียกใช้ · " + " · ".join(bits), **details)


# --------------------------------------------------------------------------- display adapters

_PNP_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$rows = @(Get-PnpDevice -Class Display | ForEach-Object {
  $ver = $null
  if ($_.Present -and $_.InstanceId -like 'PCI\VEN_10DE*') {
    $ver = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_DriverVersion').Data
  }
  [pscustomobject]@{ name = [string]$_.FriendlyName; status = [string]$_.Status; present = [bool]$_.Present;
                     code = [int]$_.ConfigManagerErrorCode; pnp = [string]$_.InstanceId; driver = [string]$ver }
})
ConvertTo-Json -InputObject $rows -Compress
"""


def _nvidia_branch(version: str) -> float | None:
    """Windows driver version 32.0.15.8195 -> NVIDIA 581.95 (last five digits)."""
    digits = re.sub(r"\D", "", ".".join(str(version or "").split(".")[-2:]))
    if len(digits) < 5:
        return None
    return int(digits[-5:]) / 100


def check_display(ctx: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="qwen-health-ps-", ignore_cleanup_errors=True) as tmp:
        script = Path(tmp) / "pnp.ps1"
        script.write_text(_PNP_SCRIPT, encoding="utf-8-sig")
        code, out, _err = _run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                "-File", str(script)], timeout=9)
    if code is None:
        return _result(UNKNOWN, "Get-PnpDevice ไม่ตอบภายใน 9 วินาที")
    try:
        rows = _json_from_output(out)
    except ValueError:
        return _result(UNKNOWN, "อ่านรายการการ์ดจอไม่ได้")
    if isinstance(rows, dict):
        rows = [rows]
    adapters, problems = [], []
    p620_state = None
    branch = None
    for row in rows:
        pnp = str(row.get("pnp") or "").upper()
        dev = re.search(r"VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})", pnp)
        name = str(row.get("name") or "")
        present, code_ = bool(row.get("present")), int(row.get("code") or 0)
        nvidia = bool(dev and dev.group(1) == "10DE")
        if not present and not nvidia:
            continue  # phantom entries of adapters that are not attached
        item = {"name": name, "present": present, "status": row.get("status"), "problem_code": code_,
                "pci": f"{dev.group(1)}:{dev.group(2)}" if dev else None}
        if nvidia and row.get("driver"):
            item["driver_branch"] = _nvidia_branch(str(row["driver"]))
            branch = branch or item["driver_branch"]
        adapters.append(item)
        is_p620 = bool(dev and dev.group(2) == "1CBD") or "P620" in name
        if is_p620 and present:
            p620_state = "disabled" if code_ == 22 else ("ok" if code_ == 0 else f"code {code_}")
        elif nvidia and present and code_ not in (0, 22):
            problems.append((BAD, f"{name}: Code {code_} (ไดรเวอร์ไม่ทำงานกับการ์ดนี้)"))
    if TARGET_GPU_PATH == "B":
        if p620_state == "ok":
            problems.append((WARN, "Quadro P620 ยังเปิดอยู่ — ทาง B ต้องปิดใน Device Manager (ดู README 'ทาง B — ขั้นตอน')"))
        if branch and branch < 590:
            problems.append((WARN, f"ไดรเวอร์ NVIDIA ยังเป็นสาย R580 ({branch:.2f}) — ทาง B ต้องลงรุ่นล่าสุดสำหรับ RTX"))
    details = {"adapters": adapters, "target_path": TARGET_GPU_PATH, "p620": p620_state,
               "nvidia_driver_branch": branch}
    if problems:
        worst = max((s for s, _ in problems), key=lambda s: RANK[s])
        return _result(worst, " · ".join(msg for _, msg in problems), **details)
    names = ", ".join(a["name"] for a in adapters if a["present"]) or "ไม่พบการ์ดจอ"
    if TARGET_GPU_PATH == "B" and p620_state == "disabled":
        names += " · P620 ปิดแล้วตามทาง B"
    return _result(OK, names, **details)


# --------------------------------------------------------------------------- disk

def check_disk(ctx: dict) -> dict:
    drives, problems = [], []
    worst = OK
    for drive in ("C:\\", "E:\\"):
        try:
            usage = shutil.disk_usage(drive)
        except OSError:
            drives.append({"drive": drive[:2], "present": False})
            continue
        free_gb = usage.free / 2**30
        drives.append({"drive": drive[:2], "free_gb": round(free_gb, 1), "total_gb": round(usage.total / 2**30),
                       "used_percent": round(100 * usage.used / usage.total, 1) if usage.total else None})
        if free_gb < DISK_BAD_GB:
            worst = BAD
            problems.append(f"{drive[:2]} เหลือ {free_gb:.1f} GB (วิกฤต)")
        elif free_gb < DISK_WARN_GB:
            worst = WARN if worst != BAD else worst
            problems.append(f"{drive[:2]} เหลือ {free_gb:.1f} GB (ต่ำกว่า {DISK_WARN_GB:.0f} GB)")
    fine = [f"{d['drive']} ว่าง {d['free_gb']} GB" for d in drives
            if "free_gb" in d and d["free_gb"] >= DISK_WARN_GB]
    summary = " · ".join(problems + fine)
    return _result(worst, summary or "อ่านพื้นที่ดิสก์ไม่ได้", drives=drives)


# --------------------------------------------------------------------------- OpenClaw channels

def _openclaw_log_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    candidates = [Path(local) / "Temp" / "openclaw"] if local else []
    candidates.append(Path(tempfile.gettempdir()) / "openclaw")
    for cand in candidates:
        if cand.is_dir():
            return cand
    return candidates[0]


# "[whatsapp:default] health-monitor: skipping restart, terminal-disconnect" is repeated every 5 minutes for as
# long as the channel stays down; the event itself is the channel giving up ("after 12/12 attempts",
# "auto-restart skipped, terminal disconnect"). One outage counts once, at the time it happened.
_RE_TERMINAL = re.compile(r"\[(\w+):[^\]]*\][^\n]*terminal-disconnect")
_RE_TERMINAL_EVENT = re.compile(r"auto-restart skipped, terminal disconnect|after \d+/\d+ attempts", re.I)
_RE_PROVIDER_START = re.compile(r"starting provider|channel started|provider started", re.I)
# a session the service ended on purpose needs a new login - restarting the channel cannot fix it (WhatsApp/Baileys:
# 401 logged out, 403 forbidden, 411 multi-device mismatch, 440 replaced by another session, 500 bad session).
# Network codes such as 408/428/503/515 and DNS/socket errors are what a restart is for.
RE_CHANNEL_NEEDS_LOGIN = re.compile(
    r"(?i)logged ?out|unauthori[sz]ed|\b(?:401|403|411|440|500)\b|conflict|replaced|banned|bad ?session|multi-?device")


def channel_needs_login(state: dict[str, Any]) -> bool:
    """Gateway health of one channel says the account is not linked any more (new QR / token needed)."""
    return (state.get("linked") is False or state.get("statusState") not in (None, "linked")
            or bool(RE_CHANNEL_NEEDS_LOGIN.search(str(state.get("lastError") or ""))))


def channel_watchdog_enabled() -> bool:
    return (os.environ.get("QWEN_CHANNEL_WATCHDOG", "1") or "1").strip() != "0"


def parse_openclaw_log(directory: Path | None = None) -> dict[str, Any]:
    """Per-channel events from the newest openclaw-YYYY-MM-DD.log (JSON lines). Counts and
    times only; message text never leaves this function."""
    directory = directory or _openclaw_log_dir()
    logs = sorted(directory.glob("openclaw-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return {"file": None, "channels": {}}
    path = logs[0]
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > 8 * 2**20:
            fh.seek(size - 8 * 2**20)
            fh.readline()
        raw = fh.read().decode("utf-8", "replace")
    channels: dict[str, dict[str, Any]] = {}

    def ch(name: str) -> dict[str, Any]:
        return channels.setdefault(name, {"terminal_disconnects": 0, "last_terminal_disconnect": None,
                                          "last_start": None, "warnings": 0, "errors": 0, "last_problem": None})

    down: set[str] = set()  # channels in a terminal outage that is already counted

    def terminal(name: str, ts: float | None) -> None:
        if name in down:
            return
        down.add(name)
        c = ch(name)
        c["terminal_disconnects"] += 1
        c["last_terminal_disconnect"] = ts

    for line in raw.splitlines():
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        meta = obj.get("_meta") or {}
        ts = _iso_to_ts(str(meta.get("date") or ""))
        name = str(meta.get("name") or "")
        msg = obj.get("1") if isinstance(obj.get("1"), str) else (obj.get("2") if isinstance(obj.get("2"), str) else "")
        level = int(meta.get("logLevelId") or 0)
        tm = _RE_TERMINAL.search(msg or "")
        if tm:
            terminal(tm.group(1).lower(), ts)
            continue
        sub = re.search(r"channels/(\w+)", name)
        if sub:
            cname = sub.group(1).lower()
            c = ch(cname)
            if _RE_PROVIDER_START.search(msg or ""):
                c["last_start"] = ts
                down.discard(cname)
            if _RE_TERMINAL_EVENT.search(msg or ""):
                terminal(cname, ts)
            if level >= 5:
                c["errors"] += 1
                c["last_problem"] = ts
            elif level == 4:
                c["warnings"] += 1
                c["last_problem"] = ts
        elif "whatsapp" in name.lower() or "telegram" in name.lower():
            c = ch("whatsapp" if "whatsapp" in name.lower() else "telegram")
            if level >= 5:
                c["errors"] += 1
                c["last_problem"] = ts
    return {"file": path.name, "channels": channels}


_CHANNEL_FIELDS = ("enabled", "configured", "running", "connected", "lifecycle", "healthState", "statusState",
                   "linked", "reconnectAttempts", "restartPending")


def _gateway_health_now() -> dict[str, Any]:
    code, out, err = _run(_openclaw_argv("gateway", "call", "health", "--json"), timeout=OPENCLAW_CLI_TIMEOUT_S,
                          cwd=str(OPENCLAW_RUNTIME) if OPENCLAW_RUNTIME.is_dir() else None)
    if code is None:
        raise TimeoutError(f"openclaw gateway call health ไม่ตอบภายใน {OPENCLAW_CLI_TIMEOUT_S:.0f} วินาที")
    data = _json_from_output(out)
    if not isinstance(data, dict):
        raise ValueError("unexpected health output")
    channels = {}
    for name, value in (data.get("channels") or {}).items():
        if not isinstance(value, dict):
            continue
        item = {k: (_redact(value[k], 40) if isinstance(value[k], str) else value[k])
                for k in _CHANNEL_FIELDS if k in value}
        for k in ("lastConnectedAt", "lastInboundAt", "lastOutboundAt"):
            if isinstance(value.get(k), (int, float)):
                item[k] = _fmt_time(value[k] / 1000)
        if value.get("lastError"):
            item["lastError"] = _redact(value["lastError"], 160)
        if "terminalDisconnect" in value:
            item["terminal"] = bool(value.get("terminalDisconnect"))
        channels[str(name)] = item
    failed_queues = [q for q in ((data.get("deliveryQueues") or {}).get("failed") or []) if isinstance(q, dict)]
    failed = [(str(q.get("queueName")), int(q.get("count") or 0)) for q in failed_queues]
    oldest = [q["oldestFailedAt"] for q in failed_queues if isinstance(q.get("oldestFailedAt"), (int, float))]
    return {"channels": channels, "failed_deliveries": sum(c for _, c in failed),
            "oldest_failed": _fmt_time(min(oldest) / 1000) if oldest else None,
            "event_loop_degraded": bool((data.get("eventLoop") or {}).get("degraded")),
            "labels": data.get("channelLabels") or {}}


def _gateway_health(ctx: dict | None = None) -> tuple[dict[str, Any], float, bool]:
    ctx = ctx or {}
    return _SLOW.get("gateway_health", _gateway_health_now, _wait_left(ctx), "openclaw gateway call health",
                     reuse_s=0.0 if ctx.get("fresh") else CHECKS["channels"]["ttl"])


CHANNEL_WATCHDOG_STATUS = SUITE_ROOT / "data" / "channel-watchdog.json"


def _watchdog_events() -> list[dict[str, Any]]:
    """Today's actions of local-tools\\channel_watchdog.py (it restarts a channel that gave up after a network
    outage); times, channel names and outcomes only."""
    try:
        data = json.loads(CHANNEL_WATCHDOG_STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    return [e for e in data.get("events") or [] if isinstance(e, dict) and str(e.get("time", "")).startswith(today)]


def _event_ts(ev: dict[str, Any]) -> float:
    try:
        return datetime.strptime(str(ev.get("time") or ""), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return 0.0


def check_channels(ctx: dict) -> dict:
    try:
        log = parse_openclaw_log()
    except Exception as exc:
        log = {"file": None, "channels": {}, "error": _redact(exc, 100)}
    as_of, live_now = None, True
    try:
        rpc, as_of, live_now = _gateway_health(ctx)
        rpc_error = None
    except Exception as exc:
        rpc, rpc_error = None, _redact(exc, 160)
    # the gateway is only slow: its answer is still on the way and comes with the next report
    pending = rpc is None and bool(rpc_error) and "ยังไม่ตอบภายใน" in rpc_error
    log_ch = log.get("channels") or {}
    names = sorted(set((rpc or {}).get("channels", {})) | set(log_ch))
    wd = _watchdog_events()
    rows, bits, worst = [], [], OK

    def bump(status: str) -> None:
        nonlocal worst
        if RANK[status] > RANK[worst]:
            worst = status

    for name in names:
        live = (rpc or {}).get("channels", {}).get(name, {})
        hist = log_ch.get(name, {})
        label = (((rpc or {}).get("labels") or {}).get(name)
                 or {"whatsapp": "WhatsApp", "telegram": "Telegram", "line": "LINE"}.get(name) or name.capitalize())
        row = {"channel": name, "label": label, **live,
               "terminal_disconnects_today": hist.get("terminal_disconnects", 0),
               "last_terminal_disconnect": _fmt_time(hist.get("last_terminal_disconnect")),
               "warnings_today": hist.get("warnings", 0), "errors_today": hist.get("errors", 0)}
        rows.append(row)
        if rpc is not None and not live:
            bits.append(f"{label}: ไม่ได้เปิดใน gateway ตอนนี้ (พบแค่ในล็อก)")
            continue
        if rpc is not None:
            if live.get("enabled") is False or live.get("configured") is False:
                bits.append(f"{label}: ปิดใช้งาน")
                continue
            healthy = bool(live.get("running")) and bool(live.get("connected"))
            # the watchdog's actions since the latest outage (an earlier outage's actions say nothing about this one)
            td_ts = hist.get("last_terminal_disconnect") or 0.0
            mine = [e for e in wd if e.get("channel") == name and _event_ts(e) >= td_ts - 5]
            if not healthy:
                bump(BAD)
                state = live.get("healthState") or live.get("lifecycle") or "ไม่ได้เชื่อมต่อ"
                txt = f"{label}: หลุด ({_redact(state, 40)})"
                last_ev = mine[-1] if mine else {}
                if channel_needs_login(live) or last_ev.get("action") == "needs-login":
                    txt += " — ต้องเชื่อมบัญชีใหม่ (สแกน QR)" if name == "whatsapp" else " — ต้องเชื่อมบัญชี/ตั้งค่าใหม่"
                elif last_ev.get("action") in ("restart-failed", "restart-not-connected"):
                    txt += (f" — ระบบลองต่อใหม่ให้แล้วตอน {str(last_ev.get('time', ''))[11:16]} แต่ยังไม่ติด"
                            " จะลองอีกเป็นระยะ (ไม่เกิน 3 ครั้งต่อชั่วโมง)")
                elif hist.get("terminal_disconnects"):
                    txt += (" — ระบบจะต่อใหม่ให้เองเมื่ออินเทอร์เน็ตกลับมา" if channel_watchdog_enabled()
                            else " — การต่อใหม่อัตโนมัติปิดอยู่ (QWEN_CHANNEL_WATCHDOG=0)")
                bits.append(txt)
            else:
                txt = f"{label}: เชื่อมต่ออยู่"
                last_td = _fmt_time(hist.get("last_terminal_disconnect"))
                fixed = [e for e in mine if e.get("action") == "restarted"]
                if hist.get("terminal_disconnects") and last_td and fixed:
                    txt += (f" (วันนี้หลุดตอน {last_td[-5:]} แล้วระบบต่อใหม่ให้เองตอน "
                            f"{str(fixed[-1].get('time', ''))[11:16]})")
                elif hist.get("terminal_disconnects") and last_td:
                    txt += f" (วันนี้เคยหลุดแบบไม่ต่อใหม่เอง ล่าสุด {last_td[-5:]} แล้วกลับมา)"
                bits.append(txt)
        else:
            last_td, last_start = hist.get("last_terminal_disconnect"), hist.get("last_start")
            if last_td and (not last_start or last_td > last_start):
                bump(BAD)
                bits.append(f"{label}: หลุดแบบไม่ต่อใหม่เอง (terminal-disconnect ล่าสุด {_fmt_time(last_td)})")
            elif pending:
                bump(UNKNOWN)
                bits.append(f"{label}: สถานะสดยังไม่มา (gateway ตอบช้า กำลังอ่านต่อเบื้องหลัง ผลจะขึ้นในการตรวจรอบถัดไป)")
            else:
                bump(UNKNOWN)
                bits.append(f"{label}: สถานะสดไม่ทราบ (ถาม gateway ไม่ได้)")
    extra = {}
    if rpc is not None:
        if rpc.get("failed_deliveries"):
            bump(WARN)
            # dead letters: OpenClaw keeps them for its retention period and does not resend them
            since = f" (เก่าสุด {rpc['oldest_failed']})" if rpc.get("oldest_failed") else ""
            bits.append(f"ข้อความขาออกที่ส่งไม่สำเร็จค้างในคิว {rpc['failed_deliveries']} รายการ{since}")
        if rpc.get("event_loop_degraded"):
            bump(WARN)
            bits.append("OpenClaw gateway ตอบช้า (event loop degraded)")
        extra["failed_deliveries"] = rpc.get("failed_deliveries")
        extra["oldest_failed_delivery"] = rpc.get("oldest_failed")
    if as_of is not None:
        extra["live_as_of"] = _fmt_time(as_of)
    if pending or not live_now:
        extra["pending_refresh"] = True  # report() does not cache this one
    if not names:
        if rpc_error and not _port_open(18789):
            return _result(BAD, "OpenClaw gateway ไม่ได้รัน (พอร์ต 18789 ปิด)", log_file=log.get("file"))
        return _result(UNKNOWN, "ไม่พบข้อมูลช่องทาง (" + (rpc_error or "ไม่มีล็อก") + ")", log_file=log.get("file"),
                       **extra)
    if wd:
        extra["watchdog_today"] = wd[-5:]
    return _result(worst, " · ".join(bits) + (_late_note(as_of, live_now) if as_of is not None else ""),
                   channels=rows, log_file=log.get("file"), live_source=rpc is not None,
                   live_error=rpc_error, **extra)


# --------------------------------------------------------------------------- automations

def _list_automations_now() -> dict[str, Any]:
    cwd = str(OPENCLAW_RUNTIME) if OPENCLAW_RUNTIME.is_dir() else None
    code, out, err = _run(_openclaw_argv("automations", "list", "--all", "--json"), timeout=OPENCLAW_CLI_TIMEOUT_S,
                          cwd=cwd)
    if code is None:
        raise TimeoutError(f"openclaw automations list ไม่ตอบภายใน {OPENCLAW_CLI_TIMEOUT_S:.0f} วินาที")
    if code != 0 and re.search(r"unknown command|too many arguments", err + out, re.I):
        code, out, err = _run(_openclaw_argv("cron", "list", "--all", "--json"), timeout=OPENCLAW_CLI_TIMEOUT_S,
                              cwd=cwd)
        if code is None:
            raise TimeoutError("openclaw cron list ไม่ตอบ")
    data = _json_from_output(out)
    return data if isinstance(data, dict) else {"jobs": data}


def _list_automations(ctx: dict | None = None) -> tuple[dict[str, Any], float, bool]:
    ctx = ctx or {}
    return _SLOW.get("automations", _list_automations_now, _wait_left(ctx), "openclaw automations list",
                     reuse_s=0.0 if ctx.get("fresh") else CHECKS["automations"]["ttl"])


def check_automations(ctx: dict) -> dict:
    try:
        data, as_of, live_now = _list_automations(ctx)
    except Exception as exc:
        if not _port_open(18789):
            return _result(UNKNOWN, "OpenClaw gateway ไม่ได้รัน อ่านรายการงานตั้งเวลาไม่ได้")
        if "ยังไม่ตอบภายใน" in str(exc):  # only slow: the answer comes with the next report
            return _result(UNKNOWN, "รายการงานตั้งเวลายังไม่มา: " + _redact(exc, 160), pending_refresh=True)
        return _result(UNKNOWN, "อ่านรายการงานตั้งเวลาไม่ได้: " + _redact(exc, 160))
    note = _late_note(as_of, live_now)
    extra: dict[str, Any] = {"live_as_of": _fmt_time(as_of)}
    if not live_now:
        extra["pending_refresh"] = True
    jobs, bits = [], []
    worst = OK
    for job in data.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        st = job.get("state") if isinstance(job.get("state"), dict) else {}
        last_status = str(job.get("lastRunStatus") or st.get("lastRunStatus") or st.get("lastStatus") or "")
        errors = int(st.get("consecutiveErrors") or 0)
        last_at = job.get("lastRunAtMs") or st.get("lastRunAtMs")
        next_at = job.get("nextRunAtMs") or st.get("nextRunAtMs")
        err_text = job.get("lastRunError") or st.get("lastError") or ""
        name = _redact(job.get("name") or job.get("displayName") or "?", 60)
        enabled = bool(job.get("enabled"))
        item = {"name": name, "enabled": enabled, "last_status": last_status or None,
                "consecutive_errors": errors, "last_run": _fmt_time(last_at / 1000) if last_at else None,
                "next_run": _fmt_time(next_at / 1000) if next_at and enabled else None}
        failed = last_status.lower() in ("error", "failed", "timeout", "timed_out")
        if err_text and (failed or last_status.lower() == "skipped"):
            item["last_error"] = _redact(err_text, 200)
        jobs.append(item)
        if enabled and failed:
            status = BAD if errors >= 3 else WARN
            if RANK[status] > RANK[worst]:
                worst = status
            bits.append(f"{name} ล้มเหลว" + (f" {errors} ครั้งติด" if errors > 1 else "")
                        + (f": {item['last_error'][:110]}" if item.get("last_error") else ""))
    enabled_count = sum(j["enabled"] for j in jobs)
    head = f"{len(jobs)} งาน (เปิด {enabled_count})"
    if bits:
        return _result(worst, head + " · " + " · ".join(bits) + note, jobs=jobs, **extra)
    return _result(OK, head + " · ไม่มีงานที่ล้มเหลวล่าสุด" + note, jobs=jobs, **extra)


# --------------------------------------------------------------------------- Open WebUI OAuth

def check_oauth(ctx: dict) -> dict:
    if not WEBUI_DB.is_file():
        return _result(UNKNOWN, "ไม่พบฐานข้อมูล Open WebUI")
    uri = "file:" + WEBUI_DB.as_posix() + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error as exc:
        return _result(UNKNOWN, "เปิดฐานข้อมูลแบบอ่านอย่างเดียวไม่ได้: " + _redact(exc, 80))
    try:
        con.execute("PRAGMA query_only = 1")
        # provider and expiry only - the token column is never selected
        rows = con.execute("SELECT provider, expires_at FROM oauth_session").fetchall()
        # Sessions of an MCP tool server that is no longer configured can never be used again
        # (the Runway ids changed on 2026-09-19); their expiry must not raise an alarm.
        configured = None
        row = con.execute("SELECT value FROM config WHERE key='tool_server.connections'").fetchone()
        if row and isinstance(row[0], str):
            try:
                configured = {"mcp:" + str((c.get("info") or {}).get("id") or "") for c in json.loads(row[0])}
            except (ValueError, AttributeError, TypeError):
                configured = None
        orphaned = 0
        if configured is not None:
            kept = [r for r in rows if not str(r[0] or "").startswith("mcp:") or str(r[0]) in configured]
            orphaned = len(rows) - len(kept)
            rows = kept
    except sqlite3.Error as exc:
        msg = str(exc)
        if "no such table" in msg:
            return _result(OK, "ไม่มีการล็อกอิน OAuth ใน Open WebUI", providers=[])
        return _result(UNKNOWN, "อ่านตาราง oauth_session ไม่ได้: " + _redact(msg, 80))
    finally:
        con.close()
    now = _now()
    families: dict[str, dict[str, Any]] = {}
    for provider, expires in rows:
        # "mcp:runway-<label>" -> "runway": the family is enough to act on, labels stay private
        fam = re.split(r"[-_]", str(provider or "").split(":", 1)[-1], maxsplit=1)[0] or "oauth"
        fam = _redact(fam, 30)
        try:
            exp = float(expires)
            exp = exp / 1000 if exp > 1e11 else exp
        except (TypeError, ValueError):
            exp = None
        f = families.setdefault(fam, {"provider": fam, "sessions": 0, "earliest_expiry_ts": None})
        f["sessions"] += 1
        if exp and (f["earliest_expiry_ts"] is None or exp < f["earliest_expiry_ts"]):
            f["earliest_expiry_ts"] = exp
    providers, bits, worst = [], [], OK
    for fam in sorted(families.values(), key=lambda f: f["earliest_expiry_ts"] or 0):
        exp = fam.pop("earliest_expiry_ts")
        days = (exp - now) / 86400 if exp else None
        fam.update(earliest_expiry=_fmt_time(exp), days_left=round(days, 1) if days is not None else None)
        providers.append(fam)
        if days is None:
            continue
        if days < 0:
            worst = BAD
            bits.append(f"{fam['provider']} หมดอายุแล้ว ({fam['earliest_expiry']}) — ต้องล็อกอินใหม่ใน Open WebUI")
        elif days < OAUTH_WARN_DAYS:
            worst = WARN if worst != BAD else worst
            bits.append(f"{fam['provider']} จะหมดอายุ {fam['earliest_expiry'][:10]} (อีก {int(days)} วัน) — ล็อกอินใหม่ก่อนวันนั้น")
    note = ""
    if orphaned:
        # e.g. Runway after the 2026-09-19 id change. Shown as information: re-registering does
        # not delete the old rows, so a warning here could never clear (and would sit in every
        # morning brief).
        note = (f"การล็อกอินเครื่องมือ MCP เก่า {orphaned} รายการไม่ได้ใช้แล้ว (ถ้าจะใช้ Runway ใน WebUI: "
                "Admin › Settings › External Tools › OAuth 2.1 › Register Client แล้วล็อกอินในแชท)")
    if not providers and not bits:
        return _result(OK, note or "ไม่มีการล็อกอิน OAuth ใน Open WebUI", providers=[], orphaned_sessions=orphaned)
    if bits:
        return _result(worst, " · ".join(bits), providers=providers)
    return _result(OK, " · ".join(f"{p['provider']} หมดอายุ {p['earliest_expiry'][:10] if p['earliest_expiry'] else '?'}"
                                  for p in providers), providers=providers)


# --------------------------------------------------------------------------- Project sync

PROJECT_SYNC_STATUS = SUITE_ROOT / "data" / "project-sync.json"
PROJECT_SYNC_ENDPOINTS = ("/api/v1/folders", "/api/v1/memories", "/api/v1/files")
_OWNER_SQL = (
    "SELECT id FROM user ORDER BY CASE WHEN lower(email) IN ('admin@localhost','admin') "
    "OR lower(COALESCE(username,''))='admin' THEN 0 WHEN role='admin' THEN 1 ELSE 2 END, created_at LIMIT 1"
)


def check_project_sync(ctx: dict) -> dict:
    """Projects reaching Open WebUI (folders, memories, attachments): broken from 1.6.1 to 1.6.2."""
    if not WEBUI_DB.is_file():
        return _result(UNKNOWN, "ไม่พบฐานข้อมูล Open WebUI")
    try:
        con = sqlite3.connect("file:" + WEBUI_DB.as_posix() + "?mode=ro", uri=True, timeout=2)
    except sqlite3.Error as exc:
        return _result(UNKNOWN, "เปิดฐานข้อมูลแบบอ่านอย่างเดียวไม่ได้: " + _redact(exc, 80))
    try:
        con.execute("PRAGMA query_only = 1")

        def cfg(key: str) -> Any:
            row = con.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
            try:
                return json.loads(row[0]) if row else None
            except (TypeError, ValueError):
                return row[0]

        enabled = cfg("auth.enable_api_keys") is True
        restricted = cfg("auth.api_key.endpoint_restrictions") is True
        allowed = [p.strip() for p in str(cfg("auth.api_key.allowed_endpoints") or "").split(",") if p.strip()]
        owner = con.execute(_OWNER_SQL).fetchone()
        # existence only - the key column is never selected
        has_key = bool(owner) and con.execute("SELECT 1 FROM api_key WHERE user_id=?", (owner[0],)).fetchone() is not None
    except sqlite3.Error as exc:
        return _result(UNKNOWN, "อ่านค่า API key ของ Open WebUI ไม่ได้: " + _redact(exc, 80))
    finally:
        con.close()
    missing = [p for p in PROJECT_SYNC_ENDPOINTS if restricted and p not in allowed]
    try:
        last = json.loads(PROJECT_SYNC_STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = {}
    pending = last.get("pending_after") or {}
    details = {"api_keys_enabled": enabled, "endpoint_restrictions": restricted, "owner_key": has_key,
               "last_run": last.get("at"), "last_status": last.get("status"), "pending": pending}
    fix = " — รัน Repair Qwen Suite (หรือ Configure-Qwen-WebUI.py) เพื่อตั้งค่าให้"
    if not enabled:
        return _result(BAD, "Project ไม่ sync เข้า Open WebUI: ปิด API keys อยู่ (Admin Settings › General)" + fix, **details)
    if not has_key:
        return _result(BAD, "Project ไม่ sync เข้า Open WebUI: บัญชีเจ้าของเครื่องไม่มี API key" + fix, **details)
    if missing:
        return _result(BAD, "Project ไม่ sync เข้า Open WebUI: API key ถูกจำกัด endpoint และไม่รวม " + ", ".join(missing) + fix, **details)
    if last.get("status") == "error":
        return _result(WARN, "sync ล่าสุดล้มเหลว: " + _redact(last.get("message"), 160), **details)
    waiting = int(pending.get("projects") or 0)
    if waiting:
        return _result(WARN, f"มี {waiting} Project รอ sync (โฟลเดอร์ {pending.get('folders', 0)}, ความจำ "
                             f"{pending.get('memories', 0)}, ไฟล์ {pending.get('files', 0)}) — ลองใหม่ทุก 10 นาที", **details)
    if not last:
        return _result(OK, "พร้อม sync (ยังไม่มีผลรอบแรก — เริ่มหลัง local-tools และ Open WebUI ขึ้นครบ)", **details)
    return _result(OK, "Project sync เข้า Open WebUI ใช้ได้ (ตรวจล่าสุด " + str(last.get("at", ""))[:16].replace("T", " ") + ")", **details)


# --------------------------------------------------------------------------- README vs tools

_RE_README_TOOLS = re.compile(r"^\|\s*8765\s*\|.*?(\d+)\s*tools", re.M | re.I)


def _mcp_tool_count_http() -> int:
    data = _http_json(LOCAL_TOOLS_MCP, timeout=5,
                      body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                      headers={"Accept": "application/json, text/event-stream"})
    return len(((data or {}).get("result") or {}).get("tools") or [])


def check_readme_tools(ctx: dict) -> dict:
    try:
        text = README.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _result(UNKNOWN, "ไม่พบ README.md")
    m = _RE_README_TOOLS.search(text)
    documented = int(m.group(1)) if m else None
    provider = ctx.get("tool_count")
    live, source = None, None
    if provider is not None:
        try:
            live, source = int(provider()), "in-process"
        except Exception:
            live = None
    if live is None:
        try:
            live, source = _mcp_tool_count_http(), "tools/list"
        except Exception:
            return _result(UNKNOWN, "ถาม tools/list ที่ :8765 ไม่ได้", documented=documented)
    details = {"documented": documented, "live": live, "source": source}
    if documented is None:
        return _result(WARN, f"README ไม่ระบุจำนวน tools ของ :8765 (ที่รันอยู่มี {live})", **details)
    if documented != live:
        return _result(WARN, f"README บอก {documented} tools แต่ :8765 มี {live} — README ล้าสมัย", **details)
    return _result(OK, f"README ตรงกับ :8765 ({live} tools)", **details)


# --------------------------------------------------------------------------- installer payload parity

def check_payload(ctx: dict) -> dict:
    if not PARITY_SCRIPT.is_file():
        return _result(UNKNOWN, "ไม่พบ Check-PayloadParity.py (โฟลเดอร์ installer ไม่อยู่ในเครื่องนี้)")
    python = str(PYTHON) if PYTHON.is_file() else sys.executable
    code, out, _err = _run([python, str(PARITY_SCRIPT)], timeout=11, cwd=str(PARITY_SCRIPT.parent))
    if code is None:
        return _result(UNKNOWN, "ตรวจ payload ไม่เสร็จใน 11 วินาที")
    counts = {}
    m = re.search(r"compared (\d+) files - identical (\d+), allowed differences (\d+), unexpected (\d+), "
                  r"unknown payload-only (\d+)", out)
    if m:
        counts = dict(zip(("compared", "identical", "allowed", "unexpected", "payload_only"), map(int, m.groups())))
    issues = []
    for line in out.splitlines():
        s = line.strip()
        for tag in ("DRIFT", "ONLY IN PAYLOAD", "ONLY IN DEPLOYMENT", "NOT INSTALLED"):
            if s.startswith(tag):
                rel = re.sub(r"\s+payload=.*$", "", s)
                issues.append(_redact(rel, 140))
    details = {"exit_code": code, **counts, "issues": issues[:12], "issue_count": len(issues)}
    if code == 0:
        return _result(OK, f"payload ของตัวติดตั้งตรงกับของจริง ({counts.get('compared', '?')} ไฟล์)", **details)
    lead = f"payload ไม่ตรงกับของจริง {len(issues)} รายการ"
    if issues:
        lead += ": " + "; ".join(issues[:3]) + (" …" if len(issues) > 3 else "")
    return _result(WARN, lead + " — sync ก่อน build ตัวติดตั้ง", **details)


# --------------------------------------------------------------------------- processes

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def _process_table() -> list[tuple[int, int, str]]:
    """(pid, parent pid, exe name) for every process. A Toolhelp snapshot takes ~20 ms here;
    psutil needs ~3.6 s for the same list of ~480 processes, most of the time budget."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        raise OSError("CreateToolhelp32Snapshot failed")
    rows = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            rows.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return rows


def check_processes(ctx: dict) -> dict:
    try:
        table = _process_table()
    except Exception as exc:
        return _result(UNKNOWN, "อ่านรายการโปรเซสไม่ได้: " + _redact(exc, 80))
    names = {pid: name.lower() for pid, _pp, name in table}
    repl = [(pid, ppid) for pid, ppid, name in table if name.lower() == "node_repl.exe"]
    llama = sum(1 for _p, _pp, n in table if n.lower() == "llama-server.exe")
    ollama = sum(1 for _p, _pp, n in table if n.lower() == "ollama.exe")
    parents: dict[str, int] = {}
    for _pid, ppid in repl:
        parent = names.get(ppid, "(จบไปแล้ว)")
        parents[parent] = parents.get(parent, 0) + 1
    rss_mb = None
    try:
        import psutil  # only the matching pids, so this stays fast
        total = 0
        for pid, _pp in repl[:200]:
            try:
                total += psutil.Process(pid).memory_info().rss
            except Exception:
                pass
        rss_mb = round(total / 2**20)
    except ImportError:
        pass
    details = {"node_repl": len(repl), "node_repl_parents": parents, "node_repl_ram_mb": rss_mb,
               "llama_server": llama, "ollama": ollama, "total_processes": len(table)}
    ram = f" ใช้แรมรวม ~{rss_mb:,} MB" if rss_mb else ""
    owner = ""
    if parents:
        top = max(parents.items(), key=lambda kv: kv[1])[0]
        owner = " (มาจาก Codex Desktop)" if top.startswith("codex") else f" (ตัวแม่: {top})"
    if len(repl) > NODE_REPL_WARN:
        return _result(WARN, f"node_repl.exe ค้างอยู่ {len(repl)} ตัว{owner}{ram} — รายงานอย่างเดียว ไม่ได้ปิดให้; "
                             "ปิด Codex Desktop เมื่อไม่ใช้จะคืนแรม", **details)
    return _result(OK, f"node_repl.exe {len(repl)} ตัว · llama-server {llama} ตัว · ollama {ollama} ตัว", **details)


# --------------------------------------------------------------------------- runner

CHECKS: dict[str, dict[str, Any]] = {
    "ports": {"title": "บริการ/พอร์ต", "fn": check_ports, "ttl": 5},
    "ollama": {"title": "Ollama ตอนนี้", "fn": check_ollama, "ttl": 5},
    "ollama_config": {"title": "ค่าของ Ollama / GPU (ทาง B)", "fn": check_ollama_config, "ttl": 15},
    "models": {"title": "โมเดลที่ชุดเรียกใช้", "fn": check_models, "ttl": 60},
    "display": {"title": "การ์ดจอ", "fn": check_display, "ttl": 120, "heavy": True},
    "disk": {"title": "พื้นที่ดิสก์", "fn": check_disk, "ttl": 15},
    "channels": {"title": "Telegram / WhatsApp", "fn": check_channels, "ttl": 30, "heavy": True},
    "automations": {"title": "งานตั้งเวลา OpenClaw", "fn": check_automations, "ttl": 60, "heavy": True},
    "oauth": {"title": "OAuth ใน Open WebUI", "fn": check_oauth, "ttl": 300},
    "project_sync": {"title": "Project sync เข้า Open WebUI", "fn": check_project_sync, "ttl": 60},
    "readme_tools": {"title": "README กับจำนวน tools", "fn": check_readme_tools, "ttl": 60},
    "payload": {"title": "payload ตัวติดตั้ง", "fn": check_payload, "ttl": 300, "heavy": True},
    "processes": {"title": "โปรเซสค้าง", "fn": check_processes, "ttl": 15},
}
LIGHT_CHECKS = tuple(k for k, v in CHECKS.items() if not v.get("heavy"))

# key -> (wall time, perf_counter stamp, result); the high-resolution stamp orders a result
# against a request exactly (time.time() ticks in ~15 ms steps on Windows)
_CACHE: dict[str, tuple[float, float, dict[str, Any]]] = {}
_RUN_LOCK = threading.Lock()


def _select(only: str | list[str] | None) -> list[str]:
    if not only:
        return list(CHECKS)
    if isinstance(only, str):
        if only.strip().lower() == "light":
            return list(LIGHT_CHECKS)
        only = [x.strip() for x in only.split(",")]
    keys = [k for k in only if k in CHECKS]
    if not keys:
        raise ValueError("unknown check name(s); valid: light, " + ", ".join(CHECKS))
    return keys


def report(only: str | list[str] | None = None, fresh: bool = False,
           tool_count: Callable[[], int] | None = None, deadline: float = DEADLINE_S) -> dict[str, Any]:
    """Run the selected checks in parallel (each fails soft) and return the full report."""
    keys = _select(only)
    started = time.monotonic()
    requested = time.perf_counter()
    if not _RUN_LOCK.acquire(timeout=deadline + 2):
        raise RuntimeError("การตรวจรอบก่อนยังไม่เสร็จ ลองใหม่ในอีกครู่")
    try:
        now = _now()
        results: dict[str, dict[str, Any]] = {}
        todo = []
        for key in keys:
            cached = _CACHE.get(key)
            # fresh = measured after this request arrived, so callers that queued behind a
            # running check share its result instead of each starting the helpers again
            usable = cached and (cached[1] > requested if fresh else now - cached[0] < CHECKS[key]["ttl"])
            if usable:
                results[key] = dict(cached[2], cached_s=round(now - cached[0]))
            else:
                todo.append(key)
        if todo:
            ctx = {"shared": _Shared(), "tool_count": tool_count, "fresh": fresh, "deadline_at": started + deadline}
            pool = ThreadPoolExecutor(max_workers=len(todo), thread_name_prefix="suite-health")
            futures = {key: pool.submit(CHECKS[key]["fn"], ctx) for key in todo}
            wait(list(futures.values()), timeout=max(1.0, deadline - (time.monotonic() - started)))
            for key, fut in futures.items():
                if not fut.done():
                    results[key] = _result(UNKNOWN, f"ตรวจไม่เสร็จภายใน {deadline:.0f} วินาที")
                    continue
                try:
                    res = fut.result()
                    # an answer still on its way (OpenClaw CLI) is not cached: the next report
                    # picks up the late answer instead of this placeholder
                    if not (res.get("details") or {}).get("pending_refresh"):
                        _CACHE[key] = (_now(), time.perf_counter(), res)
                    results[key] = res
                except Exception as exc:
                    results[key] = _result(UNKNOWN, "ตรวจไม่สำเร็จ: " + _redact(f"{type(exc).__name__}: {exc}", 120))
            # a check that overran keeps running in its thread until its own timeout kills
            # its child process; the caller does not wait for it
            pool.shutdown(wait=False, cancel_futures=True)
    finally:
        _RUN_LOCK.release()
    ordered = {k: dict(results[k], key=k, title=CHECKS[k]["title"]) for k in keys}
    overall = max((r["status"] for r in ordered.values()), key=lambda s: RANK[s], default=OK)
    counts = {s: sum(1 for r in ordered.values() if r["status"] == s) for s in (OK, WARN, BAD, UNKNOWN)}
    problems = [f"{r['title']}: {r['summary']}" for r in sorted(ordered.values(), key=lambda r: -RANK[r["status"]])
                if r["status"] in (WARN, BAD)]
    return {"overall": overall, "overall_th": STATUS_TH[overall], "counts": counts,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "took_s": round(time.monotonic() - started, 2), "page": STATUS_PAGE,
            "checks": ordered, "problems": problems}


def prewarm_openclaw(delay: float = 90.0) -> None:
    """Server start: read the OpenClaw state once in the background a little later, so the first
    report after a restart has an answer and Node's files are warm. Read-only, never raises."""

    def runner() -> None:
        time.sleep(max(0.0, delay))
        try:
            if _port_open(18789):
                _SLOW.start("gateway_health", _gateway_health_now)
                _SLOW.start("automations", _list_automations_now)
        except Exception:
            pass

    threading.Thread(target=runner, name="suite-health-prewarm", daemon=True).start()


def compact(rep: dict[str, Any]) -> dict[str, Any]:
    """Short form for a model on the CPU: one line per check, ~1-2 KB instead of ~8 KB."""
    return {
        "overall": rep["overall"], "overall_th": rep["overall_th"], "checked_at": rep["checked_at"],
        "took_s": rep["took_s"],
        "lines": [f"[{r['status_th']}] {r['title']}: {r['summary']}" for r in rep["checks"].values()],
        "detail_page": rep["page"],
        "note": "สถานะอย่างเดียว ไม่มีค่าลับ; ตัวเลขเวลาเป็นเวลาเครื่อง (GMT+7)",
    }


def format_text(rep: dict[str, Any]) -> str:
    lines = [f"Qwen Suite — สถานะรวม: {rep['overall_th']}  ({rep['checked_at']}, ใช้เวลา {rep['took_s']} วินาที)"]
    for r in rep["checks"].values():
        lines.append(f"  [{r['status_th']}] {r['title']}: {r['summary']}")
    lines.append(f"รายละเอียด: {rep['page']}")
    return "\n".join(lines)


def exit_code(rep: dict[str, Any]) -> int:
    return {BAD: 2, WARN: 1}.get(rep["overall"], 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qwen Suite health report (read-only)")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--compact", action="store_true", help="print the short JSON a model receives")
    parser.add_argument("--only", default="", help="comma list of checks, or 'light'")
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        rep = report(only=args.only or None, fresh=True, deadline=SCRIPT_DEADLINE_S)
    except ValueError as exc:
        print(f"เลือกหัวข้อไม่ถูกต้อง ({exc})")
        return 3
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    elif args.compact:
        print(json.dumps(compact(rep), ensure_ascii=False, indent=2, default=str))
    else:
        print(format_text(rep))
    sys.stdout.flush()
    _SLOW.wait_running(OPENCLAW_CLI_TIMEOUT_S + 5)
    return exit_code(rep)


if __name__ == "__main__":
    sys.exit(main())
