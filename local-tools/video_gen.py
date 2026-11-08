"""generate_ai_video: short AI video clips made on this computer by the optional Wan 2.2 pack.

The pack (Wan 2.2 TI2V-5B on ComfyUI) is NOT part of the main Qwen Suite installer: it is the separate
Wan-2.2 Optional Pack ZIP with Install-Wan-2.2.cmd, and it needs an NVIDIA CUDA GPU (an eGPU works).
Until the pack's install.json exists - it is written only after the pack's final CUDA GPU check passed -
this module only explains how to get it and changes nothing.

Everything stays on this computer: ComfyUI listens on 127.0.0.1 only, is started on first use and keeps
running; after each clip the models are unloaded from the GPU (POST /free) so Ollama gets the memory
back. A clip takes minutes, so a call waits up to WAIT_SECONDS and otherwise returns status=running with
a task_id; calling again with that task_id continues waiting and then saves the file. Stdlib only.
"""
from __future__ import annotations

import json
import mimetypes
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

SUITE_ROOT = Path(__file__).resolve().parent.parent
PACK_ROOT = SUITE_ROOT / "optional" / "wan-2.2"
INSTALL_RECORD = PACK_ROOT / "install.json"
JOB_DIR = PACK_ROOT / "jobs"
PID_FILE = PACK_ROOT / "comfyui.pid"
LOG_DIR = SUITE_ROOT / "logs"
SERVER_LOG = LOG_DIR / "wan-comfyui.log"
DEFAULT_PORT = 8190
WAIT_SECONDS = 240          # per call; the clip keeps rendering after a call returns
POLL_SECONDS = 3
START_TIMEOUT = 240         # first start imports torch and ComfyUI
MAX_PROMPT = 2000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
FPS = 24
SIZES = {"16:9": (1280, 704), "9:16": (704, 1280)}
VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv")
# Nodes of ComfyUI's own Wan 2.2 TI2V-5B workflow; checked against /object_info before every submit so a
# ComfyUI update that renames one fails with a clear message instead of a half-run job.
REQUIRED_NODES = ("UNETLoader", "CLIPLoader", "VAELoader", "ModelSamplingSD3", "CLIPTextEncode",
                  "Wan22ImageToVideoLatent", "KSampler", "VAEDecode", "CreateVideo", "SaveVideo", "LoadImage")
# Wan's own default negative prompt (the model was trained with it; Chinese on purpose).
NEGATIVE_DEFAULT = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，"
                    "JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，"
                    "手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走")
_TASK_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


class PackError(RuntimeError):
    pass


# ----------------------------------------------------------------------------------------- install state

