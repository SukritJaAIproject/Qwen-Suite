from __future__ import annotations

import hashlib
import re
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool


OLLAMA_URL = os.environ.get("QWEN_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
LOG_PATH = Path(os.environ.get("QWEN_ROUTER_LOG", r"E:\localllm\logs\model-router.jsonl"))
HOST = "127.0.0.1"
PORT = 8770
# Written by Resolve-OllamaGpu.ps1 each time the Ollama server starts (added 2026-09-29).
GPU_PROFILE_PATH = Path(
    os.environ.get("QWEN_GPU_PROFILE", str(Path(__file__).resolve().parents[1] / "data" / "gpu-profile.json"))
)

GENERAL_MODEL = "qwen3.8:27b"
CODE_MODEL = "qwen3-coder:30b"
AGENT_MODEL = "muse-glimmer:30b"
STRUCTURED_MODEL = "granite4.2:8b"
LEGACY_REVIEW_MODEL = "gpt-oss:20b"
LEGACY_FAST_MODEL = "qwen3:4b"

PROFILES: dict[str, dict[str, str]] = {
    "qwen-auto:latest": {
        "name": "Qwen Auto Router",
        "target": "auto",
        "description": "Automatically selects General, Code, Agent, Structured/RAG, or Review for each request.",
    },
    "qwen-general:latest": {
        "name": "Qwen General + Vision",
        "target": GENERAL_MODEL,
        "description": "Best default for professional work, Thai, tools, images, and long tasks.",
    },
    "qwen-code:latest": {
        "name": "Qwen Coding",
        "target": CODE_MODEL,
        "description": "Repository work, programming, debugging, and automation.",
    },
    "qwen-fast:latest": {
        "name": "Qwen Fast Structured + RAG",
        "target": STRUCTURED_MODEL,
        "description": "Granite 4.2 8B for fast structured output, RAG answers, classification, and formatting.",
    },
    "qwen-structured:latest": {
        "name": "Qwen Structured + RAG",
        "target": STRUCTURED_MODEL,
        "description": "Granite 4.2 8B for JSON, schemas, extraction, and retrieval-grounded answers.",
    },
    "qwen-agent:latest": {
        "name": "Qwen Agent",
        "target": AGENT_MODEL,
        "description": "Muse Glimmer 30B for tool use, multi-step agent work, and failure recovery.",
    },
    "qwen-review:latest": {
        "name": "Qwen Second Opinion",
        "target": AGENT_MODEL,
        "description": "Muse Glimmer 30B for independent review, critique, and second-pass checking.",
    },
}

CODE_TERMS = (
    " code", "coding", "python", "javascript", "typescript", "powershell", "sql", "api", "bug", "debug",
    "function", "class ", "repository", "repo", "git", ".py", ".js", ".ts", ".ps1", ".cs", ".cpp",
    "เขียนโปรแกรม", "แก้บั๊ก", "โค้ด", "สคริปต์", "ฐานข้อมูล",
)
REVIEW_TERMS = (
    "second opinion", "review this", "critique", "audit", "verify the answer", "double-check",
    "ตรวจคำตอบ", "ตรวจสอบอีกครั้ง", "วิจารณ์", "หาจุดผิด", "มุมมองที่สอง",
)
RESEARCH_TERMS = (
    "research", "latest", "current", "compare", "analyze", "source", "citation", "evidence",
    "ค้นข้อมูล", "ล่าสุด", "วิเคราะห์", "เปรียบเทียบ", "แหล่งอ้างอิง", "หลักฐาน", "หุ้น", "กฎหมาย",
)
FAST_TERMS = (
    "hello", "hi ", "thanks", "thank you", "translate", "summarize briefly", "format this",
    "สวัสดี", "ขอบคุณ", "แปล", "สรุปสั้น", "จัดรูปแบบ", "ทักทาย",
)

# Re-declare the routing vocabulary in clean UTF-8. Older builds wrote the
# Thai literals through a legacy Windows code page, which made Thai prompts
# miss every intent rule.
CODE_TERMS = (
    " code", "coding", "python", "javascript", "typescript", "powershell", "sql", "api", "bug", "debug",
    "function", "class ", "repository", "repo", "git", ".py", ".js", ".ts", ".ps1", ".cs", ".cpp",
    "github", "gitlab", "mysql", "sqlite", "postgres", "postgresql", "regex", "traceback", "stack trace", "compile",
    "เขียนโปรแกรม", "แก้บั๊ก", "โค้ด", "สคริปต์", "ฐานข้อมูล", "ดีบัก",
)
REVIEW_TERMS = (
    "second opinion", "review this", "critique", "audit", "verify the answer", "double-check",
    "ตรวจคำตอบ", "ตรวจสอบอีกครั้ง", "วิจารณ์", "หาจุดผิด", "มุมมองที่สอง", "ทวนคำตอบ",
)
RESEARCH_TERMS = (
    "research", "latest", "current", "compare", "analyze", "analysis", "source", "citation", "evidence",
    "ค้นข้อมูล", "ล่าสุด", "วิเคราะห์", "เปรียบเทียบ", "แหล่งอ้างอิง", "หลักฐาน", "หุ้น", "กฎหมาย", "ข่าว",
)
FAST_TERMS = (
    "hello", "hi ", "thanks", "thank you", "translate", "summarize briefly", "format this",
    "สวัสดี", "ขอบคุณ", "แปล", "สรุปสั้น", "จัดรูปแบบ", "ทักทาย",
)
IMPORTANT_TERMS = (
    "important", "critical", "high stakes", "final answer", "executive", "board meeting", "board of directors", "must be correct",
    "สำคัญ", "งานสำคัญ", "ตรวจสองรอบ", "ต้องถูกต้อง", "ส่งผู้บริหาร", "ตัดสินใจ", "อนุมัติ",
)
STRUCTURED_TERMS = (
    "structured output", "json", "jsonl", "yaml", "xml", "schema", "extract fields", "classification",
    "rag", "retrieval augmented", "retrieval-augmented", "knowledge base", "grounded answer", "context below",
    "ข้อมูลแบบมีโครงสร้าง", "ส่งออกเป็น json", "ฐานความรู้", "ค้นคืนข้อมูล", "ตอบจากเอกสาร", "ดึงข้อมูลตามฟิลด์",
)

def _term_pattern(term: str) -> re.Pattern[str] | None:
    # ASCII terms match whole words only ('repo' must not hit 'report', 'git' not 'digital',
    # 'api' not 'capital', '.cs' not '.csv'), but with common word endings and version numbers
    # ('bugs', 'debugging', 'coded', 'debugger', 'python3', 'apis'). A leading '.' is a file
    # extension, so it may follow a name. Thai terms stay substring matches (no spaces in Thai).
    word = term.strip().casefold()
    if not word or not word.isascii():
        return None
    head = "" if word.startswith(".") else r"(?<![0-9a-z_])"
    tail = "" if word.startswith(".") else r"(?:e?s|g?ing|g?ed|d|g?e?rs?|\d[\d.]*)?"
    return re.compile(head + re.escape(word) + tail + r"(?![0-9a-z_])")


def _matcher(terms: Iterable[str]):
    patterns = [p for p in (_term_pattern(t) for t in terms) if p is not None]
    thai = [t.strip().casefold() for t in terms if t.strip() and not t.strip().isascii()]

    def hit(text: str) -> bool:
        lowered = text.casefold()
        return any(p.search(lowered) for p in patterns) or any(t in lowered for t in thai)

    return hit


_is_code = _matcher(CODE_TERMS)
_is_review = _matcher(REVIEW_TERMS)
_is_research = _matcher(RESEARCH_TERMS)
_is_important = _matcher(IMPORTANT_TERMS)
_is_structured = _matcher(STRUCTURED_TERMS)

app = FastAPI(title="Qwen Smart Model Router", version="1.3.0")
_session = requests.Session()
_available_cache: tuple[float, set[str]] = (0.0, set())
_cache_lock = threading.Lock()
_gpu_profile_cache: tuple[float, float, dict[str, Any]] = (0.0, -1.0, {})


def gpu_profile() -> dict[str, Any]:
    """The GPU profile the running Ollama server was started with; {} if unknown."""
    global _gpu_profile_cache
    checked, mtime, data = _gpu_profile_cache
    # monotonic: a wall-clock correction must not keep a stale profile for hours
    if checked and time.monotonic() - checked < 10:
        return data
    try:
        current = GPU_PROFILE_PATH.stat().st_mtime
        if current != mtime:
            data = json.loads(GPU_PROFILE_PATH.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                data = {}
        mtime = current
    except (OSError, ValueError):
        data, mtime = {}, -1.0
    _gpu_profile_cache = (time.monotonic(), mtime, data)
    return data


def _json_log(value: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def _text_from_messages(messages: Iterable[dict[str, Any]]) -> tuple[str, bool]:
    parts: list[str] = []
    has_media = False
    for message in messages:
        role = str(message.get("role", "user")).casefold()
        if role != "user":
            continue
        if message.get("images"):
            has_media = True
        content = message.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") in {"image", "image_url", "video", "input_image", "input_video"}:
                    has_media = True
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts), has_media


def _user_texts(messages: Iterable[dict[str, Any]]) -> list[str]:
    texts = []
    for message in messages:
        if isinstance(message, dict) and str(message.get("role", "user")).casefold() == "user":
            texts.append(_text_from_messages([message])[0])
    return texts


def _latest_user_text(messages: Iterable[dict[str, Any]]) -> str:
    # The intent of a turn is in the newest user message; old words far back in a long-lived
    # session (OpenClaw keeps days of Telegram history) must not steer every later turn.
    texts = _user_texts(messages)
    return texts[-1] if texts else ""


RECENT_USER_TURNS = 3


def _all_chars(messages: Iterable[dict[str, Any]]) -> int:
    """Characters of every message, assistant replies included (for the length rule)."""
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(i.get("text") or "") for i in content if isinstance(i, dict) and isinstance(i.get("text"), str))
    return total


def available_models(force: bool = False) -> set[str]:
    global _available_cache
    with _cache_lock:
        if not force and time.time() - _available_cache[0] < 30:
            return set(_available_cache[1])
        response = _session.get(f"{OLLAMA_URL}/api/tags", timeout=15)
        response.raise_for_status()
        names = {
            str(item.get("name") or item.get("model"))
            for item in response.json().get("models", [])
            if item.get("name") or item.get("model")
        }
        _available_cache = (time.time(), names)
        return names


def preferred_review_model(installed: set[str] | None = None) -> str | None:
    """Use Muse for review when installed, retaining GPT-OSS as an upgrade-safe fallback."""
    installed = available_models() if installed is None else installed
    for candidate in (AGENT_MODEL, LEGACY_REVIEW_MODEL):
        if candidate in installed:
            return candidate
    return None


def classify(payload: dict[str, Any]) -> tuple[str, str]:
    messages = payload.get("messages") or []
    messages = messages if isinstance(messages, list) else []
    text, has_media = _text_from_messages(messages)
    if not text and isinstance(payload.get("prompt"), str):
        text = str(payload["prompt"])
    latest = _latest_user_text(messages) or text
    # A follow-up with no intent words of its own ("now make it faster") keeps the route of
    # the last few user turns, so a coding thread stays on the coder instead of reloading
    # another model; older history is ignored.
    if not any(f(latest) for f in (_is_code, _is_important, _is_review, _is_research, _is_structured)):
        recent = "\n".join(_user_texts(messages)[-RECENT_USER_TURNS:])
        if _is_code(recent):
            latest = recent
    has_tools = bool(payload.get("tools"))
    # Images first: only qwen3.8 can see them; the coder and gpt-oss have no vision and
    # loading them would also evict qwen3.8.
    if has_media:
        return GENERAL_MODEL, "image or video input"
    if _is_code(latest):
        return CODE_MODEL, "coding or repository task"
    if _is_important(latest):
        return GENERAL_MODEL, "important request requires a strong draft before second-pass review"
    if _is_review(latest):
        return AGENT_MODEL, "independent review requested"
    if has_tools:
        return AGENT_MODEL, "tool calling or agent workflow"
    if _is_structured(latest):
        return STRUCTURED_MODEL, "structured output or retrieval-grounded task"
    # Length is the whole conversation, assistant replies included: a short follow-up in a
    # long chat must not switch to the small model, which would reload and reread everything.
    size = max(len(text), _all_chars(messages))
    if size > 1800 or _is_research(latest):
        return GENERAL_MODEL, "complex, current, or research-oriented request"
    if size <= 320 and text.strip():
        return STRUCTURED_MODEL, "short low-risk request"
    return GENERAL_MODEL, "general default"


def needs_second_pass(payload: dict[str, Any]) -> bool:
    """Review important text answers, but never interfere with tool or media turns."""
    if payload.get("tools"):
        return False
    messages = payload.get("messages") or []
    text, has_media = _text_from_messages(messages if isinstance(messages, list) else [])
    if not text and isinstance(payload.get("prompt"), str):
        text = str(payload["prompt"])
    if has_media:
        return False
    return _is_important(_latest_user_text(messages if isinstance(messages, list) else []) or text)


def resolve_model(requested: str, payload: dict[str, Any]) -> tuple[str, str]:
    profile = PROFILES.get(requested)
    if not profile and requested.endswith(":latest"):
        profile = PROFILES.get(requested)
    if profile and profile["target"] == "auto":
        target, reason = classify(payload)
    elif profile:
        target, reason = profile["target"], f"selected profile {requested}"
    else:
        return requested, "direct model request"
    available = available_models()
    if target in available:
        return target, reason
    # Preserve the requested role during an in-place upgrade: old review/fast models are
    # better substitutes for their successors than an unrelated large model.
    role_fallbacks = {
        AGENT_MODEL: (LEGACY_REVIEW_MODEL, GENERAL_MODEL, CODE_MODEL, STRUCTURED_MODEL, LEGACY_FAST_MODEL),
        STRUCTURED_MODEL: (LEGACY_FAST_MODEL, GENERAL_MODEL, AGENT_MODEL, CODE_MODEL, LEGACY_REVIEW_MODEL),
        GENERAL_MODEL: (AGENT_MODEL, CODE_MODEL, STRUCTURED_MODEL, LEGACY_REVIEW_MODEL, LEGACY_FAST_MODEL),
        CODE_MODEL: (GENERAL_MODEL, AGENT_MODEL, STRUCTURED_MODEL, LEGACY_REVIEW_MODEL, LEGACY_FAST_MODEL),
    }
    for fallback in role_fallbacks.get(
        target, (GENERAL_MODEL, AGENT_MODEL, CODE_MODEL, STRUCTURED_MODEL, LEGACY_REVIEW_MODEL, LEGACY_FAST_MODEL)
    ):
        if fallback in available:
            return fallback, f"{reason}; {target} unavailable, used fallback"
    raise RuntimeError("No routed Ollama model is installed.")


def _profile_tags() -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    models = []
    for model_id, profile in PROFILES.items():
        digest = hashlib.sha256(model_id.encode("utf-8")).hexdigest()
        models.append(
            {
                "name": model_id,
                "model": model_id,
                "modified_at": now,
                "size": 0,
                "digest": digest,
                "details": {
                    "parent_model": profile["target"],
                    "format": "qwen-router",
                    "family": "qwen-router",
                    "families": ["qwen-router"],
                    "parameter_size": "AUTO" if profile["target"] == "auto" else profile["target"],
                    "quantization_level": "managed",
                },
            }
        )
    return {"models": models}


def _forward_json(path: str, payload: dict[str, Any]) -> JSONResponse:
    data, status_code = _post_json(path, payload)
    return JSONResponse(data, status_code=status_code)


def _post_json(path: str, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    response = _session.post(f"{OLLAMA_URL}{path}", json=payload, timeout=1800)
    try:
        data = response.json()
    except ValueError:
        data = {"error": response.text}
    return data, response.status_code


def _response_text(path: str, data: dict[str, Any]) -> str:
    if path == "/api/chat":
        return str((data.get("message") or {}).get("content") or "")
    return str(data.get("response") or "")


def _review_second_pass(
    path: str, routed: dict[str, Any], first: dict[str, Any], review_model: str
) -> tuple[dict[str, Any], bool]:
    draft = _response_text(path, first).strip()
    if not draft or first.get("error"):
        return first, False
    original, _ = _text_from_messages(routed.get("messages") or [])
    if not original:
        original = str(routed.get("prompt") or "")
    review_payload = {
        "model": review_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are the independent final verifier. Check the draft for factual, logical, numerical, "
                    "safety, and instruction-following errors. Return only the corrected final answer in the same "
                    "language as the user. Preserve valid source citations and never invent sources or completed actions."
                ),
            },
            {
                "role": "user",
                "content": f"ORIGINAL REQUEST:\n{original[-16000:]}\n\nDRAFT ANSWER:\n{draft[-16000:]}",
            },
        ],
        "stream": False,
        "think": False,
        "keep_alive": "15m",
        "options": {"temperature": 0.2, "num_ctx": 32768},
    }
    reviewed, status = _post_json("/api/chat", review_payload)
    final_text = _response_text("/api/chat", reviewed).strip()
    if status >= 400 or not final_text:
        return first, False
    result = dict(first)
    result["model"] = review_model
    if path == "/api/chat":
        message = dict(result.get("message") or {})
        message["role"] = "assistant"
        message["content"] = final_text
        result["message"] = message
    else:
        result["response"] = final_text
    result["qwen_router"] = {
        "draft_model": routed.get("model"),
        "review_model": review_model,
        "second_pass": True,
    }
    return result, True


