"""generate_image: optional local Qwen-Image 2.1 with Runway cloud fallback.

Why this module has its own credential: the only Runway logins on this machine are Open WebUI's
MCP OAuth sessions (webui.db oauth_session, encrypted with Open WebUI's secret). Those tokens stay
inside Open WebUI; this module never reads them. Instead it uses a Runway *developer API* key
that the owner creates himself at https://dev.runwayml.com (API credits are bought there and are
separate from the Runway app plan's credits).

The key is read at call time (no restart needed) from KEY_FILE,
<install root>\\data\\credentials\\runway-api-key.txt, which the owner writes himself (see
_not_configured). A folder named "credentials" is refused by the model's file tools
(server.py BLOCKED_PARTS), and unlike a user environment variable (setx) the key is not
inherited by every command the chat runs, where `set` would print it. RUNWAYML_API_SECRET in
local-tools' own process environment is honoured as a fallback, but not recommended.
The key only ever goes into the Authorization header of requests to API_BASE; it is never
logged, returned, written anywhere or sent to the image CDN, and any echo of it in an error text
is redacted. Without a key the tool answers status=not_configured and changes nothing.

Every Runway generation spends API credits (gen4_image: 5 credits at 720p, 8 at 1080p) and sends
the prompt (and the reference image, if any) to Runway's cloud. A task that is still running at
the timeout is not lost: call again with task_id=<id> to fetch it without spending credits again.
The picture goes to the project's outputs folder (never overwrites, document_builder._reserve)
plus a copy in the OpenClaw workspace for Telegram (MEDIA:<telegram_attachment>).
"""
from __future__ import annotations

import base64
import io
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import document_builder as DB

API_BASE = "https://api.dev.runwayml.com"
API_VERSION = "2024-11-06"
MODEL = "gen4_image"
ENV_NAME = "RUNWAYML_API_SECRET"
KEY_FILE = Path(__file__).resolve().parent.parent / "data" / "credentials" / "runway-api-key.txt"
CREDITS = {"720p": 5, "1080p": 8}
RATIOS = {
    "16:9": {"720p": "1280:720", "1080p": "1920:1080"},
    "1:1": {"720p": "720:720", "1080p": "1080:1080"},
    "9:16": {"720p": "720:1280", "1080p": "1080:1920"},
    "4:3": {"720p": "960:720", "1080p": "1440:1080"},
    "3:4": {"720p": "720:960", "1080p": "1080:1440"},
}
_RATIO_ALIASES = {"landscape": "16:9", "แนวนอน": "16:9", "square": "1:1", "สี่เหลี่ยมจัตุรัส": "1:1",
                  "portrait": "9:16", "แนวตั้ง": "9:16", "vertical": "9:16", "story": "9:16", "reels": "9:16"}
MAX_PROMPT = 1000
POLL_SECONDS = 5          # Runway asks clients not to poll faster than every 5 s
TIMEOUT = 240
MAX_DOWNLOAD = 30 * 1024 * 1024
MAX_REFERENCE_SIDE = 2048
MAX_REFERENCE_BYTES = 3_500_000   # base64 grows it by 4/3; Runway takes data URIs up to ~5 MB
REFERENCE_TAG = "ref"
_TERMINAL_FAIL = {"FAILED", "CANCELLED"}
SUITE_ROOT = Path(__file__).resolve().parent.parent
LOCAL_PACK_ROOT = SUITE_ROOT / "optional" / "qwen-image-2.1"
LOCAL_PYTHON = LOCAL_PACK_ROOT / "venv" / "Scripts" / "python.exe"
LOCAL_WORKER = LOCAL_PACK_ROOT / "worker.py"
LOCAL_MODEL = SUITE_ROOT / "model-cache" / "Qwen-Image-2.1"
# Written by Install-QwenImage.ps1 only after `worker.py --check` passed (CUDA GPU found). A model
# folder alone is not proof: an interrupted download leaves model_index.json behind.
LOCAL_INSTALL_RECORD = LOCAL_PACK_ROOT / "install.json"
# Local worker errors that mean the pack cannot run on this machine right now; only these let
# provider=auto fall back to Runway.
_LOCAL_UNAVAILABLE = re.compile(r"CUDA|out of memory|OutOfMemory|no GPU|nvidia", re.I)


def _shorten(text: str, limit: int) -> str:
    """Keep the start and the end: a worker's real error is its last stderr line."""
    return text if len(text) <= limit else text[:60] + " ... " + text[-(limit - 65):]


