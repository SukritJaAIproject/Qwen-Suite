"""Account help for Open WebUI on this PC (added 9 Nov 2026).

Forgot password
    The owner asks for a link on http://127.0.0.1:8765/account/forgot. When the account is an admin
    account with a recovery address (set up with `Setup-RecoveryEmail.cmd`), a single-use link that
    is valid for 15 minutes is e-mailed from the owner's Gmail (SMTP with an App Password). The link
    points at 127.0.0.1, so it opens only on this PC. Setting the new password writes a bcrypt hash
    into Open WebUI's `auth` table for that one account, the same way Open WebUI does
    (open_webui/utils/auth.py get_password_hash; verify_password accepts it whatever
    PASSWORD_HASH_ALGORITHM says). Other accounts are not e-mailed: the owner resets them in
    Admin Panel > Users.

New sign-ups
    Open WebUI creates self-registered accounts as "pending". A watcher looks for pending accounts
    once a minute and tells the owner (toast + Telegram, through notifier.notify_owner) once per
    account, so the owner can approve or remove it in Admin Panel > Users.

Secrets and privacy
    The Gmail App Password is stored with Windows DPAPI for the current Windows user
    (data\\secrets\\recovery-smtp.bin) and is only ever sent to the SMTP server. Reset tokens are
    kept only as SHA-256 hashes. Passwords and tokens are never logged or returned.
"""
from __future__ import annotations

import ctypes
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import socket
import sqlite3
import ssl
import sys
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Any, Callable

INSTALL_ROOT = Path(__file__).resolve().parent.parent
DATA = INSTALL_ROOT / "data"
CONFIG_PATH = DATA / "account-recovery.json"
STATE_PATH = DATA / "account-recovery-state.json"
SECRET_PATH = DATA / "secrets" / "recovery-smtp.bin"
LOG_PATH = DATA / "account-recovery.log"
WEBUI_DB = INSTALL_ROOT / "webui-data" / "webui.db"

# The token rides in the fragment: browsers never send it, so it is in no request line or access log.
RESET_URL = "http://127.0.0.1:8765/account/reset#token={token}"
SIGNIN_URL = "http://127.0.0.1:3000/auth"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORTS = (587, 465)          # STARTTLS first, then implicit TLS
SMTP_TIMEOUT_S = 25
TOKEN_TTL_S = 15 * 60
MAX_PER_ACCOUNT_PER_HOUR = 3
MAX_SENDS_PER_HOUR = 10
MIN_GAP_S = 60
MAX_BAD_TOKENS_PER_10_MIN = 20
MIN_PASSWORD_CHARS = 8
MAX_PASSWORD_BYTES = 72          # bcrypt limit; Open WebUI's verify_password also cuts at 72 bytes
PENDING_POLL_S = 60
DPAPI_ENTROPY = b"qwen-suite/account-recovery/smtp"
MAX_LOG_BYTES = 256 * 1024
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}$")

_LOCK = threading.RLock()
_BAD_TOKENS: list[float] = []

# replaced in tests
_now: Callable[[], float] = time.time
_smtp_factory: Callable[..., Any] | None = None
notify_owner: Callable[..., Any] | None = None


# --------------------------------------------------------------------------- small helpers

def _log(event: dict[str, Any]) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
        line = json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **event}, ensure_ascii=False)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def mask_email(address: str) -> str:
    local, _, domain = str(address or "").partition("@")
    if not domain:
        return "***"
    keep = local[:2] if len(local) > 3 else local[:1]
    return f"{keep}…@{domain}"


def valid_email(address: str) -> bool:
    return bool(EMAIL_RE.match(str(address or "").strip()))


def _hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _notify(heading: str, text: str) -> None:
    sender = notify_owner
    if sender is None:
        try:
            import notifier  # local-tools module; imported late so the tests can run without it
            sender = notifier.notify_owner
        except Exception:
            return
    try:
        threading.Thread(target=sender, args=(heading, text), kwargs={"tag": "account"},
                         name="qwen-account-notify", daemon=True).start()
    except Exception:
        pass


