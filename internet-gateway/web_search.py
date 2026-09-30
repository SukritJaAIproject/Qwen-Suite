from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class WebSearchError(RuntimeError):
    """A safe, user-facing web-search failure."""


_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]"
)
_TAG_RE = re.compile(r"<[^>]{0,500}>")
_SPACE_RE = re.compile(r"\s+")
_MARKDOWN_URL_RE = re.compile(r"\[([^\]\r\n]{1,300})\]\(https?://[^)\s]+\)", re.I)
_RAW_URL_RE = re.compile(r"https?://[^\s<>\]\)]+", re.I)
_CITATION_RE = re.compile(r"\[S(\d{1,3})\]", re.I)
_EVIDENCE_MARKER_RE = re.compile(
    r"BEGIN_UNTRUSTED_WEB_EVIDENCE|END_UNTRUSTED_WEB_EVIDENCE|\[S\d{1,3}\]",
    re.I,
)
_BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa")
_ALLOWED_BACKENDS = {
    "auto",
    "bing",
    "brave",
    "duckduckgo",
    "google",
    "mojeek",
    "wikipedia",
    "yahoo",
    "yandex",
}
_CACHE: dict[tuple[str, str, int], tuple[float, list[dict[str, str]]]] = {}
_CACHE_TTL_SECONDS = 300
_MAX_WORKER_OUTPUT_BYTES = 512 * 1024


def clean_text(value: Any, limit: int) -> str:
    text = html.unescape(str(value or ""))
    text = _TAG_RE.sub(" ", text)
    text = _CONTROL_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text[: max(0, limit)].strip()


def clean_source_url(value: Any, limit: int = 600) -> str:
    raw = _CONTROL_RE.sub("", str(value or "").strip())
    if not raw or len(raw) > limit or _EVIDENCE_MARKER_RE.search(raw):
        return ""
    try:
        parsed = urlsplit(raw)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.lower() != "https" or not hostname:
        return ""
    if parsed.username or parsed.password or port not in (None, 443):
        return ""
    if hostname == "localhost" or hostname.endswith(_BLOCKED_HOST_SUFFIXES):
        return ""
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return ""
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


def clean_results(raw_results: Any, max_results: int = 5) -> list[dict[str, str]]:
    if not isinstance(raw_results, list):
        return []
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = clean_source_url(item.get("href") or item.get("url") or item.get("link"))
        if not url or url in seen:
            continue
        title = clean_text(item.get("title") or "Untitled source", 180)
        snippet = clean_text(
            item.get("body") or item.get("snippet") or item.get("text"), 1400
        )
        published = clean_text(
            item.get("date") or item.get("published") or item.get("published_at"), 80
        )
        if not snippet:
            continue
        seen.add(url)
        result = {"title": title or "Untitled source", "url": url, "snippet": snippet}
        if published:
            result["published"] = published
        results.append(result)
        if len(results) >= max(1, min(int(max_results), 8)):
            break
    return results


def render_web_context(results: list[dict[str, str]], max_chars: int = 12000) -> str:
    sections = ["BEGIN_UNTRUSTED_WEB_EVIDENCE"]
    used = len(sections[0])
    for index, item in enumerate(results, start=1):
        title = _EVIDENCE_MARKER_RE.sub("[ข้อความที่ปรับเพื่อความปลอดภัย]", item["title"])
        snippet = _EVIDENCE_MARKER_RE.sub(
            "[ข้อความที่ปรับเพื่อความปลอดภัย]", item["snippet"]
        )
        section = (
            f"\n[S{index}]\n"
            f"Title: {title}\n"
            f"URL: {item['url']}\n"
            f"Published: {item.get('published') or 'not supplied by search provider'}\n"
            f"Snippet: {snippet}\n"
        )
        remaining = max_chars - used - len("\nEND_UNTRUSTED_WEB_EVIDENCE")
        if remaining <= 80:
            break
        section = section[:remaining]
        sections.append(section)
        used += len(section)
    sections.append("\nEND_UNTRUSTED_WEB_EVIDENCE")
    return "".join(sections)


def _truncate_utf16(value: str, max_units: int) -> str:
    if max_units <= 0:
        return ""
    output: list[str] = []
    units = 0
    for character in value:
        character_units = len(character.encode("utf-16-le")) // 2
        if units + character_units > max_units:
            break
        output.append(character)
        units += character_units
    return "".join(output)


def _clean_model_answer(answer: str, source_count: int) -> str:
    text = _CONTROL_RE.sub("", answer.strip())
    text = _MARKDOWN_URL_RE.sub(r"\1", text)
    text = _RAW_URL_RE.sub("", text)

    def keep_valid(match: re.Match[str]) -> str:
        number = int(match.group(1))
        return f"[S{number}]" if 1 <= number <= source_count else ""

    return _CITATION_RE.sub(keep_valid, text).strip()


