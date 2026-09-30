from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from gateway_config import (
    clear_line_credentials,
    ensure_config,
    load_config,
    rotate_api_key,
    rotate_pairing_code,
    safe_status,
    set_line_credentials,
    set_public_base_url,
)


def _line_request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.line.me{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {"status": response.status}
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LINE API returned HTTP {error.code}: {body[:500]}") from error


def register_line_webhook(base_url: str, test: bool = True) -> dict:
    parsed = urllib.parse.urlparse(base_url.strip())
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Public base URL must be a clean https:// URL")
    normalized = f"https://{parsed.netloc}{parsed.path.rstrip('/')}"
    webhook_url = normalized + "/webhooks/line"
    if len(webhook_url) > 500:
        raise ValueError("LINE webhook URL exceeds 500 characters")
    config = load_config()
    token = str(config.get("line_channel_access_token") or "")
    if not token:
        raise RuntimeError("LINE credentials are not configured")
    _line_request(
        "PUT",
        "/v2/bot/channel/webhook/endpoint",
        token,
        {"endpoint": webhook_url},
    )
    test_result = None
    if test:
        test_result = _line_request("POST", "/v2/bot/channel/webhook/test", token)
    set_public_base_url(normalized)
    return {"webhook_url": webhook_url, "test": test_result}


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure Qwen Internet Prompt Gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    subparsers.add_parser("status")
    subparsers.add_parser("line-from-env")
    subparsers.add_parser("disable-line")
    subparsers.add_parser("rotate-api-key")
    subparsers.add_parser("rotate-pairing-code")
    register = subparsers.add_parser("register-line")
    register.add_argument("base_url")
    register.add_argument("--no-test", action="store_true")
    public_url = subparsers.add_parser("set-public-url")
    public_url.add_argument("base_url")
    args = parser.parse_args()

    if args.command == "init":
        ensure_config()
        print(json.dumps({"initialized": True, **safe_status()}, ensure_ascii=False, indent=2))
    elif args.command == "status":
        print(json.dumps(safe_status(), ensure_ascii=False, indent=2))
    elif args.command == "line-from-env":
        secret = os.environ.get("QWEN_LINE_CHANNEL_SECRET", "")
        token = os.environ.get("QWEN_LINE_CHANNEL_ACCESS_TOKEN", "")
        if not secret or not token:
            raise RuntimeError("Required temporary environment variables are missing")
        set_line_credentials(secret, token)
        print(json.dumps({"line_configured": True}, ensure_ascii=False))
    elif args.command == "disable-line":
        clear_line_credentials()
        print(json.dumps({"line_configured": False}, ensure_ascii=False))
    elif args.command == "rotate-api-key":
        rotate_api_key()
        print("API key rotated. Read the new value from the local setup page.")
    elif args.command == "rotate-pairing-code":
        rotate_pairing_code()
        print("LINE pairing code rotated. Read it from the local setup page.")
    elif args.command == "register-line":
        result = register_line_webhook(args.base_url, test=not args.no_test)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "set-public-url":
        parsed = urllib.parse.urlparse(args.base_url.strip())
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Public base URL must use HTTPS")
        set_public_base_url(args.base_url)
        print(json.dumps({"public_base_url": args.base_url.rstrip("/")}, ensure_ascii=False))
    else:  # pragma: no cover
        parser.error("Unknown command")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