def _api_key() -> tuple[str, str]:
    try:
        if KEY_FILE.is_file() and KEY_FILE.stat().st_size <= 4096:
            for line in KEY_FILE.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    return line.strip(), "key file"
    except OSError:
        pass
    key = os.environ.get(ENV_NAME, "").strip()
    if key:
        return key, "process environment"
    return "", ""


def _not_configured() -> dict[str, Any]:
    return {
        "status": "not_configured",
        "message": ("ยังไม่ได้ตั้ง Runway API key จึงสร้างรูปไม่ได้ (ไม่มีการใช้เครดิต). เจ้าของเครื่องสร้าง key เองที่ "
                    "https://dev.runwayml.com (เครดิต API ซื้อแยกจากแพ็กเกจแอป Runway) แล้วบันทึกลงไฟล์ "
                    f"{KEY_FILE} เองที่เครื่อง — ห้ามส่ง key ในแชทหรือ Telegram และห้ามให้โมเดลเขียนไฟล์นี้. "
                    "ในหน้าแชท Open WebUI ใช้เครื่องมือ Runway (MCP) หลังล็อกอินแทนได้"),
    }


def _redact(text: str, key: str) -> str:
    text = str(text or "")
    if key:
        text = text.replace(key, "<redacted>")
    return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}", r"\1<redacted>", text)[:600]


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "X-Runway-Version": API_VERSION, "Content-Type": "application/json"}


def _error_text(response, key: str) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            parts = [str(body.get("error") or body.get("message") or "")]
            issues = body.get("issues")
            if isinstance(issues, list):
                parts += [str(i.get("message") if isinstance(i, dict) else i) for i in issues[:3]]
            text = "; ".join(p for p in parts if p) or str(body)
        else:
            text = str(body)
    except ValueError:
        text = getattr(response, "text", "") or ""
    return _redact(text, key)


def _raise_for(response, key: str, what: str) -> None:
    code = response.status_code
    if code < 400:
        return
    detail = _error_text(response, key)
    if code == 401:
        raise RuntimeError(f"Runway ปฏิเสธ API key (401) ตอน{what}: key ผิดหรือถูกยกเลิก — เจ้าของเครื่องเปลี่ยน key ในไฟล์ {KEY_FILE}")
    if code == 429:
        raise RuntimeError(f"Runway จำกัดจำนวนคำขอ (429) ตอน{what}: ลองใหม่อีกสักครู่. {detail}")
    if "credit" in detail.lower():
        raise RuntimeError(f"เครดิต Runway API ไม่พอ ({code}) ตอน{what}: {detail}")
    raise RuntimeError(f"Runway ตอบ {code} ตอน{what}: {detail}")


def _ratio(ratio: Any, quality: str) -> str:
    text = str(ratio or "16:9").strip().lower()
    text = _RATIO_ALIASES.get(text, text)
    if text in RATIOS:
        return RATIOS[text][quality]
    allowed = {r for sizes in RATIOS.values() for r in sizes.values()}
    if text in allowed:
        return text
    raise ValueError("ratio must be 16:9, 1:1, 9:16 (also 4:3, 3:4) or an exact Runway size such as 1920:1080")


