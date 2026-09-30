"""Tell the owner that the Cloudflare Quick Tunnel URL changed (Telegram + toast).

Started detached by start-quick-tunnel.ps1 whenever data\\public-url.txt gets a new
value. A quick tunnel gets a new random hostname on every restart/reboot, and LINE only
delivers to the webhook URL it has on record, so the owner has to know the new URL
unless start-quick-tunnel.ps1 already re-registered it with the LINE API.

Delivery goes through local-tools\\notifier.py, so its switches in data\\notify.json
(enabled / telegram / toast) and its owner-only Telegram target apply unchanged. The
URL is not a secret; no token or key is read or printed here.

    python notify_tunnel_url.py --url https://x.trycloudflare.com --line not_configured [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

INSTALL_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = INSTALL_ROOT / "logs" / "internet-url-notify.log"
sys.path.insert(0, str(INSTALL_ROOT / "local-tools"))

import notifier  # noqa: E402  (local-tools\notifier.py)

LINE_STATES = ("not_configured", "registered", "unchanged", "failed")
HEADING = "URL ของ tunnel เปลี่ยน"


def build_text(url: str, line_state: str) -> str:
    base = url.strip().rstrip("/")
    webhook = base + "/webhooks/line"
    text = f"URL ของ tunnel เปลี่ยนเป็น {base}"
    if line_state in ("registered", "unchanged"):
        return text + f" - ระบบอัปเดต Webhook ใน LINE ให้อัตโนมัติแล้ว ({webhook})"
    if line_state == "failed":
        return text + f" - อัปเดต Webhook ใน LINE อัตโนมัติไม่สำเร็จ กรุณาอัปเดต Webhook ใน LINE Developers เป็น {webhook}"
    return text + f" - อัปเดต Webhook ใน LINE Developers เป็น {webhook}"


def send(text: str, dry_run: bool) -> dict:
    """Same gating as notifier.notify() (enabled + per-channel switches), custom text.

    notifier.notify() formats "<label><status>: <title>" and truncates the title at 120
    characters, which would cut the URL; so the notifier's own senders are used directly.
    """
    config = notifier.load_config()
    if not config.get("enabled"):
        return {"status": "skipped", "reason": "disabled", "channels": {}}
    active = [c for c in notifier.CHANNELS if config.get(c)]
    if not active:
        return {"status": "skipped", "reason": "no_channel", "channels": {}}
    outcomes: dict[str, dict] = {}
    with notifier._SEND_LOCK:
        if "toast" in active:
            body = text.split("เปลี่ยนเป็น ", 1)[-1]
            outcomes["toast"] = notifier._send_toast(f"Qwen Suite — {HEADING}", body, dry_run)
        if "telegram" in active:
            outcomes["telegram"] = notifier._send_telegram(text, "", dry_run)
    successes = sum(1 for outcome in outcomes.values() if outcome.get("ok"))
    if successes == len(outcomes):
        overall = "dry_run" if dry_run else "sent"
    else:
        overall = "partial" if successes else "failed"
    notifier._log({
        "task_id": "internet-tunnel-url",
        "status": "url_changed",
        "result": overall,
        "dry_run": dry_run,
        "title": HEADING,
        "channels": {name: {k: v for k, v in o.items() if k != "ok"} for name, o in outcomes.items()},
    })
    return {"status": overall, "channels": {name: o.get("result") for name, o in outcomes.items()}}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # Thai text must not crash on a cp874/cp437 console or redirected stdout
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--line", default="not_configured", choices=LINE_STATES)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    url = args.url.strip().rstrip("/")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9._~/-]*)?", url):
        print("ERROR: --url must be a plain https:// URL", file=sys.stderr)
        return 2
    text = build_text(url, args.line)
    try:
        result = send(text, args.dry_run)
    except Exception as exc:  # e.g. a notifier refactor removed a helper: fall back to notify()
        fallback = notifier.notify(f"{HEADING} {url}", "completed", None, dry_run=args.dry_run, label="")
        result = {"status": fallback.get("status"), "channels": fallback.get("channels"),
                  "fallback": type(exc).__name__}
    record = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "url": url,
              "line": args.line, "dry_run": args.dry_run, **result}
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
    print(json.dumps({**record, "text": text}, ensure_ascii=False))
    return 0 if result.get("status") in ("sent", "dry_run", "partial", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