def _single_stream(data: dict[str, Any]) -> StreamingResponse:
    encoded = (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")
    return StreamingResponse(iter((encoded,)), media_type="application/x-ndjson")


def _get_json(path: str) -> JSONResponse:
    response = _session.get(f"{OLLAMA_URL}{path}", timeout=15)
    try:
        data = response.json()
    except ValueError:
        data = {"error": response.text}
    return JSONResponse(data, status_code=response.status_code)


def _forward_stream(path: str, payload: dict[str, Any]) -> StreamingResponse:
    def iterator():
        try:
            with _session.post(f"{OLLAMA_URL}{path}", json=payload, timeout=1800, stream=True) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=16 * 1024):
                    if chunk:
                        yield chunk
        except requests.RequestException as exc:
            # Do not let an interrupted Ollama stream tear down Starlette's
            # chunked HTTP response. Open WebUI otherwise reports the opaque
            # aiohttp error "Not enough data to satisfy transfer length
            # header" and loses the useful failure reason.
            _json_log(
                {
                    "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "event": "upstream_stream_error",
                    "model": str(payload.get("model", "")),
                    "path": path,
                    "error": str(exc),
                }
            )
            yield (json.dumps({"error": f"Ollama stream interrupted: {exc}", "done": True}, ensure_ascii=False) + "\n").encode("utf-8")

    return StreamingResponse(iterator(), media_type="application/x-ndjson")


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        installed = sorted(available_models(force=True))
        return {"ok": True, "profiles": PROFILES, "installed": installed, "ollama": OLLAMA_URL}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "ollama": OLLAMA_URL}