def _reference_uri(reference_image: Any) -> str:
    """https URL as is; a local image (the caller resolved and permission-checked the path) as a data URI."""
    ref = str(reference_image or "").strip()
    if not ref:
        return ""
    if re.match(r"(?i)^https://", ref):
        return ref
    if re.match(r"(?i)^[a-z][a-z0-9+.-]*://", ref):
        raise ValueError("reference_image must be an https:// URL or an image file in the project")
    path = Path(ref)
    if not path.is_file():
        raise FileNotFoundError(f"reference image not found: {path}")
    from PIL import Image

    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as im:
        fmt = (im.format or "").upper()
        if fmt in {"JPEG", "PNG", "WEBP"} and len(raw) <= MAX_REFERENCE_BYTES and max(im.size) <= MAX_REFERENCE_SIDE:
            mime = {"JPEG": "jpeg", "PNG": "png", "WEBP": "webp"}[fmt]
            return f"data:image/{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        im = im.convert("RGB")
        im.thumbnail((MAX_REFERENCE_SIDE, MAX_REFERENCE_SIDE), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


def _download(http, url: str) -> bytes:
    if not re.match(r"(?i)^https://", str(url or "")):
        raise RuntimeError("Runway returned a non-https output URL; not downloaded")
    # No Authorization header: the API key never leaves for the CDN host.
    with http.get(url, stream=True, timeout=60) as response:
        if response.status_code >= 400:
            raise RuntimeError(f"ดาวน์โหลดรูปจาก Runway ไม่ได้ ({response.status_code}); ลิงก์อาจหมดอายุ")
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > MAX_DOWNLOAD:
                raise RuntimeError("รูปจาก Runway ใหญ่เกิน 30 MB")
    return bytes(data)


def account_status(check_credits: bool = False, http=None) -> dict[str, Any]:
    """Is a key configured (and, with check_credits, the read-only credit balance). Never returns the key."""
    key, source = _api_key()
    result: dict[str, Any] = {"configured": bool(key), "key_source": source or None}
    if key and check_credits:
        import requests

        http = http or requests.Session()
        response = http.get(f"{API_BASE}/v1/organization", headers=_headers(key), timeout=30)
        _raise_for(response, key, "อ่านยอดเครดิต")
        body = response.json()
        result["credit_balance"] = body.get("creditBalance")
    return result


def _generate_runway(out_dir: Path, prompt: str = "", ratio: Any = "16:9", output_name: str = "", reference_image: Any = "",
                      task_id: str = "", quality: str = "720p", timeout: float = TIMEOUT, http=None,
                      sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    quality = str(quality or "720p").strip().lower()
    if quality not in CREDITS:
        raise ValueError("quality must be 720p or 1080p")
    task_id = str(task_id or "").strip()
    resumed = bool(task_id)
    if task_id and not re.fullmatch(r"[A-Za-z0-9-]{8,64}", task_id):
        raise ValueError("task_id looks wrong (it is the id returned by an earlier generate_image call)")
    prompt = str(prompt or "").strip()
    if not task_id:
        if not prompt:
            raise ValueError("prompt is required (describe the picture; English usually works best)")
        if len(prompt) > MAX_PROMPT:
            raise ValueError(f"prompt is too long ({len(prompt)} > {MAX_PROMPT} characters)")
    size = _ratio(ratio, quality)
    reference = "" if task_id else _reference_uri(reference_image)
    key, _source = _api_key()
    if not key:
        return _not_configured()
    if http is None:
        import requests

        http = requests.Session()
    started = time.monotonic()
    try:
        if not task_id:
            body: dict[str, Any] = {"model": MODEL, "promptText": prompt, "ratio": size}
            if reference:
                body["referenceImages"] = [{"uri": reference, "tag": REFERENCE_TAG}]
            response = http.post(f"{API_BASE}/v1/text_to_image", json=body, headers=_headers(key), timeout=60)
            _raise_for(response, key, "สั่งสร้างรูป")
            task_id = str(response.json().get("id") or "")
            if not task_id:
                raise RuntimeError("Runway did not return a task id")
        deadline = time.monotonic() + max(10.0, float(timeout))
        task: dict[str, Any] = {}
        while True:
            try:
                response = http.get(f"{API_BASE}/v1/tasks/{task_id}", headers=_headers(key), timeout=30)
                if response.status_code in (429, 500, 502, 503, 504):
                    task = {"status": "RETRY"}
                else:
                    _raise_for(response, key, "ตรวจสถานะงาน")
                    task = response.json() or {}
            except (OSError, ValueError):  # connection drop / bad JSON: keep polling until the deadline
                task = {"status": "RETRY"}
            status = str(task.get("status") or "").upper()
            if status == "SUCCEEDED":
                break
            if status in _TERMINAL_FAIL:
                return {
                    "status": "failed",
                    "task_id": task_id,
                    "runway_status": status,
                    "failure": _redact(task.get("failure") or "", key),
                    "failure_code": task.get("failureCode"),
                    "message": "Runway สร้างรูปไม่สำเร็จ (เช่น prompt ไม่ผ่านการตรวจเนื้อหา) — ปรับ prompt แล้วลองใหม่",
                }
            if time.monotonic() >= deadline:
                return {
                    "status": "timeout",
                    "task_id": task_id,
                    "runway_status": status or "UNKNOWN",
                    "message": (f"ยังไม่เสร็จใน {int(timeout)} วินาที. เรียก generate_image อีกครั้งด้วย task_id={task_id} "
                                "เพื่อรับรูป (ไม่เสียเครดิตซ้ำ); ลิงก์ผลลัพธ์ของ Runway หมดอายุภายในไม่กี่ชั่วโมง"),
                }
            sleep(POLL_SECONDS)
        outputs = task.get("output") or []
        if not outputs:
            raise RuntimeError("Runway finished without an output image")
        data = _download(http, str(outputs[0]))
    except RuntimeError as exc:
        raise RuntimeError(_redact(str(exc), key)) from None

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            fmt, width, height = (im.format or "PNG").upper(), im.width, im.height
    except Exception as exc:
        raise RuntimeError(f"Runway output is not a valid image: {type(exc).__name__}") from None
    ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(fmt, "png")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = str(output_name or "").strip()
    if name.lower().endswith(("." + ext, ".png", ".jpg", ".jpeg", ".webp")):
        name = name.rsplit(".", 1)[0]
    target = DB._reserve(out_dir / f"{DB._safe_name(name, 'runway-' + time.strftime('%Y%m%d-%H%M%S'))[:100]}.{ext}")
    try:
        target.write_bytes(data)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    telegram = DB._telegram_copy(target)
    return {
        "status": "created",
        "path": str(target),
        "format": ext,
        "width": width,
        "height": height,
        "bytes": len(data),
        "model": MODEL,
        "ratio": f"{width}:{height}" if resumed else size,
        "quality": None if resumed else quality,
        "task_id": task_id,
        "resumed": resumed,
        "credits_estimate": 0 if resumed else CREDITS[quality],
        "used_reference_image": bool(reference),
        "seconds": round(time.monotonic() - started, 2),
        "telegram_attachment": str(telegram) if telegram else None,
        "media": [f"MEDIA:{telegram}"] if telegram else [],
        "delivery": "Tell the user the path. On Telegram, end the reply with a line of its own: MEDIA:<telegram_attachment>",
        "privacy": "The prompt (and reference image) were sent to Runway's cloud; credits were spent on the owner's Runway API account.",
    }


def local_status() -> dict[str, Any]:
    """Report optional-pack readiness without importing Torch or Diffusers into this service."""
    required = (LOCAL_PYTHON, LOCAL_WORKER, LOCAL_MODEL / "model_index.json", LOCAL_INSTALL_RECORD)
    missing = [str(path) for path in required if not path.is_file()]
    return {
        "installed": not missing,
        "provider": "qwen-image-2.1-local",
        "pack_root": str(LOCAL_PACK_ROOT),
        "model_root": str(LOCAL_MODEL),
        "missing": missing,
    }


def _generate_local(out_dir: Path, prompt: str, ratio: Any, output_name: str, reference_image: Any,
                    quality: str, timeout: float, seed: int | None) -> dict[str, Any]:
    status = local_status()
    if not status["installed"]:
        if status["missing"] == [str(LOCAL_INSTALL_RECORD)]:
            message = (
                "Qwen-Image 2.1 is downloaded but its installer's CUDA GPU check has not passed, so it is not enabled. "
                "It needs an NVIDIA GPU with the CUDA build of PyTorch; see logs\\qwen-image-install-*.log."
            )
        else:
            message = (
                "Qwen-Image 2.1 optional pack is not installed. Extract the separate optional-pack ZIP and run "
                "Install-Qwen-Image-2.1.cmd; the main Qwen Suite installer intentionally does not include these weights."
            )
        return {"status": "not_configured", "provider": "local", "message": message, "missing": status["missing"]}
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required (describe the picture; English usually works best)")
    if len(prompt) > 4000:
        raise ValueError(f"prompt is too long ({len(prompt)} > 4000 characters)")
    quality = str(quality or "720p").strip().lower()
    if quality not in CREDITS:
        raise ValueError("quality must be 720p or 1080p")
    size = _ratio(ratio, quality)
    width, height = (int(part) for part in size.split(":", 1))
    reference = str(reference_image or "").strip()
    if reference and re.match(r"(?i)^[a-z][a-z0-9+.-]*://", reference):
        raise ValueError("the local provider accepts only a local project image, not a URL")
    if reference and not Path(reference).is_file():
        raise FileNotFoundError(f"reference image not found: {reference}")
    seed = secrets.randbelow(2**31) if seed is None else int(seed)
    if seed < 0 or seed >= 2**63:
        raise ValueError("seed must be between 0 and 9223372036854775807")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = str(output_name or "").strip()
    if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        name = name.rsplit(".", 1)[0]
    target = DB._reserve(out_dir / f"{DB._safe_name(name, 'qwen-image-' + time.strftime('%Y%m%d-%H%M%S'))[:100]}.png")
    command = [
        str(LOCAL_PYTHON), str(LOCAL_WORKER), "--model-dir", str(LOCAL_MODEL), "--output", str(target),
        "--prompt", prompt, "--width", str(width), "--height", str(height),
        "--steps", "30" if quality == "720p" else "40", "--seed", str(seed),
    ]
    if reference:
        command.extend(("--image", reference))
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1800.0, float(timeout)),
            env=env,
            creationflags=0x08000000 if os.name == "nt" else 0,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown worker error").strip()[-1200:]
            raise RuntimeError(f"Qwen-Image 2.1 worker failed ({completed.returncode}): {detail}")
        if not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError("Qwen-Image 2.1 worker completed without an output image")
        from PIL import Image

        with Image.open(target) as image:
            image.verify()
        with Image.open(target) as image:
            actual_width, actual_height = image.size
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    telegram = DB._telegram_copy(target)
    return {
        "status": "created",
        "provider": "local",
        "path": str(target),
        "format": "png",
        "width": actual_width,
        "height": actual_height,
        "bytes": target.stat().st_size,
        "model": "Qwen/Qwen-Image-2.1",
        "ratio": size,
        "quality": quality,
        "seed": seed,
        "used_reference_image": bool(reference),
        "seconds": round(time.monotonic() - started, 2),
        "credits_estimate": 0,
        "telegram_attachment": str(telegram) if telegram else None,
        "media": [f"MEDIA:{telegram}"] if telegram else [],
        "delivery": "Tell the user the path. On Telegram, end the reply with a line of its own: MEDIA:<telegram_attachment>",
        "privacy": "Generation ran locally with Qwen-Image 2.1; no prompt or reference image was sent to Runway.",
    }


def generate(out_dir: Path, prompt: str = "", ratio: Any = "16:9", output_name: str = "", reference_image: Any = "",
             task_id: str = "", quality: str = "720p", timeout: float = TIMEOUT, http=None,
             sleep: Callable[[float], None] = time.sleep, provider: str = "auto", seed: int | None = None) -> dict[str, Any]:
    """Use the optional local Qwen pack when ready, otherwise preserve the Runway workflow.

    provider=auto falls back to Runway only when the installed local pack cannot run on this machine
    right now (no CUDA GPU, eGPU unplugged, out of GPU memory) and the request made no local-only
    promise (no seed, no local reference image, which must not be uploaded). Input errors and
    timeouts are reported instead of being retried in the cloud. provider=local never falls back."""
    provider = str(provider or "auto").strip().lower()
    if provider not in {"auto", "local", "runway"}:
        raise ValueError("provider must be auto, local, or runway")
    if task_id and provider == "local":
        raise ValueError("task_id is only used by the Runway provider")
    if provider == "local":
        return _generate_local(out_dir, prompt, ratio, output_name, reference_image, quality, timeout, seed)
    reference = str(reference_image or "").strip()
    url_reference = bool(re.match(r"(?i)^[a-z][a-z0-9+.-]*://", reference))  # only Runway takes URLs
    local_error = ""
    if provider == "auto" and not task_id and not url_reference and local_status()["installed"]:
        try:
            return _generate_local(out_dir, prompt, ratio, output_name, reference_image, quality, timeout, seed)
        except RuntimeError as exc:
            if seed is not None or reference or not _LOCAL_UNAVAILABLE.search(str(exc)):
                raise
            local_error = _shorten(f"{type(exc).__name__}: {exc}", 600)
    try:
        result = _generate_runway(
            out_dir, prompt, ratio=ratio, output_name=output_name, reference_image=reference_image,
            task_id=task_id, quality=quality, timeout=timeout, http=http, sleep=sleep,
        )
    except Exception as exc:
        if not local_error:
            raise
        raise RuntimeError(_shorten(
            f"Qwen-Image 2.1 could not run here ({local_error}); the Runway fallback also failed: "
            f"{type(exc).__name__}: {exc}", 1500)) from None
    if local_error:
        verb = "used" if result.get("status") == "created" else "tried"
        result["local_fallback"] = f"Qwen-Image 2.1 could not run here, so Runway was {verb} instead: {local_error}"
    if provider == "auto" and result.get("status") == "not_configured":
        result["local_optional_pack"] = local_status()
    return result