def _record() -> dict[str, Any] | None:
    """The pack's install.json, only if everything it points at still exists."""
    try:
        data = json.loads(INSTALL_RECORD.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    needed = [data.get("python"), data.get("comfy_dir"), data.get("model_root"), data.get("extra_model_paths")]
    if not all(isinstance(p, str) and p for p in needed):
        return None
    if not (Path(data["python"]).is_file() and (Path(data["comfy_dir"]) / "main.py").is_file()
            and (Path(data["model_root"]) / ".wan22-complete.json").is_file() and Path(data["extra_model_paths"]).is_file()):
        return None
    return data


def _not_installed() -> dict[str, Any]:
    return {
        "status": "not_installed",
        "message": ("ยังไม่ได้ติดตั้งชุดเสริม Wan 2.2 (สร้างคลิปวิดีโอ AI ในเครื่อง) จึงยังสร้างคลิปไม่ได้ และไม่มีการส่งข้อมูลออกนอกเครื่อง. "
                    "ชุดนี้ต้องใช้การ์ดจอ NVIDIA ที่รองรับ CUDA (ต่อเป็น eGPU ได้) compute capability 7.5 ขึ้นไป VRAM อย่างน้อย 8 GB "
                    "(แนะนำ 12 GB+) และพื้นที่ว่างประมาณ 30 GB. วิธีติดตั้ง: แตกไฟล์ Wan-2.2-Optional-Pack ZIP จากโฟลเดอร์ "
                    "Qwen Suite Installers แล้วดับเบิลคลิก Install-Wan-2.2.cmd — ตัวติดตั้งตรวจการ์ดจอก่อนและหยุดก่อนดาวน์โหลดไฟล์ใหญ่ถ้าไม่ผ่าน. "
                    "ระหว่างนี้ใช้ generate_video (วิดีโอแบบสไลด์ + เสียงบรรยาย) ได้ตามปกติ"),
    }


def pack_status() -> dict[str, Any]:
    """Read-only summary for status pages: installed or not, and whether ComfyUI answers."""
    rec = _record()
    if rec is None:
        return {"installed": False}
    port = int(rec.get("port") or DEFAULT_PORT)
    return {"installed": True, "version": rec.get("version"), "gpu": rec.get("gpu"), "port": port,
            "server": "running" if _server_up(f"http://127.0.0.1:{port}") else "stopped"}


# ----------------------------------------------------------------------------------------- HTTP helpers

def _http(method: str, url: str, body: Any = None, timeout: float = 15, headers: dict | None = None,
          raw: bool = False) -> Any:
    data = None
    hdrs = dict(headers or {})
    if body is not None and not isinstance(body, (bytes, bytearray)):
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif body is not None:
        data = bytes(body)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        raise PackError(f"ComfyUI ตอบ HTTP {exc.code}: {text[:500]}") from None
    if raw:
        return payload
    return json.loads(payload.decode("utf-8")) if payload else {}


def _server_up(base: str) -> bool:
    try:
        stats = _http("GET", base + "/system_stats", timeout=3)
        return isinstance(stats, dict) and "system" in stats
    except Exception:
        return False


def _log_tail(lines: int = 15) -> str:
    try:
        text = SERVER_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _server_command(rec: dict[str, Any], port: int) -> list[str]:
    comfy = Path(rec["comfy_dir"])
    return [rec["python"], str(comfy / "main.py"), "--listen", "127.0.0.1", "--port", str(port),
            "--extra-model-paths-config", rec["extra_model_paths"],
            "--output-directory", str(PACK_ROOT / "output"), "--input-directory", str(PACK_ROOT / "input"),
            "--user-directory", str(PACK_ROOT / "user"), "--disable-auto-launch"]


def _ensure_server(rec: dict[str, Any]) -> str:
    port = int(rec.get("port") or DEFAULT_PORT)
    base = f"http://127.0.0.1:{port}"
    if _server_up(base):
        return base
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for folder in ("output", "input", "user"):
        (PACK_ROOT / folder).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with SERVER_LOG.open("a", encoding="utf-8") as log:
        log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} starting ComfyUI for Wan 2.2 on {base}\n")
        log.flush()
        proc = subprocess.Popen(_server_command(rec, port), cwd=rec["comfy_dir"], stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, creationflags=flags)
    try:
        PID_FILE.write_text(str(proc.pid), encoding="ascii")
    except OSError:
        pass
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise PackError(f"ComfyUI หยุดทำงานระหว่างเริ่ม (exit {proc.returncode}). ท้าย log {SERVER_LOG}:\n{_log_tail()}")
        if _server_up(base):
            return base
        time.sleep(2)
    raise PackError(f"ComfyUI ยังไม่พร้อมภายใน {START_TIMEOUT} วินาที ดู {SERVER_LOG}")


def _check_nodes(base: str, rec: dict[str, Any]) -> None:
    info = _http("GET", base + "/object_info", timeout=60)
    missing = [name for name in REQUIRED_NODES if name not in info]
    if missing:
        raise PackError("ComfyUI รุ่นนี้ไม่มีโหนดที่ workflow ของ Wan 2.2 ต้องใช้: " + ", ".join(missing)
                        + " — ติดตั้งชุดเสริม Wan 2.2 ใหม่ (Install-Wan-2.2.cmd)")
    def options(node: str, field: str) -> list:
        try:
            spec = info[node]["input"]["required"][field][0]
        except (KeyError, IndexError, TypeError):
            return []
        return spec if isinstance(spec, list) else []
    files = rec.get("model_files") or {}
    for node, field, kind in (("UNETLoader", "unet_name", "diffusion_models"), ("CLIPLoader", "clip_name", "text_encoders"),
                              ("VAELoader", "vae_name", "vae")):
        want = files.get(kind)
        if want and want not in options(node, field):
            raise PackError(f"ComfyUI มองไม่เห็นไฟล์โมเดล {want} ({kind}) — ตรวจ {rec.get('extra_model_paths')} หรือรันตัวติดตั้งชุดเสริมอีกครั้ง")