# --------------------------------------------------------------------------- DPAPI (Windows)

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, Any]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    source, keep1 = _blob(data)
    entropy, keep2 = _blob(DPAPI_ENTROPY)
    out = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "Qwen Suite recovery mail", ctypes.byref(entropy),
                                                 None, None, 0x1, ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def dpapi_unprotect(blob: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI is only available on Windows")
    source, keep1 = _blob(blob)
    entropy, keep2 = _blob(DPAPI_ENTROPY)
    out = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(entropy),
                                                   None, None, 0x1, ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


# replaced in tests (the container has no DPAPI)
_protect: Callable[[bytes], bytes] = dpapi_protect
_unprotect: Callable[[bytes], bytes] = dpapi_unprotect


# --------------------------------------------------------------------------- configuration

def load_config() -> dict[str, Any]:
    """{"enabled", "sender", "recovery": {login email: recovery address}}; bad entries are dropped."""
    raw = _read_json(CONFIG_PATH, {})
    sender = str(raw.get("sender") or "").strip()
    recovery: dict[str, str] = {}
    for login, target in (raw.get("recovery") or {}).items() if isinstance(raw.get("recovery"), dict) else []:
        if valid_email(str(login)) or str(login).strip().lower() == "admin":
            if valid_email(str(target)):
                recovery[str(login).strip().lower()] = str(target).strip()
    return {
        "enabled": raw.get("enabled", True) is not False,
        "sender": sender if valid_email(sender) else "",
        "recovery": recovery,
    }


def configured(config: dict[str, Any] | None = None) -> bool:
    config = config or load_config()
    return bool(config["enabled"] and config["sender"] and config["recovery"] and SECRET_PATH.is_file())


def _app_password() -> str:
    return _unprotect(SECRET_PATH.read_bytes()).decode("utf-8")


# --------------------------------------------------------------------------- Open WebUI database

def _db(readonly: bool = True) -> sqlite3.Connection:
    # A plain connection (a "mode=ro" URI cannot open a WAL database whose -shm file is missing);
    # query_only stops the read paths from ever writing.
    if not WEBUI_DB.is_file():
        raise sqlite3.OperationalError("webui.db not found")
    con = sqlite3.connect(str(WEBUI_DB), timeout=30)
    con.execute("PRAGMA busy_timeout=15000")
    if readonly:
        con.execute("PRAGMA query_only=ON")
    return con


def find_account(email: str) -> dict[str, Any] | None:
    con = _db()
    try:
        row = con.execute(
            """SELECT u.id, u.email, u.role, COALESCE(a.active, 0)
               FROM user u JOIN auth a ON a.id = u.id
               WHERE lower(u.email) = lower(?) LIMIT 1""",
            (str(email).strip(),),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    return {"id": str(row[0]), "email": str(row[1]), "role": str(row[2]), "active": bool(row[3])}


def admin_emails() -> list[str]:
    con = _db()
    try:
        rows = con.execute("SELECT email FROM user WHERE role='admin' ORDER BY created_at").fetchall()
    finally:
        con.close()
    return [str(r[0]) for r in rows]


def pending_accounts() -> list[dict[str, Any]]:
    con = _db()
    try:
        rows = con.execute(
            "SELECT id, name, email, created_at FROM user WHERE role='pending' ORDER BY created_at"
        ).fetchall()
    finally:
        con.close()
    return [{"id": str(r[0]), "name": str(r[1] or ""), "email": str(r[2] or ""), "created_at": r[3]} for r in rows]


def check_password_rules(password: str) -> str:
    """Return '' when the password is acceptable, otherwise the reason (Thai)."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_CHARS:
        return f"รหัสผ่านต้องยาวอย่างน้อย {MIN_PASSWORD_CHARS} ตัวอักษร"
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return f"รหัสผ่านยาวเกิน {MAX_PASSWORD_BYTES} ไบต์ (ขีดจำกัดของ bcrypt)"
    if password != password.strip():
        return "รหัสผ่านขึ้นต้นหรือลงท้ายด้วยช่องว่างไม่ได้"
    if any(ord(ch) < 32 for ch in password):
        return "รหัสผ่านมีอักขระควบคุม"
    return ""


def _set_password(user_id: str, password: str) -> bool:
    import bcrypt  # Open WebUI's own dependency (webui-venv)

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    con = _db(readonly=False)
    try:
        with con:
            cur = con.execute("UPDATE auth SET password=? WHERE id=?", (hashed, user_id))
            if cur.rowcount != 1:
                return False
        stored = con.execute("SELECT password FROM auth WHERE id=?", (user_id,)).fetchone()
    finally:
        con.close()
    return bool(stored) and bcrypt.checkpw(password.encode("utf-8")[:MAX_PASSWORD_BYTES], str(stored[0]).encode("utf-8"))


# --------------------------------------------------------------------------- state (tokens, rate limits)

def _load_state() -> dict[str, Any]:
    state = _read_json(STATE_PATH, {})
    state.setdefault("tokens", [])
    state.setdefault("sends", [])
    state.setdefault("notified_pending", [])
    return state


def _prune(state: dict[str, Any], now: float) -> None:
    state["tokens"] = [t for t in state["tokens"] if isinstance(t, dict) and now - float(t.get("created", 0)) < 86_400][-50:]
    state["sends"] = [s for s in state["sends"] if isinstance(s, dict) and now - float(s.get("t", 0)) < 3600][-100:]
    state["notified_pending"] = [str(x) for x in state["notified_pending"]][-500:]


# --------------------------------------------------------------------------- e-mail

def build_reset_email(sender: str, to: str, login_email: str, token: str, now: float) -> EmailMessage:
    link = RESET_URL.format(token=token)
    computer = socket.gethostname() or "PC"
    when = time.strftime("%d/%m/%Y %H:%M", time.localtime(now))
    minutes = TOKEN_TTL_S // 60
    msg = EmailMessage()
    msg["Subject"] = "ลิงก์ตั้งรหัสผ่านใหม่ — Qwen Suite (Open WebUI)"
    msg["From"] = formataddr(("Qwen Suite", sender))
    msg["To"] = to
    msg["Date"] = formatdate(now, localtime=True)
    msg["Message-ID"] = make_msgid(domain="qwen-suite.local")
    msg.set_content(
        f"มีการขอตั้งรหัสผ่านใหม่ของ Open WebUI สำหรับบัญชี {login_email}\n"
        f"บนเครื่อง {computer} เมื่อ {when}\n\n"
        f"เปิดลิงก์นี้บนเครื่องนั้นภายใน {minutes} นาที (ใช้ได้ครั้งเดียว):\n{link}\n\n"
        "ลิงก์ชี้ไปที่ 127.0.0.1 จึงเปิดได้เฉพาะบนเครื่องนั้น\n"
        "ถ้าคุณไม่ได้ขอ ไม่ต้องทำอะไร รหัสผ่านเดิมยังใช้ได้ตามปกติ\n\n"
        "— Qwen Suite\n"
    )
    msg.add_alternative(
        "<!doctype html><html><body style=\"margin:0;padding:24px;background:#f4f5fb;font-family:'Segoe UI',Tahoma,sans-serif;color:#1f2330\">"
        "<div style=\"max-width:520px;margin:auto;background:#fff;border:1px solid #e3e6ef;border-radius:14px;padding:26px\">"
        "<div style=\"font-size:18px;font-weight:700;margin-bottom:6px\">ตั้งรหัสผ่านใหม่ — Open WebUI</div>"
        f"<div style=\"color:#555c6e;font-size:14px;margin-bottom:18px\">บัญชี <b>{_html(login_email)}</b> บนเครื่อง {_html(computer)} · {when}</div>"
        f"<a href=\"{_html(link)}\" style=\"display:inline-block;background:#7c3aed;color:#fff;text-decoration:none;padding:11px 20px;border-radius:10px;font-weight:600\">ตั้งรหัสผ่านใหม่</a>"
        f"<div style=\"color:#555c6e;font-size:13px;margin-top:18px\">ลิงก์ใช้ได้ครั้งเดียวภายใน {minutes} นาที และเปิดได้เฉพาะบนเครื่องนั้น (127.0.0.1)<br>"
        "ถ้าคุณไม่ได้ขอ ไม่ต้องทำอะไร รหัสผ่านเดิมยังใช้ได้ตามปกติ</div>"
        f"<div style=\"color:#8a90a0;font-size:12px;margin-top:14px;word-break:break-all\">{_html(link)}</div>"
        "</div></body></html>",
        subtype="html",
    )
    return msg


def build_test_email(sender: str, to: str, logins: list[str], now: float) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "ทดสอบอีเมลกู้คืนรหัสผ่าน — Qwen Suite"
    msg["From"] = formataddr(("Qwen Suite", sender))
    msg["To"] = to
    msg["Date"] = formatdate(now, localtime=True)
    msg["Message-ID"] = make_msgid(domain="qwen-suite.local")
    msg.set_content(
        "อีเมลนี้ส่งจากการตั้งค่า \"ลืมรหัสผ่าน\" ของ Qwen Suite\n"
        f"ต่อไปลิงก์ตั้งรหัสผ่านใหม่ของบัญชี {', '.join(logins)} จะส่งมาที่อีเมลนี้\n"
        "ไม่ต้องตอบกลับ\n\n— Qwen Suite\n"
    )
    return msg


def _html(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def send_email(msg: EmailMessage, sender: str, app_password: str) -> None:
    """Gmail SMTP: 587 with STARTTLS, then 465 with TLS when 587 cannot be reached. Raises RuntimeError (Thai)."""
    context = ssl.create_default_context()
    last: Exception | None = None
    for port in SMTP_PORTS:
        try:
            if _smtp_factory is not None:
                server = _smtp_factory(SMTP_HOST, port)
            elif port == 465:
                server = smtplib.SMTP_SSL(SMTP_HOST, port, timeout=SMTP_TIMEOUT_S, context=context)
            else:
                server = smtplib.SMTP(SMTP_HOST, port, timeout=SMTP_TIMEOUT_S)
            with server:
                if port != 465:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                server.login(sender, app_password)
                server.send_message(msg)
            return
        except smtplib.SMTPAuthenticationError as exc:
            raise RuntimeError("Gmail ไม่รับ App Password (ตรวจว่าเปิด 2-Step Verification แล้ว และใช้ App Password ที่สร้างใหม่)") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise RuntimeError("Gmail ไม่รับอีเมลปลายทาง") from exc
        except (OSError, smtplib.SMTPException) as exc:
            last = exc
            continue
    raise RuntimeError(f"ต่อ {SMTP_HOST} ไม่ได้ (เครือข่ายอาจปิดพอร์ต 587 และ 465): {type(last).__name__ if last else 'unknown'}")


# --------------------------------------------------------------------------- forgot / reset

GENERIC = ("ถ้าอีเมลนี้เป็นบัญชีเจ้าของที่ตั้งอีเมลกู้คืนไว้ ระบบจะส่งลิงก์ให้ภายใน 1-2 นาที "
           "ถ้าเป็นบัญชีทั่วไป ให้ผู้ดูแลตั้งรหัสใหม่ให้ใน Admin Panel > Users")


def request_reset(email: str) -> dict[str, Any]:
    email = str(email or "").strip()
    now = _now()
    if not email or len(email) > 254:
        return {"ok": False, "reason": "bad_email", "message": "กรอกอีเมลที่ใช้ล็อกอิน"}
    config = load_config()
    if not configured(config):
        _log({"event": "forgot", "result": "not_configured"})
        return {"ok": False, "reason": "not_configured",
                "message": "ยังไม่ได้ตั้งค่าอีเมลกู้คืน ให้เจ้าของเครื่องรัน Setup-RecoveryEmail.cmd ในโฟลเดอร์ local-tools ก่อน "
                           "หรือใช้สคริปต์รีเซ็ตรหัสในเครื่อง"}
    try:
        account = find_account(email)
    except sqlite3.Error as exc:
        _log({"event": "forgot", "result": "db_error", "error": type(exc).__name__})
        return {"ok": False, "reason": "db_error", "message": "อ่านฐานข้อมูล Open WebUI ไม่ได้ ลองใหม่อีกครั้ง"}
    target = ""
    if account and account["role"] == "admin" and account["active"]:
        target = config["recovery"].get(account["email"].lower(), "")
    if not target:
        _log({"event": "forgot", "result": "not_eligible"})
        return {"ok": True, "sent": False, "message": GENERIC}

    with _LOCK:
        state = _load_state()
        _prune(state, now)
        sends = state["sends"]
        mine = [s for s in sends if s.get("uid") == account["id"]]
        if mine and now - float(mine[-1]["t"]) < MIN_GAP_S:
            wait = int(MIN_GAP_S - (now - float(mine[-1]["t"]))) + 1
            return {"ok": False, "reason": "rate_limited", "message": f"เพิ่งส่งลิงก์ไป รออีก {wait} วินาทีแล้วค่อยขอใหม่"}
        if len(mine) >= MAX_PER_ACCOUNT_PER_HOUR or len(sends) >= MAX_SENDS_PER_HOUR:
            first = float((mine or sends)[0]["t"])
            wait_min = max(1, int((3600 - (now - first)) // 60) + 1)
            _log({"event": "forgot", "result": "rate_limited"})
            return {"ok": False, "reason": "rate_limited",
                    "message": f"ขอลิงก์ครบจำนวนต่อชั่วโมงแล้ว ลองใหม่ในอีกประมาณ {wait_min} นาที"}
        token = secrets.token_urlsafe(32)
        for item in state["tokens"]:
            if item.get("uid") == account["id"] and not item.get("used"):
                item["used"] = True
                item["superseded"] = True
        record = {"h": _hash_token(token), "uid": account["id"], "email": account["email"],
                  "created": now, "expires": now + TOKEN_TTL_S, "used": False}
        state["tokens"].append(record)
        state["sends"].append({"uid": account["id"], "t": now})
        _atomic_json(STATE_PATH, state)

    try:
        message = build_reset_email(config["sender"], target, account["email"], token, now)
        send_email(message, config["sender"], _app_password())
    except Exception as exc:
        with _LOCK:
            state = _load_state()
            for item in state["tokens"]:
                if item.get("h") == record["h"]:
                    item["used"] = True
                    item["send_failed"] = True
            # a send that failed does not count against the hourly limit (the owner may fix it and retry)
            state["sends"] = [s for s in state["sends"]
                              if not (s.get("uid") == account["id"] and float(s.get("t", 0)) == now)]
            _atomic_json(STATE_PATH, state)
        reason = str(exc) if isinstance(exc, RuntimeError) else f"ส่งอีเมลไม่สำเร็จ ({type(exc).__name__})"
        _log({"event": "forgot", "result": "send_failed", "account": account["email"], "error": reason[:200]})
        return {"ok": False, "reason": "send_failed", "message": reason}

    _log({"event": "forgot", "result": "sent", "account": account["email"], "to": mask_email(target)})
    _notify("ส่งลิงก์ตั้งรหัสผ่านแล้ว",
            f"ส่งลิงก์ตั้งรหัสผ่าน Open WebUI ของ {account['email']} ไปที่ {mask_email(target)} เมื่อ {_clock(now)} "
            f"(ใช้ได้ {TOKEN_TTL_S // 60} นาที)")
    return {"ok": True, "sent": True, "to": mask_email(target), "minutes": TOKEN_TTL_S // 60,
            "message": f"ส่งลิงก์ไปที่ {mask_email(target)} แล้ว เปิดลิงก์บนเครื่องนี้ภายใน {TOKEN_TTL_S // 60} นาที "
                       "(ถ้าไม่เห็น ให้ดูในโฟลเดอร์ Spam)"}


def _bad_token() -> dict[str, Any]:
    now = _now()
    with _LOCK:
        _BAD_TOKENS[:] = [t for t in _BAD_TOKENS if now - t < 600]
        _BAD_TOKENS.append(now)
    return {"ok": False, "reason": "invalid", "message": "ลิงก์ไม่ถูกต้อง หมดอายุ หรือใช้ไปแล้ว ขอลิงก์ใหม่ที่หน้า \"ลืมรหัสผ่าน\""}


def _lookup(token: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    state = _load_state()
    wanted = _hash_token(token)
    for item in state["tokens"]:
        if hmac.compare_digest(str(item.get("h", "")), wanted):
            return item, state
    return None, state


def token_info(token: str) -> dict[str, Any]:
    now = _now()
    with _LOCK:
        if len([t for t in _BAD_TOKENS if now - t < 600]) >= MAX_BAD_TOKENS_PER_10_MIN:
            return {"ok": False, "reason": "locked", "message": "มีการลองลิงก์ผิดหลายครั้ง รอ 10 นาทีแล้วลองใหม่"}
        if not re.fullmatch(r"[A-Za-z0-9_-]{30,80}", str(token or "")):
            return _bad_token()
        item, _ = _lookup(token)
    if not item or item.get("used") or now > float(item.get("expires", 0)):
        return _bad_token()
    return {"ok": True, "email": item["email"], "expires_in": int(float(item["expires"]) - now),
            "min_chars": MIN_PASSWORD_CHARS, "max_bytes": MAX_PASSWORD_BYTES}


def complete_reset(token: str, password: str) -> dict[str, Any]:
    info = token_info(token)
    if not info.get("ok"):
        return info
    problem = check_password_rules(password)
    if problem:
        return {"ok": False, "reason": "weak_password", "message": problem}
    now = _now()
    with _LOCK:
        item, state = _lookup(token)
        if not item or item.get("used") or now > float(item.get("expires", 0)):
            return _bad_token()
        item["used"] = True          # single use even if the write below fails
        item["used_at"] = now
        _atomic_json(STATE_PATH, state)
        try:
            ok = _set_password(item["uid"], password)
        except Exception as exc:
            _log({"event": "reset", "result": "error", "account": item["email"], "error": type(exc).__name__})
            return {"ok": False, "reason": "error", "message": "ตั้งรหัสผ่านไม่สำเร็จ ขอลิงก์ใหม่แล้วลองอีกครั้ง"}
        for other in state["tokens"]:
            if other.get("uid") == item["uid"] and not other.get("used"):
                other["used"] = True
                other["superseded"] = True
        _atomic_json(STATE_PATH, state)
    if not ok:
        _log({"event": "reset", "result": "verify_failed", "account": item["email"]})
        return {"ok": False, "reason": "error", "message": "ตั้งรหัสผ่านไม่สำเร็จ ขอลิงก์ใหม่แล้วลองอีกครั้ง"}
    _log({"event": "reset", "result": "changed", "account": item["email"]})
    _notify("เปลี่ยนรหัสผ่านแล้ว",
            f"รหัสผ่าน Open WebUI ของ {item['email']} ถูกเปลี่ยนผ่านลิงก์อีเมลเมื่อ {_clock(now)} "
            "ถ้าไม่ใช่คุณ ให้รีเซ็ตรหัสอีกครั้งทันที")
    return {"ok": True, "email": item["email"], "signin": SIGNIN_URL,
            "message": f"ตั้งรหัสผ่านใหม่ของ {item['email']} แล้ว ล็อกอินด้วยรหัสใหม่ได้เลย"}


# --------------------------------------------------------------------------- pending sign-ups

def check_pending_once() -> list[dict[str, Any]]:
    """Notify the owner about pending accounts not reported before. Returns the new ones."""
    try:
        pending = pending_accounts()
    except sqlite3.Error:
        return []
    with _LOCK:
        state = _load_state()
        seen = set(state["notified_pending"])
        fresh = [p for p in pending if p["id"] not in seen]
        if not fresh:
            return []
        state["notified_pending"].extend(p["id"] for p in fresh)
        _prune(state, _now())
        _atomic_json(STATE_PATH, state)
    for p in fresh:
        who = (p["name"] + " " if p["name"] else "") + f"<{p['email']}>"
        _log({"event": "signup_pending", "account": p["email"]})
        _notify("มีบัญชีใหม่รออนุมัติ",
                f"Open WebUI: {who} สมัครบัญชีใหม่ อนุมัติหรือลบได้ที่ Admin Panel > Users "
                "(บัญชีใหม่ใช้เครื่องมือในเครื่องไม่ได้)")
    return fresh


def status() -> dict[str, Any]:
    config = load_config()
    return {
        "configured": configured(config),
        "sender": mask_email(config["sender"]) if config["sender"] else "",
        "accounts": {login: mask_email(addr) for login, addr in config["recovery"].items()},
        "token_minutes": TOKEN_TTL_S // 60,
    }


_WATCHER: threading.Thread | None = None


def start_background() -> threading.Thread | None:
    """Pending sign-up watcher; QWEN_ACCOUNT_WATCH=0 turns it off."""
    global _WATCHER
    if os.environ.get("QWEN_ACCOUNT_WATCH", "1").strip() == "0":
        return None
    if _WATCHER is not None and _WATCHER.is_alive():
        return _WATCHER

    def loop() -> None:
        time.sleep(30)
        while True:
            try:
                check_pending_once()
            except Exception as exc:  # never let the watcher die
                _log({"event": "watcher_error", "error": type(exc).__name__})
            time.sleep(PENDING_POLL_S)

    _WATCHER = threading.Thread(target=loop, name="qwen-account-watch", daemon=True)
    _WATCHER.start()
    return _WATCHER


# --------------------------------------------------------------------------- setup (run by the owner)

def _clipboard_text() -> str:
    """Unicode text on the Windows clipboard ('' when there is none)."""
    if os.name != "nt":
        return ""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def read_secret(prompt: str, getch: Callable[[], str] | None = None, putch: Callable[[str], None] | None = None,
                clipboard: Callable[[], str] = _clipboard_text) -> str:
    """Hidden input that shows one * per character, so a paste is visible. Ctrl+V reads the clipboard
    itself (some consoles hand Ctrl+V to the program instead of pasting); right-click paste works too.
    Not on a Windows console (tests, other systems): getpass."""
    if getch is None:
        if os.name != "nt":
            return getpass.getpass(prompt)
        import msvcrt
        getch, putch = msvcrt.getwch, msvcrt.putwch
    for ch in prompt:
        putch(ch)
    chars: list[str] = []
    while True:
        c = getch()
        if c in ("\r", "\n"):
            break
        if c == "\x03":
            raise KeyboardInterrupt
        if c in ("\x00", "\xe0"):      # arrows and function keys send a second code
            getch()
            continue
        if c == "\b":
            if chars:
                chars.pop()
                for x in "\b \b":
                    putch(x)
            continue
        if c == "\x16":                # Ctrl+V reached the program: paste from the clipboard
            for p in clipboard():
                if p not in "\r\n":
                    chars.append(p)
                    putch("*")
            continue
        if ord(c) < 32:
            continue
        chars.append(c)
        putch("*")
    putch("\r")
    putch("\n")
    return "".join(chars)


APP_PASSWORD_RE = re.compile(r"^[A-Za-z]{16}$")


def setup(ask: Callable[[str], str] = input, ask_secret: Callable[[str], str] = read_secret,
          say: Callable[[str], None] = print) -> int:
    say("ตั้งค่าอีเมลสำหรับปุ่ม \"ลืมรหัสผ่าน\" ของ Open WebUI (Qwen Suite)")
    say("")
    say("ต้องมี: Gmail ที่เปิด 2-Step Verification แล้ว และ App Password 16 ตัวอักษร")
    say("สร้าง App Password ได้ที่ https://myaccount.google.com/apppasswords")
    say("  (ตั้งชื่อแอป เช่น Qwen Suite แล้วกด Create — Google จะแสดงตัวอักษร 16 ตัว เช่น abcd efgh ijkl mnop)")
    say("App Password จะถูกเก็บแบบเข้ารหัส (Windows DPAPI) ใช้ได้เฉพาะผู้ใช้ Windows คนนี้บนเครื่องนี้")
    say("")
    sender = ask("Gmail ที่ใช้ส่ง (เช่น name@gmail.com): ").strip()
    if not valid_email(sender):
        say("อีเมลไม่ถูกต้อง ไม่ได้บันทึกอะไร")
        return 1
    say("วาง App Password ด้วยคลิกขวาหรือ Ctrl+V (หรือพิมพ์เอง) แล้วกด Enter")
    say("บนจอจะเห็นเป็น * แทนตัวอักษร ช่องว่างไม่เป็นไร")
    app_password = ""
    for attempt in range(3):
        raw = ask_secret("App Password: ")
        candidate = re.sub(r"\s+", "", raw)
        if APP_PASSWORD_RE.match(candidate):
            app_password = candidate
            say("ได้รับ App Password 16 ตัวอักษรแล้ว")
            break
        if not candidate:
            say("ไม่ได้รับตัวอักษรเลย ถ้าวางแล้วไม่เห็น * ให้ลองคลิกขวาในหน้าต่างนี้ หรือพิมพ์เอง")
        else:
            say(f"ได้รับ {len(candidate)} ตัวอักษร (ไม่นับช่องว่าง) แต่ App Password ต้องเป็นตัวอักษรภาษาอังกฤษ 16 ตัว "
                "ไม่ใช่รหัสผ่าน Gmail ปกติ")
        if attempt < 2:
            say("ลองอีกครั้ง")
    if not app_password:
        say("ไม่ได้บันทึกอะไร")
        return 1
    target = ask(f"อีเมลที่จะรับลิงก์ตั้งรหัสใหม่ [กด Enter = {sender}]: ").strip() or sender
    if not valid_email(target):
        say("อีเมลไม่ถูกต้อง ไม่ได้บันทึกอะไร")
        return 1
    try:
        logins = admin_emails()
    except sqlite3.Error:
        logins = []
    if not logins:
        say("ไม่พบบัญชีเจ้าของ (admin) ใน Open WebUI ไม่ได้บันทึกอะไร")
        return 1
    say(f"บัญชีที่ใช้ปุ่มนี้ได้: {', '.join(logins)}")
    say(f"กำลังส่งอีเมลทดสอบไปที่ {target} ...")
    try:
        send_email(build_test_email(sender, target, logins, _now()), sender, app_password)
    except RuntimeError as exc:
        say(f"ส่งไม่สำเร็จ: {exc}")
        say("ไม่ได้บันทึกอะไร")
        return 1
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRET_PATH.write_bytes(_protect(app_password.encode("utf-8")))
    _atomic_json(CONFIG_PATH, {
        "enabled": True,
        "sender": sender,
        "recovery": {login.lower(): target for login in logins},
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    _log({"event": "setup", "result": "saved", "sender": mask_email(sender), "to": mask_email(target),
          "accounts": logins})
    say("")
    say(f"เสร็จแล้ว ส่งอีเมลทดสอบไปที่ {target} แล้ว (ดูในกล่องจดหมายหรือ Spam)")
    say("ต่อไปกด \"ลืมรหัสผ่าน?\" ที่หน้าล็อกอิน Open WebUI ได้เลย")
    return 0


if __name__ == "__main__":
    if "--setup" in sys.argv[1:]:
        try:
            sys.exit(setup())
        except KeyboardInterrupt:
            print("\nยกเลิกแล้ว ไม่ได้บันทึกอะไร")
            sys.exit(1)
    if "--status" in sys.argv[1:]:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        sys.exit(0)
    print("ใช้: account_recovery.py --setup | --status")
    sys.exit(2)