@app.get("/api/tags")
async def tags() -> dict[str, Any]:
    return _profile_tags()


@app.post("/api/show")
async def show(request: Request) -> JSONResponse:
    body = await request.json()
    requested = str(body.get("model") or body.get("name") or "")
    if requested in PROFILES:
        profile = PROFILES[requested]
        return JSONResponse(
            {
                "license": "Virtual local routing profile",
                "modelfile": f"FROM {profile['target']}",
                "parameters": "num_ctx 32768",
                "template": "",
                "system": profile["description"],
                "details": {"family": "qwen-router", "parameter_size": profile["target"]},
                "capabilities": ["completion", "tools", "vision"],
            }
        )
    return await run_in_threadpool(_forward_json, "/api/show", body)


@app.get("/api/version")
async def version() -> JSONResponse:
    return await run_in_threadpool(_get_json, "/api/version")


# Models whose llama-server turns KV-cache shifting off by itself (logged at every load:
# "KV cache shifting is not supported for this context, disabling"), so 'shift' is inert.
# muse-glimmer:30b logs the same line (checked 2026-10-07); gpt-oss:20b stays for upgraded
# machines that still have it installed.
NO_KV_SHIFT_MODELS = {"qwen3.8:27b", "qwen3.8-backup:27b", "muse-glimmer:30b", "gpt-oss:20b"}


