"""Choose the processing unit (CPU / which GPU) for the local LLM - shared by the chat tool
(compute_device / list_compute_devices) and the /compute page. Added 2026-09-29.

Import-safe: nothing runs at import time. All real work is done by the PowerShell scripts in
the suite root, so the menu, the page and chat behave the same:
  Select-QwenCompute.ps1      server-level choice: auto | cpu | GPU(s)  -> data/compute-selection.json
  Set-QwenModelPlacement.ps1  per-model: keep one model on the CPU      -> data/model-placement.json
The resident watcher (watch-ollama-gpu.ps1) applies a server-level change by restarting Ollama
once it is idle, so a chat that asks for a change can finish its answer first.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

SUITE_ROOT = Path(__file__).resolve().parent.parent
DATA = SUITE_ROOT / "data"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
SELECT_PS1 = SUITE_ROOT / "Select-QwenCompute.ps1"
PLACEMENT_PS1 = SUITE_ROOT / "Set-QwenModelPlacement.ps1"
OLLAMA = "http://127.0.0.1:11434"
# A CPU choice made from chat falls back to auto after this long unless the user gave hours:
# on the CPU alone a long OpenClaw prompt can take longer than OpenClaw's timeout, so the
# user could otherwise lock themselves out of switching back from Telegram.
CHAT_CPU_DEFAULT_HOURS = 4.0
CHAT_MAX_HOURS = 72.0

_LOCK = threading.Lock()
_REFRESH = threading.Lock()
# No leading '-' (PowerShell parameter injection); names like "RTX 4090", "P620", UUIDs.
_DEVICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,79}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}(?::[A-Za-z0-9._-]{1,60})?$")
_devices_cache: dict[str, Any] = {"at": 0.0, "report": None}


def _ps_json(script: Path, args: list[str], timeout: int = 120) -> dict[str, Any]:
    """Run a suite script with -Json. Output goes to a file, not a pipe, so a grandchild that
    inherited the handle (nvidia-smi) can never keep us waiting past the timeout."""
    if not script.is_file():
        raise FileNotFoundError(f"{script.name} is not installed next to the suite: {script}")
    argv = [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), *args]
    with tempfile.TemporaryDirectory(prefix="qwen-compute-") as tmp:
        out_path, err_path = Path(tmp) / "out.txt", Path(tmp) / "err.txt"
        with out_path.open("wb") as out, err_path.open("wb") as err:
            proc = subprocess.Popen(argv, cwd=str(SUITE_ROOT), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                code = None
        text = out_path.read_bytes().decode("utf-8", "replace")
        errtext = err_path.read_bytes().decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        line = line.strip().lstrip("\ufeff")
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    if code is None:
        raise RuntimeError(f"{script.name} did not answer within {timeout} s")
    raise RuntimeError(f"{script.name} exit {code}: {(errtext or text).strip()[:600]}")


def _locked(fn, *args, wait: float = 60.0):
    if not _LOCK.acquire(timeout=wait):
        raise RuntimeError("another processing-unit change is still running; try again in a minute")
    try:
        return fn(*args)
    finally:
        _LOCK.release()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _get(path: str, timeout: float = 5.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(OLLAMA + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _selection_key(sel: dict[str, Any] | None) -> str:
    """Mirror of Get-QwenSelectionKey in Resolve-OllamaGpu.ps1."""
    if not sel:
        return "auto"
    expires = sel.get("expires_at")
    if expires:
        try:
            if datetime.fromisoformat(str(expires)[:26]).replace(tzinfo=None) < datetime.now():
                return "auto"
        except ValueError:
            pass
    device = str(sel.get("device") or "")
    if device == "cpu":
        return "cpu"
    if device == "gpu":
        ids = sorted(str(g) for g in (sel.get("gpus") or []) if g)
        if ids:
            return "gpu:" + ",".join(ids)
    return "auto"


def _restart_pending() -> bool:
    if (DATA / "ollama-restart.json").exists():
        return True
    try:  # a restart is waiting for idle if its mutex exists
        handle = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, "Local\\QwenSuite-OllamaRestart")
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
    except Exception:
        pass
    return False


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    s = report.get("state") or {}
    devices = []
    listed = report.get("devices") or []
    if isinstance(listed, dict):  # a one-row list serialised by PowerShell as a single object
        listed = [listed]
    for d in listed:
        item = {"kind": d.get("Kind"), "name": d.get("Name"), "detail": d.get("Detail")}
        if d.get("Kind") in ("gpu", "absent"):
            item["id"] = d.get("Id")
        elif d.get("Uuid"):
            # a remembered card Windows shows but that cannot run (so a chosen one can be seen)
            item["uuid"] = d.get("Uuid")
        if d.get("Note"):
            item["note"] = d.get("Note")
        devices.append(item)
    return {"devices": devices, "chosen": s.get("choice"), "running_now": s.get("running"),
            "applied": bool(s.get("applied")), "ollama_running": bool(s.get("server_running")),
            "choice_fallback": bool(s.get("fallback")),
            "until": s.get("expires_at"), "manual_override": s.get("manual_override") or [],
            "legacy_mode": s.get("legacy_mode"), "note": s.get("note") or ""}


def _record_stamp() -> tuple[str, str]:
    record = _read_json(DATA / "gpu-profile.json") or {}
    return str(record.get("written_at") or ""), str(record.get("pnp_signature") or "")


def list_devices() -> dict[str, Any]:
    stamp = _record_stamp()
    report = _ps_json(SELECT_PS1, ["-List", "-Json"])
    _devices_cache.update(at=time.time(), report=report, stamp=stamp)
    return _summary(report)


def _parse_hours(hours: Any) -> float | None:
    if hours in (None, ""):
        return None
    try:
        return float(hours)
    except (TypeError, ValueError):
        raise ValueError("hours must be a number (0 or empty = no time limit)") from None


def select(device: str, hours: Any = None, apply: str = "idle", by: str = "chat") -> dict[str, Any]:
    tokens = [t.strip() for t in str(device or "").split(",") if t.strip()]
    tokens = [t.lower() if t.lower() in ("auto", "cpu") else t for t in tokens]
    if not tokens or len(tokens) > 4 or not all(_DEVICE.match(t) for t in tokens):
        raise ValueError("device must be auto, cpu, part of a GPU name (e.g. 4090, RTX 4060) or a GPU id "
                         "from list_compute_devices; several GPUs separated by commas")
    # 1-2 digit tokens are menu list numbers, which change when cards are swapped; longer
    # digit runs such as 4090 are GPU names.
    if by == "chat" and any(t.isdigit() and len(t) <= 2 for t in tokens):
        raise ValueError("from chat use the GPU name (e.g. 4090, 4060) or id, not a list number")
    if apply not in {"idle", "now"} or (apply == "now" and by != "web"):
        raise ValueError("apply must be idle (the change is applied once Ollama is idle)")
    h = _parse_hours(hours)
    if by == "chat":
        if tokens == ["cpu"] and (h is None or h <= 0):
            h = CHAT_CPU_DEFAULT_HOURS
        if h is not None and h > CHAT_MAX_HOURS:
            h = CHAT_MAX_HOURS
    args = ["-Device", ",".join(tokens), "-Apply", apply, "-Json", "-By", f"local-tools:{by}"]
    if h is not None and h > 0:
        args += ["-Hours", f"{h:g}"]
    stamp = _record_stamp()
    report = _locked(_ps_json, SELECT_PS1, args, 420 if apply == "now" else 120)
    _devices_cache.update(at=time.time(), report=report, stamp=stamp)
    result = {"status": report.get("status") or "error", "message": report.get("message") or "", **_summary(report)}
    if result["status"] == "saved" and apply == "idle":
        result["message"] += (" Ollama switches by itself once it has been idle for about a minute (after this"
                              " answer); the next message then loads the model on the new unit.")
        result["next_action"] = ("Tell the user this in one or two sentences and end the turn. Do not call "
                                 "compute_device again to wait for it.")
    if tokens == ["cpu"] and result["status"] in ("saved", "extended"):
        result["warning"] = ("CPU only is usually much slower at reading long prompts than with a GPU"
                             + (f"; this choice returns to auto after {h:g} h" if h and h > 0 else "") + ".")
    return result


def placement_status() -> dict[str, Any]:
    return _ps_json(PLACEMENT_PS1, ["-Json"])


def set_model_placement(model: str, placement: str, num_ctx: Any = 0, by: str = "chat") -> dict[str, Any]:
    model = str(model or "").strip()
    if not _MODEL.match(model):
        raise ValueError("model must be an installed model name such as qwen3-embedding:4b")
    names = [m.get("name") for m in (_get("/api/tags") or {}).get("models") or []]
    if names and model not in names:
        raise ValueError(f"model '{model}' is not installed; installed: {', '.join(n for n in names if n)}")
    placement = str(placement or "").strip().lower()
    if placement not in {"cpu", "auto"}:
        raise ValueError("placement must be cpu (keep this model on the CPU) or auto (give it back to the GPU choice)")
    try:
        ctx = int(num_ctx or 0)
    except (TypeError, ValueError):
        raise ValueError("num_ctx must be a whole number") from None
    if ctx < 0 or ctx > 262144:
        raise ValueError("num_ctx must be between 0 (keep the model's own) and 262144")
    args = ["-Model", model, "-Placement", placement, "-Json", "-By", f"local-tools:{by}"]
    if placement == "cpu" and ctx > 0:
        args += ["-NumCtx", str(ctx)]
    # lock wait 30 s + server-start lock up to 120 s + create 180 s, with room to spare
    return _locked(_ps_json, PLACEMENT_PS1, args, 420)


def _placements(installed: dict[str, str]) -> dict[str, str]:
    """cpu / lost / pending per model, from the state file and the live digests."""
    out: dict[str, str] = {}
    placement = _read_json(DATA / "model-placement.json") or {}
    for name, entry in (placement.get("models") or {}).items():
        if not isinstance(entry, dict) or entry.get("placement") != "cpu":
            continue
        if entry.get("pending"):
            out[name] = "pending"
        elif installed.get(name) and installed[name] == str(entry.get("pinned_digest") or ""):
            out[name] = "cpu"
        else:
            out[name] = "lost"
    return out


def state(refresh_devices: bool = False) -> dict[str, Any]:
    """Cheap view for the page: files + /api/ps + /api/tags; runs nvidia-smi only when asked
    (one refresh at a time; the last report is reused if a refresh fails)."""
    # The device list costs an nvidia-smi run, so the 10 s page poll reuses it - except when
    # the server was restarted or the devices changed since (the record then changes too).
    if refresh_devices or not _devices_cache["report"] or _devices_cache.get("stamp") != _record_stamp():
        if _REFRESH.acquire(blocking=False):
            try:
                list_devices()
            except Exception:
                # do not retry on every 10 s poll; the next record change or ?devices=1 does
                _devices_cache["stamp"] = _record_stamp()
                if not _devices_cache["report"]:
                    raise
            finally:
                _REFRESH.release()
    report = _devices_cache["report"] or {}
    summary = _summary(report)
    sel = _read_json(DATA / "compute-selection.json")
    record = _read_json(DATA / "gpu-profile.json") or {}
    want = _selection_key(sel)
    have = str(record.get("selection_key") or "auto")
    used = {str(g.get("uuid")) for g in record.get("gpus_used") or [] if isinstance(g, dict)}
    present = {d.get("id") for d in summary["devices"] if d.get("kind") == "gpu"}
    pending = want != have
    fallback = False
    if not pending and want.startswith("gpu:"):
        chosen = set(want[4:].split(","))
        if record.get("mode") == "selected":
            pending = any(c in present and c not in used for c in chosen)
        else:
            # None of the chosen cards is attached and usable (in its box, disabled - the P620
            # on path B - or without a driver): the automatic choice runs on purpose, nothing
            # waits for idle. A chosen card that is attached makes it pending again.
            pending = any(c in present for c in chosen)
            fallback = not pending
    loaded = []
    for m in (_get("/api/ps") or {}).get("models") or []:
        size = int(m.get("size") or 0)
        vram = int(m.get("size_vram") or 0)
        loaded.append({"model": m.get("name"), "gpu_percent": round(100 * vram / size) if size else 0,
                       "gb": round(size / 2**30, 1)})
    installed = {str(m.get("name")): "sha256:" + str(m.get("digest"))
                 for m in (_get("/api/tags") or {}).get("models") or []}
    return {
        **summary,
        "choice_key": want,
        "running_key": have,
        "switch_pending": pending,
        "choice_fallback": fallback,
        "cpu_only": record.get("cpu_only"),
        "restart_pending": _restart_pending(),
        "profile": record.get("profile"),
        "vram_class": record.get("vram_class"),
        "gpus_used": [g.get("name") for g in record.get("gpus_used") or [] if isinstance(g, dict)],
        "record_note": record.get("note") or "",
        "loaded_models": loaded,
        "model_placement": _placements(installed),
        "installed_models": sorted(installed),
        "devices_listed_at": _devices_cache["at"],
    }
