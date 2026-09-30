from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:
    import win32crypt
except ImportError:  # pragma: no cover - production runtime is Windows with pywin32
    win32crypt = None


ROOT = Path(os.environ.get("QWEN_INTERNET_GATEWAY_ROOT", r"E:\localllm\internet-gateway"))
DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"
CLIENT_ACCESS_PATH = DATA_DIR / "client-access.json"
DATABASE_PATH = DATA_DIR / "gateway.db"
PUBLIC_URL_PATH = DATA_DIR / "public-url.txt"

_LOCK = threading.RLock()
PAIRING_CODE_TTL_SECONDS = 30 * 60


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 4,
    "model": "qwen3.8:27b",
    "ollama_url": "http://127.0.0.1:11434",
    "api_key_sha256": "",
    "pairing_code_sha256": "",
    "pairing_code_expires_at": 0,
    "line_channel_secret_dpapi": "",
    "line_channel_access_token_dpapi": "",
    "line_allowed_source_ids": [],
    "public_base_url": "",
    "max_prompt_chars": 8000,
    "max_response_chars": 40000,
    "max_history_messages": 20,
    "max_active_jobs_per_source": 2,
    "max_jobs_per_hour_per_source": 20,
    "job_timeout_seconds": 1800,
    "job_retention_days": 7,
    "session_retention_days": 30,
    "web_search_enabled": True,
    "web_search_backend": "auto",
    "web_search_result_count": 3,
    "web_search_timeout_seconds": 15,
    "web_search_max_query_chars": 500,
    "web_search_max_context_chars": 12000,
    "num_ctx": 32768,
    # 0 = let llama-server decide, like every other caller of the model (see server.py
    # _runner_options: a different value reloads the model on each switch).
    "num_thread": 0,
    "num_predict": 1536,
    "keep_alive": "2h",
}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_pairing_code() -> str:
    # 18 random bytes = 144 bits. This value is intended to be copied into LINE.
    return secrets.token_urlsafe(18)


def _protect_secret(value: str) -> str:
    if not value:
        return ""
    if win32crypt is None:
        raise RuntimeError("Windows DPAPI (pywin32) is required to store LINE secrets")
    blob = win32crypt.CryptProtectData(
        value.encode("utf-8"),
        "Qwen Internet Gateway",
        None,
        None,
        None,
        0,
    )
    return base64.b64encode(blob).decode("ascii")


def _unprotect_secret(value: str) -> str:
    if not value:
        return ""
    if win32crypt is None:
        raise RuntimeError("Windows DPAPI (pywin32) is required to read LINE secrets")
    _, raw = win32crypt.CryptUnprotectData(
        base64.b64decode(value.encode("ascii")), None, None, None, 0
    )
    return raw.decode("utf-8")


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_config() -> dict[str, Any]:
    with _LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if CONFIG_PATH.exists():
            config = {**DEFAULT_CONFIG, **_read_json(CONFIG_PATH)}
        else:
            config = dict(DEFAULT_CONFIG)

        access = _read_json(CLIENT_ACCESS_PATH) if CLIENT_ACCESS_PATH.exists() else {}
        changed = False
        stored_version = int(config.get("version") or 0)
        if stored_version < 3:
            extend_defaults = {
                "max_response_chars": (22000, 40000),
                "max_history_messages": (12, 20),
                "job_timeout_seconds": (600, 1800),
                "web_search_result_count": (5, 3),
                "num_ctx": (8192, 32768),
                "num_predict": (768, 1536),
            }
            for key, (previous, extended) in extend_defaults.items():
                if config.get(key) == previous:
                    config[key] = extended
                    changed = True
        if stored_version < 4 and config.get("num_thread") == 12:
            # 12 was the old default; no other caller sends it, so it reloaded the model.
            config["num_thread"] = 0
            changed = True
        if stored_version < int(DEFAULT_CONFIG["version"]):
            config["version"] = DEFAULT_CONFIG["version"]
            changed = True
        # One-time migration from an early plaintext development config.
        legacy_secret = str(config.pop("line_channel_secret", "") or "")
        legacy_token = str(config.pop("line_channel_access_token", "") or "")
        if legacy_secret:
            config["line_channel_secret_dpapi"] = _protect_secret(legacy_secret)
            changed = True
        if legacy_token:
            config["line_channel_access_token_dpapi"] = _protect_secret(legacy_token)
            changed = True
        current_api_key = str(access.get("api_key") or "")
        if (
            not config.get("api_key_sha256")
            or not current_api_key
            or not hmac.compare_digest(_sha256(current_api_key), str(config["api_key_sha256"]))
        ):
            api_key = secrets.token_urlsafe(36)
            config["api_key_sha256"] = _sha256(api_key)
            access["api_key"] = api_key
            changed = True
        current_pairing_code = str(access.get("line_pairing_code") or "")
        pairing_expired = float(config.get("pairing_code_expires_at") or 0) <= time.time()
        pairing_mismatch = bool(current_pairing_code) and not hmac.compare_digest(
            _sha256(current_pairing_code), str(config.get("pairing_code_sha256") or "")
        )
        if (
            not config.get("pairing_code_sha256")
            or not current_pairing_code
            or pairing_expired
            or pairing_mismatch
        ):
            pairing_code = _new_pairing_code()
            config["pairing_code_sha256"] = _sha256(pairing_code)
            config["pairing_code_expires_at"] = time.time() + PAIRING_CODE_TTL_SECONDS
            access["line_pairing_code"] = pairing_code
            changed = True

        access.update(
            {
                "local_setup_url": "http://127.0.0.1:8780/local/setup",
                "note": "Keep these credentials private. Rotate them if this file is exposed.",
            }
        )
        if changed or not CONFIG_PATH.exists():
            _atomic_write_json(CONFIG_PATH, config)
        if changed or not CLIENT_ACCESS_PATH.exists():
            _atomic_write_json(CLIENT_ACCESS_PATH, access)
        return config