@app.get("/api/ps")
async def process_status() -> JSONResponse:
    return await run_in_threadpool(_get_json, "/api/ps")


async def _route_request(request: Request, path: str) -> JSONResponse | StreamingResponse:
    body = await request.json()
    requested = str(body.get("model", "qwen-auto:latest"))
    try:
        target, reason = await run_in_threadpool(resolve_model, requested, body)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    routed = dict(body)
    routed["model"] = target
    # Every caller that loads a model must launch it with the same runner options, or Ollama
    # 0.32 tears the runner down and starts it again: 17 GB for qwen3.8:27b, and the prompt
    # cache is lost (OpenClaw's ~20k-token prompt is then read again - 13 to 23 minutes on
    # the CPU). The server log of 2026-09-23..29 shows 7 such reloads; 6 were 'shift' flips.
    # So the router makes its callers look exactly like the ones that talk to Ollama directly
    # (Open WebUI's direct models and RAG, the internet gateway, the video worker):
    #  - no top-level 'shift' for the models whose llama-server disables KV-cache shifting
    #    by itself (qwen3.8 = qwen35, muse-glimmer, gpt-oss: "KV cache shifting is not supported"), so the
    #    model's default applies to everyone there. OpenClaw hard-codes shift:false, and it
    #    also sends truncate:false, so for these models the flag changes nothing but the
    #    reload. For models that can shift (qwen3-coder, qwen3:4b), the caller's shift passes
    #    through: without it an answer that overflows the 32k context would keep going with
    #    the system prompt and tool schema cut out, instead of stopping. Those models load
    #    rarely and serve one request per load here, so a reload there costs little.
    #  - no options.num_thread, on every GPU setup: llama-server then uses the 6 physical
    #    cores, as for OpenClaw, which served nearly every load here.
    # num_ctx is left alone: every caller sends 32768 or nothing, and nothing means the
    # server's OLLAMA_CONTEXT_LENGTH (32768).
    if target in NO_KV_SHIFT_MODELS:
        routed.pop("shift", None)
    if isinstance(routed.get("options"), dict):
        routed["options"] = {k: v for k, v in routed["options"].items() if k != "num_thread"}
    text, _ = _text_from_messages(routed.get("messages") or [])
    if not text and isinstance(routed.get("prompt"), str):
        text = str(routed["prompt"])
    review_model = await run_in_threadpool(preferred_review_model)
    second_pass = bool(
        requested == "qwen-auto:latest"
        and review_model
        and target != review_model
        and needs_second_pass(body)
    )
    _json_log(
        {
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "requested": requested,
            "selected": target,
            "reason": reason,
            "prompt_chars": len(text),
            "stream": bool(routed.get("stream", False)),
            "second_pass_requested": second_pass,
        }
    )
    if second_pass:
        wants_stream = bool(routed.get("stream", False))
        routed["stream"] = False
        # Release the large draft model as soon as the draft is complete so
        # the independent reviewer can load without RAM pressure.
        routed["keep_alive"] = 0
        first, status = await run_in_threadpool(_post_json, path, routed)
        if status >= 400:
            return JSONResponse(first, status_code=status)
        final, reviewed = await run_in_threadpool(_review_second_pass, path, routed, first, review_model)
        _json_log(
            {
                "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "requested": requested,
                "selected": target,
                "second_pass_completed": reviewed,
                "review_model": review_model if reviewed else None,
            }
        )
        return _single_stream(final) if wants_stream else JSONResponse(final)
    if routed.get("stream", False):
        return _forward_stream(path, routed)
    return await run_in_threadpool(_forward_json, path, routed)


@app.post("/router/decision")
async def router_decision(request: Request) -> dict[str, Any]:
    body = await request.json()
    target, reason = await run_in_threadpool(resolve_model, "qwen-auto:latest", body)
    review_model = await run_in_threadpool(preferred_review_model)
    return {
        "target": target,
        "reason": reason,
        "second_pass": bool(review_model and target != review_model and needs_second_pass(body)),
        "review_model": review_model,
    }


@app.post("/api/chat")
async def chat(request: Request):
    return await _route_request(request, "/api/chat")


@app.post("/api/generate")
async def generate(request: Request):
    return await _route_request(request, "/api/generate")


@app.post("/api/embed")
@app.post("/api/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    return await run_in_threadpool(_forward_json, "/api/embed", body)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