def append_verified_sources(
    answer: str,
    results: list[dict[str, str]],
    max_chars: int,
    max_utf16_units: int = 21000,
) -> str:
    source_lines = ["ผลการค้นเว็บ (ระบบรับเฉพาะลิงก์ HTTPS ที่ผ่านการตรวจรูปแบบ):"]
    for index, item in enumerate(results, start=1):
        title = _EVIDENCE_MARKER_RE.sub(
            "[ข้อความที่ปรับเพื่อความปลอดภัย]", item["title"]
        )
        date_note = f" · {item['published']}" if item.get("published") else ""
        source_lines.append(f"[S{index}] {title}{date_note}\n{item['url']}")
    sources = "\n\n" + "\n".join(source_lines)
    cleaned = _clean_model_answer(answer, len(results))
    char_budget = max(0, int(max_chars) - len(sources))
    utf16_budget = max(0, max_utf16_units - len(sources.encode("utf-16-le")) // 2)
    cleaned = _truncate_utf16(cleaned[:char_budget], utf16_budget).rstrip()
    return (cleaned + sources)[: int(max_chars)]


async def _run_worker(
    query: str, max_results: int, backend: str, timeout_seconds: float
) -> list[dict[str, str]]:
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creation_flags,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    request = json.dumps(
        {"query": query, "max_results": max_results, "backend": backend},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(request),
            timeout=max(3.0, min(float(timeout_seconds), 12.0)),
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return []
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
            await asyncio.shield(process.wait())
        raise
    if process.returncode != 0 or len(stdout) > _MAX_WORKER_OUTPUT_BYTES:
        return []
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    return clean_results(payload.get("results"), max_results)


async def search_web(
    query: str,
    *,
    max_results: int = 5,
    timeout_seconds: float = 15,
    backend: str = "auto",
    max_query_chars: int = 500,
) -> list[dict[str, str]]:
    query = clean_text(query, max_query_chars)
    if not query:
        raise WebSearchError("กรุณาระบุคำถามสำหรับค้นเว็บ")
    backend = backend if backend in _ALLOWED_BACKENDS else "auto"
    max_results = max(1, min(int(max_results), 8))
    cache_key = (backend, query.casefold(), max_results)
    cached = _CACHE.get(cache_key)
    if cached and cached[0] >= time.monotonic():
        return [dict(item) for item in cached[1]]

    total_timeout = max(6.0, min(float(timeout_seconds), 30.0))
    # DDGS documents Auto as its most reliable choice because it can vary the
    # upstream provider. Keep named providers as deterministic fallbacks.
    candidates = (
        [backend]
        if backend != "auto"
        else ["auto", "duckduckgo", "bing", "brave"]
    )
    deadline = time.monotonic() + total_timeout
    results: list[dict[str, str]] = []
    for index, candidate in enumerate(candidates):
        remaining = deadline - time.monotonic()
        remaining_attempts = len(candidates) - index
        if remaining < 3:
            break
        if candidate == "auto":
            # Leave at least three seconds for every explicit fallback.
            attempt_timeout = max(
                3.0,
                min(8.0, remaining - (remaining_attempts - 1) * 3.0),
            )
        else:
            attempt_timeout = max(3.0, min(8.0, remaining / remaining_attempts))
        results = await _run_worker(query, max_results, candidate, attempt_timeout)
        if results:
            break
    if not results:
        raise WebSearchError("ค้นเว็บไม่สำเร็จหรือไม่พบข้อมูล กรุณาลองใหม่")
    _CACHE[cache_key] = (time.monotonic() + _CACHE_TTL_SECONDS, results)
    if len(_CACHE) > 128:
        now = time.monotonic()
        for key in list(_CACHE):
            if _CACHE[key][0] < now:
                del _CACHE[key]
    return [dict(item) for item in results]


def _worker_main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read(8192).decode("utf-8"))
        query = clean_text(request.get("query"), 500)
        count = max(1, min(int(request.get("max_results", 5)), 8))
        backend = str(request.get("backend") or "auto")
        if backend not in _ALLOWED_BACKENDS:
            backend = "auto"
        if not query:
            raise ValueError("empty query")
        from ddgs import DDGS

        proxies = urllib.request.getproxies()
        proxy = proxies.get("https") or proxies.get("http")
        kwargs: dict[str, Any] = {"safesearch": "moderate", "max_results": count}
        if backend != "auto":
            kwargs["backend"] = backend
        with DDGS(proxy=proxy, timeout=8) as client:
            client.threads = 1
            raw = client.text(query, **kwargs) or []
        output = {"results": clean_results(raw, count)}
        sys.stdout.write(json.dumps(output, ensure_ascii=False))
        return 0
    except Exception as exc:
        sys.stderr.write(type(exc).__name__)
        return 2


if __name__ == "__main__":
    if "--worker" not in sys.argv:
        raise SystemExit(2)
    raise SystemExit(_worker_main())