def load_config() -> dict[str, Any]:
    with _LOCK:
        ensure_config()
        config = {**DEFAULT_CONFIG, **_read_json(CONFIG_PATH)}
        config["line_channel_secret"] = _unprotect_secret(
            str(config.get("line_channel_secret_dpapi") or "")
        )
        config["line_channel_access_token"] = _unprotect_secret(
            str(config.get("line_channel_access_token_dpapi") or "")
        )
        return config


def load_client_access() -> dict[str, Any]:
    with _LOCK:
        ensure_config()
        return _read_json(CLIENT_ACCESS_PATH)


def update_config(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _LOCK:
        ensure_config()
        config = {**DEFAULT_CONFIG, **_read_json(CONFIG_PATH)}
        mutator(config)
        config.pop("line_channel_secret", None)
        config.pop("line_channel_access_token", None)
        _atomic_write_json(CONFIG_PATH, config)
        return load_config()


def verify_api_key(value: str) -> bool:
    # Authentication does not need LINE credentials. Reading only the persisted
    # hash avoids an expensive DPAPI decrypt for every invalid Internet request.
    with _LOCK:
        ensure_config()
        expected = str(_read_json(CONFIG_PATH).get("api_key_sha256", ""))
    return bool(value and expected and hmac.compare_digest(_sha256(value), expected))


def verify_pairing_code(value: str) -> bool:
    with _LOCK:
        ensure_config()
        expected = str(_read_json(CONFIG_PATH).get("pairing_code_sha256", ""))
    return bool(value and expected and hmac.compare_digest(_sha256(value.strip()), expected))


def rotate_api_key() -> str:
    with _LOCK:
        new_value = secrets.token_urlsafe(36)
        update_config(lambda config: config.update({"api_key_sha256": _sha256(new_value)}))
        access = load_client_access()
        access["api_key"] = new_value
        _atomic_write_json(CLIENT_ACCESS_PATH, access)
        return new_value


def rotate_pairing_code() -> str:
    with _LOCK:
        new_value = _new_pairing_code()
        update_config(
            lambda config: config.update(
                {
                    "pairing_code_sha256": _sha256(new_value),
                    "pairing_code_expires_at": time.time() + PAIRING_CODE_TTL_SECONDS,
                }
            )
        )
        access = load_client_access()
        access["line_pairing_code"] = new_value
        _atomic_write_json(CLIENT_ACCESS_PATH, access)
        return new_value


def allow_line_source(source_id: str) -> None:
    def add(config: dict[str, Any]) -> None:
        allowed = list(config.get("line_allowed_source_ids") or [])
        if source_id not in allowed:
            allowed.append(source_id)
        config["line_allowed_source_ids"] = allowed

    update_config(add)


def set_line_credentials(channel_secret: str, access_token: str) -> None:
    if len(channel_secret.strip()) < 16:
        raise ValueError("LINE channel secret looks too short")
    if len(access_token.strip()) < 32:
        raise ValueError("LINE channel access token looks too short")

    def set_values(config: dict[str, Any]) -> None:
        config["line_channel_secret_dpapi"] = _protect_secret(channel_secret.strip())
        config["line_channel_access_token_dpapi"] = _protect_secret(access_token.strip())

    update_config(set_values)


def clear_line_credentials() -> None:
    def clear(config: dict[str, Any]) -> None:
        config["line_channel_secret_dpapi"] = ""
        config["line_channel_access_token_dpapi"] = ""
        config["line_allowed_source_ids"] = []

    update_config(clear)


def set_public_base_url(url: str) -> None:
    normalized = url.strip().rstrip("/")
    update_config(lambda config: config.update({"public_base_url": normalized}))
    PUBLIC_URL_PATH.write_text(normalized + "\n", encoding="utf-8")


def safe_status() -> dict[str, Any]:
    config = load_config()
    return {
        "model": config["model"],
        "line_configured": bool(
            config.get("line_channel_secret") and config.get("line_channel_access_token")
        ),
        "line_allowed_sources": len(config.get("line_allowed_source_ids") or []),
        "pairing_code_expires_at": config.get("pairing_code_expires_at", 0),
        "public_base_url": config.get("public_base_url", ""),
        "max_prompt_chars": config["max_prompt_chars"],
        "max_active_jobs_per_source": config["max_active_jobs_per_source"],
        "web_search_enabled": bool(config.get("web_search_enabled")),
        "web_search_provider": "DDGS",
    }


ensure_config()