def _upload_image(base: str, path: Path) -> str:
    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"start_image ใหญ่เกิน {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    boundary = uuid.uuid4().hex
    name = f"qwen-{uuid.uuid4().hex[:8]}{path.suffix.lower() or '.png'}"
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\ninput".encode(),
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue".encode(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    reply = _http("POST", base + "/upload/image", b"".join(parts), timeout=60,
                  headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    uploaded = reply.get("name") if isinstance(reply, dict) else None
    if not uploaded:
        raise PackError(f"อัปโหลดรูปตั้งต้นไม่สำเร็จ: {reply!r}"[:300])
    sub = reply.get("subfolder") or ""
    return f"{sub}/{uploaded}" if sub else uploaded


# ----------------------------------------------------------------------------------------- workflow

def frames_for(seconds: float) -> int:
    """Wan wants 4n+1 frames; 1-5 s at 24 fps -> 25..121."""
    n = int(round(max(1.0, min(5.0, float(seconds))) * FPS / 4))
    return max(25, min(121, 4 * n + 1))


def build_workflow(rec: dict[str, Any], prompt: str, negative: str, width: int, height: int, frames: int,
                   seed: int, image_name: str = "") -> dict[str, Any]:
    d = rec.get("defaults") or {}
    files = rec.get("model_files") or {}
    wf: dict[str, Any] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": files.get("diffusion_models", "wan2.2_ti2v_5B_fp16.safetensors"),
                                                       "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": files.get("text_encoders", "umt5_xxl_fp8_e4m3fn_scaled.safetensors"),
                                                       "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": files.get("vae", "wan2.2_vae.safetensors")}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"shift": float(d.get("shift", 8.0)), "model": ["1", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["2", 0]}},
        "7": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"width": width, "height": height, "length": frames,
                                                                    "batch_size": 1, "vae": ["3", 0]}},
        "8": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": int(d.get("steps", 20)), "cfg": float(d.get("cfg", 5.0)),
                                                    "sampler_name": str(d.get("sampler", "uni_pc")),
                                                    "scheduler": str(d.get("scheduler", "simple")), "denoise": 1.0,
                                                    "model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0],
                                                    "latent_image": ["7", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo", "inputs": {"fps": float(FPS), "images": ["9", 0]}},
        "11": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "qwen-suite/wan", "format": "auto", "codec": "auto",
                                                      "video": ["10", 0]}},
    }
    if image_name:
        wf["12"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        wf["7"]["inputs"]["start_image"] = ["12", 0]
    return wf


# ----------------------------------------------------------------------------------------- jobs

def _job_path(task_id: str) -> Path:
    return JOB_DIR / f"{task_id}.json"


def _save_job(task_id: str, job: dict[str, Any]) -> None:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _job_path(task_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, _job_path(task_id))


def _load_job(task_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(_job_path(task_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _safe_name(name: str) -> str:
    stem = re.sub(r"[^\w\-. ]+", "_", (name or "").strip(), flags=re.UNICODE).strip(" ._")
    stem = re.sub(r"\.(mp4|webm|mov|mkv)$", "", stem, flags=re.I)
    return stem[:80] or time.strftime("wan-clip-%Y%m%d-%H%M%S")


def _unique(folder: Path, stem: str, suffix: str) -> Path:
    candidate = folder / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = folder / f"{stem}-{n}{suffix}"
        n += 1
    return candidate


def _outputs_of(entry: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for node_out in (entry.get("outputs") or {}).values():
        if not isinstance(node_out, dict):
            continue
        for key in ("videos", "gifs", "images"):
            for item in node_out.get(key) or []:
                if isinstance(item, dict) and item.get("filename"):
                    found.append(item)
    videos = [i for i in found if str(i["filename"]).lower().endswith(VIDEO_EXT)]
    return videos or found


def _error_text(entry: dict[str, Any]) -> str:
    for msg in (entry.get("status") or {}).get("messages") or []:
        if isinstance(msg, list) and len(msg) == 2 and msg[0] == "execution_error" and isinstance(msg[1], dict):
            info = msg[1]
            return f"{info.get('node_type', '?')}: {info.get('exception_message', '').strip()}"[:600]
    return "ComfyUI รายงานว่างานล้มเหลว"


def _free(base: str) -> None:
    try:
        _http("POST", base + "/free", {"unload_models": True, "free_memory": True}, timeout=30)
    except Exception:
        pass  # best effort: ComfyUI frees on its own under memory pressure


def _queue_position(base: str, task_id: str) -> int | None:
    try:
        q = _http("GET", base + "/queue", timeout=10)
    except Exception:
        return None
    running = [item[1] for item in q.get("queue_running") or [] if isinstance(item, list) and len(item) > 1]
    if task_id in running:
        return 0
    pending = [item[1] for item in q.get("queue_pending") or [] if isinstance(item, list) and len(item) > 1]
    return pending.index(task_id) + 1 if task_id in pending else None


def _wait_and_collect(base: str, task_id: str, job: dict[str, Any], wait_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        history = _http("GET", base + f"/history/{task_id}", timeout=30)
        entry = history.get(task_id) if isinstance(history, dict) else None
        if entry:
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                _free(base)
                job.update(state="failed", error=_error_text(entry))
                _save_job(task_id, job)
                msg = job["error"]
                hint = " — หน่วยความจำการ์ดจอไม่พอ: ลองลดความยาวคลิป (seconds) หรือปิดโปรแกรมอื่นที่ใช้ GPU" if re.search(r"out of memory|OOM", msg, re.I) else ""
                return {"status": "failed", "task_id": task_id, "error": msg + hint}
            outputs = _outputs_of(entry)
            if status.get("completed") or outputs:
                if not outputs:
                    job.update(state="failed", error="no output file")
                    _save_job(task_id, job)
                    return {"status": "failed", "task_id": task_id, "error": "ComfyUI ทำงานเสร็จแต่ไม่มีไฟล์วิดีโอ"}
                folder = Path(job["outputs_dir"])
                folder.mkdir(parents=True, exist_ok=True)
                saved = []
                for i, item in enumerate(outputs[:1]):
                    query = urllib.parse.urlencode({"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                                                    "type": item.get("type", "output")})
                    blob = _http("GET", base + "/view?" + query, timeout=300, raw=True)
                    suffix = Path(item["filename"]).suffix.lower() or ".mp4"
                    target = _unique(folder, job["output_stem"], suffix)
                    tmp = target.with_name(target.name + ".part")
                    tmp.write_bytes(blob)
                    os.replace(tmp, target)
                    saved.append(str(target))
                _free(base)
                job.update(state="completed", files=saved, finished=time.strftime("%Y-%m-%dT%H:%M:%S"))
                _save_job(task_id, job)
                return {"status": "completed", "task_id": task_id, "file": saved[0], "files": saved,
                        "width": job["width"], "height": job["height"], "frames": job["frames"], "fps": FPS,
                        "seconds": round(job["frames"] / FPS, 2), "seed": job["seed"], "audio": False,
                        "elapsed_s": round(time.time() - job.get("created_ts", time.time()))}
        if time.monotonic() >= deadline:
            pos = _queue_position(base, task_id)
            if not entry and pos is None:
                job.update(state="lost")
                _save_job(task_id, job)
                return {"status": "failed", "task_id": task_id,
                        "error": "ComfyUI ไม่รู้จักงานนี้แล้ว (อาจถูกรีสตาร์ต) — สั่งสร้างใหม่ได้เลย"}
            where = "กำลังสร้าง" if pos == 0 else (f"รอคิวลำดับที่ {pos}" if pos else "กำลังสร้าง")
            return {"status": "running", "task_id": task_id, "queue_position": pos,
                    "message": (f"คลิปยัง{where}อยู่ (Wan 2.2 ใช้เวลาหลายนาที). เรียก generate_ai_video อีกครั้งด้วย "
                                f"task_id={task_id} เพื่อรอต่อและรับไฟล์ — งานไม่หายและไม่ต้องสั่งใหม่")}
        time.sleep(POLL_SECONDS)


# ----------------------------------------------------------------------------------------- entry point

def generate(outputs_dir: Path, prompt: str, *, seconds: float = 5, aspect_ratio: str = "16:9", start_image: str = "",
             negative_prompt: str = "", seed: int | None = None, output_name: str = "", task_id: str = "",
             wait_seconds: float = WAIT_SECONDS) -> dict[str, Any]:
    rec = _record()
    task_id = (task_id or "").strip()
    if task_id:
        if not _TASK_RE.match(task_id):
            raise ValueError("task_id ไม่ถูกต้อง (ใช้ค่าที่ generate_ai_video ส่งกลับมาครั้งก่อน)")
        job = _load_job(task_id)
        if job is None:
            raise ValueError(f"ไม่พบงาน task_id={task_id}")
        if job.get("state") == "completed":
            return {"status": "completed", "task_id": task_id, "file": (job.get("files") or [""])[0], "files": job.get("files"),
                    "already_saved": True}
        if job.get("state") in ("failed", "lost"):
            return {"status": "failed", "task_id": task_id, "error": job.get("error") or "งานนี้ล้มเหลวไปแล้ว"}
        if rec is None:
            return _not_installed()
        try:
            base = _ensure_server(rec)
            return _wait_and_collect(base, task_id, job, wait_seconds)
        except PackError as exc:
            return {"status": "failed", "task_id": task_id, "error": str(exc)}

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("ต้องมี prompt: บรรยายฉาก ตัวแบบ การเคลื่อนไหว และมุมกล้อง (ภาษาอังกฤษได้ผลดีที่สุด)")
    if len(prompt) > MAX_PROMPT:
        raise ValueError(f"prompt ยาวเกิน {MAX_PROMPT} ตัวอักษร")
    if aspect_ratio not in SIZES:
        raise ValueError("aspect_ratio ต้องเป็น 16:9 หรือ 9:16")
    image = Path(start_image) if start_image else None
    if image is not None and (not image.is_file() or image.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp")):
        raise ValueError("start_image ต้องเป็นไฟล์รูป .png .jpg หรือ .webp ในโปรเจกต์")
    if rec is None:
        return _not_installed()
    width, height = SIZES[aspect_ratio]
    frames = frames_for(seconds)
    seed = int(seed) if seed is not None and int(seed) >= 0 else random.randint(0, 2**32 - 1)
    try:
        base = _ensure_server(rec)
        _check_nodes(base, rec)
        image_name = _upload_image(base, image) if image else ""
        workflow = build_workflow(rec, prompt, (negative_prompt or "").strip() or NEGATIVE_DEFAULT,
                                  width, height, frames, seed, image_name)
        reply = _http("POST", base + "/prompt", {"prompt": workflow, "client_id": "qwen-suite"}, timeout=60)
    except PackError as exc:
        return {"status": "failed", "error": str(exc)}
    new_id = reply.get("prompt_id") if isinstance(reply, dict) else None
    if not new_id:
        return {"status": "failed", "error": f"ComfyUI ไม่รับงาน: {json.dumps(reply, ensure_ascii=False)[:500]}"}
    job = {"task_id": new_id, "state": "running", "outputs_dir": str(outputs_dir), "output_stem": _safe_name(output_name),
           "prompt": prompt[:500], "width": width, "height": height, "frames": frames, "seed": seed,
           "start_image": str(image) if image else "", "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "created_ts": time.time()}
    _save_job(new_id, job)
    try:
        return _wait_and_collect(base, new_id, job, wait_seconds)
    except PackError as exc:
        return {"status": "failed", "task_id": new_id, "error": str(exc)}
