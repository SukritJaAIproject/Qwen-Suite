"""Channel watchdog - added 2026-11-09 (Qwen Suite 1.6.2).

OpenClaw retries a dropped WhatsApp connection 12 times (about 6 minutes) and then gives up with a "terminal
disconnect": the account stays linked but stopped, and OpenClaw's own health monitor only logs "skipping restart,
terminal-disconnect" every 5 minutes. After a longer network outage someone had to restart the channel by hand.

This thread reads the new lines of OpenClaw's log once a minute. When a channel gives up, it waits until the
service's host resolves again (the internet is back), confirms with the gateway that the account is still linked
and stopped for a network reason, and then calls OpenClaw's documented non-destructive recovery
`gateway call channels.start` (keeps the pairing and credentials - no logout, no gateway restart).
Only a channel that OpenClaw itself stopped after a terminal disconnect ("blocked") is started - never one that is
still reconnecting by itself or was stopped by hand. Limits: at most 3 restarts per channel per hour, 5 minutes
apart; a logged-out or replaced session is never restarted (that needs a new QR login). Every action is written
to logs\\channel-watchdog.log and
data\\channel-watchdog.json (times, channel names and outcomes only - no message text, numbers or ids).
Off switch: environment variable QWEN_CHANNEL_WATCHDOG=0 for the local-tools server.
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import suite_health as HEALTH

STATUS_PATH = HEALTH.SUITE_ROOT / "data" / "channel-watchdog.json"
LOG_PATH = HEALTH.SUITE_ROOT / "logs" / "channel-watchdog.log"
HOSTS = {"whatsapp": "web.whatsapp.com", "telegram": "api.telegram.org"}
POLL_S = 60.0
MAX_PER_HOUR = 3
MIN_GAP_S = 300.0
VERIFY_AFTER_S = 45.0
RECHECK_S = 120.0  # after a restart that was not connected yet: look again this often (no new restart)
_RE_BRACKET = re.compile(r"\[(\w+):[^\]]*\][^\n]*terminal-disconnect")
_RE_EVENT = re.compile(r"auto-restart skipped, terminal disconnect|after \d+/\d+ attempts", re.I)


class ChannelWatchdog:
    def __init__(self, log_dir: Callable[[], Path] | None = None,
                 health: Callable[[], dict[str, Any]] | None = None,
                 start: Callable[[str], dict[str, Any]] | None = None,
                 resolves: Callable[[str], bool] | None = None,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self._log_dir = log_dir or HEALTH._openclaw_log_dir
        self._health = health or self._gateway_health
        self._start = start or self._channels_start
        self._resolves = resolves or self._dns_ok
        self._now, self._sleep = now, sleep
        self._file: Path | None = None
        self._offset = 0
        self.pending: dict[str, float] = {}  # channel -> when it gave up
        self.attempts: dict[str, list[float]] = {}
        self.events: list[dict[str, Any]] = []
        self._noted: dict[str, float] = {}  # (channel, kind) -> last time a repeating note was written
        self._unconfirmed: set[str] = set()  # restarted, but not connected yet when checked
        self._checked: dict[str, float] = {}  # channel -> last gateway check
        self._stop = threading.Event()

    # ------------------------------------------------------------------ helpers (replaced in tests)
    @staticmethod
    def _gateway_health() -> dict[str, Any]:
        value, _at, live = HEALTH._SLOW.get("gateway_health", HEALTH._gateway_health_now, 50.0,
                                            "openclaw gateway call health")
        if not live:
            raise TimeoutError("the gateway answered too slowly; only an older answer is available")
        return value.get("channels") or {}

    @staticmethod
    def _channels_start(channel: str) -> dict[str, Any]:
        cwd = str(HEALTH.OPENCLAW_RUNTIME) if HEALTH.OPENCLAW_RUNTIME.is_dir() else None
        code, out, err = HEALTH._run(HEALTH._openclaw_argv("gateway", "call", "channels.start", "--params",
                                                            json.dumps({"channel": channel}), "--json"), timeout=60, cwd=cwd)
        if code is None:
            raise TimeoutError("channels.start did not answer within 60 s")
        data = HEALTH._json_from_output(out)
        if code != 0 or not isinstance(data, dict):
            raise RuntimeError(f"channels.start exit {code}")
        return data

    @staticmethod
    def _dns_ok(host: str) -> bool:
        try:
            return bool(socket.getaddrinfo(host, 443))
        except OSError:
            return False

    # ------------------------------------------------------------------ log reading
    def _new_lines(self) -> list[str]:
        try:
            logs = sorted(self._log_dir().glob("openclaw-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        if not logs:
            return []
        path = logs[0]
        size = path.stat().st_size
        if self._file != path:
            first = self._file is None
            self._file = path
            # on start: the last 256 KB (an outage that is still going on shows up within 5 minutes anyway);
            # on a new day's file: from its beginning
            self._offset = max(0, size - 256 * 1024) if first else 0
        if size < self._offset:
            self._offset = 0
        if size == self._offset:
            return []
        with path.open("rb") as fh:
            fh.seek(self._offset)
            data = fh.read(size - self._offset)
        self._offset = size
        text = data.decode("utf-8", "replace")
        return text.splitlines()

    def scan(self) -> None:
        for line in self._new_lines():
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            meta = obj.get("_meta") or {}
            name = str(meta.get("name") or "")
            msg = obj.get("1") if isinstance(obj.get("1"), str) else (obj.get("2") if isinstance(obj.get("2"), str) else "")
            ts = HEALTH._iso_to_ts(str(meta.get("date") or "")) or self._now()
            m = _RE_BRACKET.search(msg or "")
            channel = m.group(1).lower() if m else None
            if not channel:
                sub = re.search(r"channels/(\w+)", name)
                if sub and _RE_EVENT.search(msg or ""):
                    channel = sub.group(1).lower()
            if channel:
                self.pending.setdefault(channel, ts)

    # ------------------------------------------------------------------ decisions
    def _record(self, channel: str, action: str, detail: str, event: bool = True) -> None:
        """event=False: only a line in the log file (not shown on the status page)."""
        ev = {"time": datetime.fromtimestamp(self._now()).strftime("%Y-%m-%d %H:%M:%S"), "channel": channel,
              "action": action, "detail": HEALTH._redact(detail, 160)}
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(f"{ev['time']}  {channel}  {action}  {ev['detail']}\n")
            if not event:
                return
            self.events = (self.events + [ev])[-20:]
            STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATUS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps({"updated": ev["time"], "events": self.events}, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, STATUS_PATH)
        except OSError:
            pass

    def _drop(self, channel: str) -> None:
        self.pending.pop(channel, None)
        self._unconfirmed.discard(channel)

    def handle(self) -> None:
        for channel in list(self.pending):
            now = self._now()
            recent = [t for t in self.attempts.get(channel, []) if now - t < 3600]
            self.attempts[channel] = recent
            host = HOSTS.get(channel)
            if host and not self._resolves(host):
                continue  # still offline; try again next minute
            waiting = bool(recent and now - recent[-1] < MIN_GAP_S) or len(recent) >= MAX_PER_HOUR
            if waiting and (channel not in self._unconfirmed or now - self._checked.get(channel, 0) < RECHECK_S):
                continue
            self._checked[channel] = now
            try:
                state = (self._health() or {}).get(channel) or {}
            except Exception as exc:
                if now - self._noted.get(channel + ":check", 0) > 1800:  # at most one note per 30 minutes
                    self._noted[channel + ":check"] = now
                    self._record(channel, "check-failed", f"{type(exc).__name__}: {exc}")
                continue
            if state.get("running") and state.get("connected"):
                if channel in self._unconfirmed:  # the restart worked, it only took longer than the first check
                    self._record(channel, "restarted", "connected on a later check after channels.start")
                self._drop(channel)  # recovered by itself, by hand or after a restart
                continue
            if state.get("enabled") is False or state.get("configured") is False:
                self._drop(channel)
                continue
            if HEALTH.channel_needs_login(state):
                self._record(channel, "needs-login", f"not restarted: {state.get('statusState')} / {state.get('lastError')}")
                self._drop(channel)
                continue
            if state.get("running") or state.get("restartPending") or (state.get("reconnectAttempts") or 0) > 0:
                continue  # OpenClaw is still reconnecting by itself
            if not (state.get("terminal") or state.get("lifecycle") == "blocked"):
                # stopped for another reason (for example stopped by hand) - that is not ours to undo
                self._record(channel, "skipped", f"stopped but not a terminal disconnect: {state.get('lifecycle')}/"
                             f"{state.get('healthState')}", event=False)
                self._drop(channel)
                continue
            if waiting:
                continue
            self.attempts[channel].append(now)
            try:
                result = self._start(channel)
            except Exception as exc:
                self._record(channel, "restart-failed", f"{type(exc).__name__}: {exc}")
                continue
            outcome = (result.get("outcome") or {}).get("status")
            self._sleep(VERIFY_AFTER_S)
            try:
                after = (self._health() or {}).get(channel) or {}
            except Exception:
                after = {}
            ok = bool(after.get("running") and after.get("connected"))
            self._checked[channel] = self._now()
            self._record(channel, "restarted" if ok else "restart-not-connected",
                         f"channels.start outcome={outcome}; connected={ok}; was {state.get('lifecycle')}/{state.get('healthState')}")
            if ok:
                self._drop(channel)
            else:
                self._unconfirmed.add(channel)

    def run_once(self) -> None:
        self.scan()
        if self.pending:
            self.handle()

    def loop(self, first_delay: float = 120.0) -> None:
        if self._stop.wait(first_delay):
            return
        self._record("-", "started", f"checks OpenClaw's log every {POLL_S:.0f} s; at most {MAX_PER_HOUR} restarts "
                     f"per channel per hour, {MIN_GAP_S / 60:.0f} min apart", event=False)
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # never let the thread die
                self._record("-", "error", f"{type(exc).__name__}: {exc}")
            self._stop.wait(POLL_S)

    def stop(self) -> None:
        self._stop.set()


_WATCHDOG: ChannelWatchdog | None = None


def start_background(first_delay: float = 120.0) -> ChannelWatchdog | None:
    """Called once by server.py at start. Off with QWEN_CHANNEL_WATCHDOG=0."""
    global _WATCHDOG
    if not HEALTH.channel_watchdog_enabled() or _WATCHDOG is not None:
        return _WATCHDOG
    _WATCHDOG = ChannelWatchdog()
    threading.Thread(target=_WATCHDOG.loop, args=(first_delay,), name="channel-watchdog", daemon=True).start()
    return _WATCHDOG


def last_events(limit: int = 5) -> list[dict[str, Any]]:
    """For Suite Health: the latest actions from the status file (the thread may be in another process)."""
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return list(data.get("events") or [])[-limit:]
    except (OSError, ValueError):
        return []
