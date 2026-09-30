"""Example lifecycle hook: append every gateway call to a JSONL log, allow it.

Use as a Post hook (recommended) or a Pre hook. It reads the gateway payload from stdin -
{"source","action","arguments"[, "result"]} - writes one line to
E:\\localllm\\logs\\gateway-hook.jsonl, and prints {"decision":"allow"} so that, if wired as a
Pre hook, it never blocks. Copy this file and change the logic to build your own rule; a Pre
hook that prints {"decision":"block","reason":"<Thai reason>"} vetoes the action.

The owner authors these files; the gateway runs them fail-open with a 5 s timeout (Pre).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_LOG = Path(r"E:\localllm\logs\gateway-hook.jsonl")


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        payload = {"_unparsed": raw[:500]}

    row = {
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "source": payload.get("source", ""),
        "action": payload.get("action", ""),
        # arguments are logged shallowly; do not expand anything that could hold a secret
        "arg_keys": sorted(payload.get("arguments", {}).keys()) if isinstance(payload.get("arguments"), dict) else [],
    }
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with _LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass  # never let a logging failure block the action

    print(json.dumps({"decision": "allow"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
