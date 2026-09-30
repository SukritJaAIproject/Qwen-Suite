"""Partial edits of existing text files, and opening files in VS Code (added 2026-09-30).

Rewriting a 200-line file costs ~2,500 output tokens - 20-30 minutes at qwen3.8's ~1.4 tok/s
on this CPU - while an edit is 100-200 tokens. So the model sends only {old_text, new_text}
pairs. Matching is exact: applying at a guessed location is worse than an error. Because
every retry costs the model minutes, a failure says precisely what to fix (closest lines,
line-number prefixes copied from read_text_file, whitespace, Thai character forms).

All edits are applied in memory and written once through a temp file + os.replace, or not
at all. The encoding (the same utf-8-sig/utf-8/cp874/cp1252 chain read_text_file uses), a
UTF-8 BOM and CRLF line endings are kept. The previous bytes go to file_history first.
"""
from __future__ import annotations

import codecs
import difflib
import json
import os
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Callable

import file_history

MAX_TEXT_BYTES = 2 * 1024 * 1024  # same limit as server.read_text_file / write_text_file
MAX_EDITS = 50
DIFF_LIMIT = 3000
ENCODINGS = ("utf-8-sig", "utf-8", "cp874", "cp1252")
# First candidate of server._programs()["code"]; the tool server passes its own resolution.
CODE_EXE = Path(r"C:\Users\EGAT\AppData\Local\Programs\Microsoft VS Code\Code.exe")

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))
_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})
_LINE_PREFIX = re.compile(r"^\s*\d+: ?")
_DONE_NOTHING = "ไฟล์ยังไม่ถูกแก้ (ยกเลิกทุกข้อใน edits ครั้งนี้)"


# ---------------------------------------------------------------------------- encoding
def decode_text(raw: bytes) -> tuple[str, str, bool]:
    """-> (text, encoding to write back with, had UTF-8 BOM)."""
    if b"\x00" in raw:
        raise ValueError(
            "ไฟล์นี้มีไบต์ NUL — น่าจะเป็นไฟล์ binary หรือ UTF-16 จึงแก้ด้วย edit_text_file ไม่ได้"
        )
    for encoding in ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if encoding.startswith("utf-8"):
            return text, "utf-8", raw.startswith(codecs.BOM_UTF8)
        return text, encoding, False
    raise ValueError("ไฟล์นี้ไม่ใช่ข้อความที่รองรับ (utf-8, cp874, cp1252)")


def encode_text(text: str, encoding: str, bom: bool) -> bytes:
    try:
        data = text.encode(encoding)
    except UnicodeEncodeError as exc:
        bad = text[exc.start:exc.end]
        raise ValueError(
            f"ไฟล์นี้ใช้ encoding {encoding} ซึ่งเก็บอักขระ {bad!r} (U+{ord(bad[0]):04X}) ไม่ได้ — "
            f"เปลี่ยน new_text ให้ไม่มีอักขระนี้ {_DONE_NOTHING}"
        ) from exc
    return codecs.BOM_UTF8 + data if bom else data


def _newline_style(text: str) -> str:
    crlf = text.count("\r\n")
    lone_lf = text.count("\n") - crlf
    lone_cr = text.count("\r") - crlf
    if crlf and not lone_lf and not lone_cr:
        return "crlf"
    if not crlf and not lone_cr:
        return "lf"
    return "mixed"


def unified_diff_text(before: str, after: str, name: str, limit: int = DIFF_LIMIT) -> tuple[str, bool]:
    a = before.replace("\r\n", "\n").split("\n")
    b = after.replace("\r\n", "\n").split("\n")
    diff = "\n".join(difflib.unified_diff(a, b, f"a/{name}", f"b/{name}", n=2, lineterm=""))
    if len(diff) <= limit:
        return diff, False
    cut = diff.rfind("\n", 0, limit)
    return diff[: cut if cut > 0 else limit] + f"\n… (diff ยาวเกิน {limit} ตัวอักษร ตัดส่วนที่เหลือ)", True


def _diff_bytes(before: bytes, after: bytes, name: str) -> tuple[str, bool]:
    try:
        return unified_diff_text(decode_text(before)[0], decode_text(after)[0], name)
    except ValueError:
        return "", False


# ------------------------------------------------------------------------------- edits
def _clip(text: str, width: int = 100) -> str:
    text = text.replace("\t", "\\t").replace("\r", "\\r")
    return text if len(text) <= width else text[: width - 1] + "…"


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "1", "yes", "y"}
    return bool(value)


def _normalize_edits(edits: Any) -> list[tuple[str, str, bool]]:
    example = '[{"old_text": "ข้อความเดิม", "new_text": "ข้อความใหม่"}]'
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except json.JSONDecodeError as exc:
            raise ValueError(f"edits ต้องเป็นรายการ JSON เช่น {example} — อ่านไม่ออก: {exc}") from exc
    if isinstance(edits, dict):
        edits = [edits]
    if not isinstance(edits, list) or not edits:
        raise ValueError(f"edits ต้องเป็นรายการอย่างน้อย 1 ข้อ เช่น {example}")
    if len(edits) > MAX_EDITS:
        raise ValueError(f"edits มี {len(edits)} ข้อ เกิน {MAX_EDITS} ข้อต่อครั้ง — แบ่งส่งหลายครั้ง")
    result = []
    for number, item in enumerate(edits, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"edits ข้อที่ {number} ต้องเป็น object ที่มี old_text และ new_text")
        old = next((item[k] for k in ("old_text", "old_string", "old") if k in item), None)
        new = next((item[k] for k in ("new_text", "new_string", "new") if k in item), None)
        if old is None or new is None:
            got = ", ".join(map(str, item)) or "(ว่าง)"
            raise ValueError(f"edits ข้อที่ {number} ต้องมีทั้ง old_text และ new_text (ได้รับ: {got})")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError(f"edits ข้อที่ {number}: old_text และ new_text ต้องเป็นข้อความ")
        if old == "":
            raise ValueError(
                f"edits ข้อที่ {number}: old_text ว่าง — ต้องใส่ข้อความเดิมที่จะเปลี่ยน "
                "(สร้างไฟล์ใหม่ให้ใช้ write_text_file)"
            )
        result.append((old, new, _flag(item.get("replace_all", False))))
    return result


def _clean_sha(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    if cleaned.startswith("sha256:"):
        cleaned = cleaned[7:].strip()
    if cleaned and not re.fullmatch(r"[0-9a-f]{64}", cleaned):
        raise ValueError(
            f"expected_sha256 ต้องเป็น sha256 แบบ hex 64 ตัว (ได้รับ '{_clip(cleaned, 70)}') "
            "หรือเว้นว่างไว้"
        )
    return cleaned


def _loose(text: str) -> str:
    """Spaces/tabs collapsed and every line stripped: whitespace-insensitive form."""
    return "\n".join(re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.split("\n"))


def _unicode_form(text: str) -> str:
    # NFKC folds Thai sara am (U+0E33) and nikhahit + sara aa (U+0E4D U+0E32) together
    return unicodedata.normalize("NFKC", text.translate(_ZERO_WIDTH))


def _diagnose(work: str, old: str) -> str:
    old_lines = old.split("\n")
    body = [line for line in old_lines if line.strip()]
    if body and all(_LINE_PREFIX.match(line) for line in body):
        unprefixed = "\n".join(_LINE_PREFIX.sub("", line, count=1) for line in old_lines)
        if unprefixed in work or _loose(unprefixed) in _loose(work):
            return (
                "old_text มีเลขบรรทัด 'N: ' ที่ติดมาจาก read_text_file — "
                "ตัดเลขบรรทัดออกทั้งใน old_text และ new_text"
            )
    if _loose(old) in _loose(work):
        return "ช่องว่างหรือย่อหน้าไม่ตรง (เช่น tab กับ space หรือช่องว่างท้ายบรรทัด) — คัดลอกให้ตรงทุกตัว"
    if re.sub(r"\s+", " ", old).strip() in re.sub(r"\s+", " ", work):
        return "การขึ้นบรรทัดไม่ตรง (ข้อความเหมือนกันแต่ตัดบรรทัดคนละที่หรือมีบรรทัดว่างต่างกัน)"
    if _loose(_unicode_form(old)) in _loose(_unicode_form(work)):
        return (
            "อักขระไทยหรือ Unicode ต่างรูปกัน (เช่น 'ำ' กับ 'ํา' หรือมีอักขระล่องหน zero-width) — "
            "คัดลอกข้อความจากไฟล์ตรง ๆ"
        )
    if _loose(old.translate(_QUOTES)) in _loose(work.translate(_QUOTES)):
        return "เครื่องหมายคำพูดต่างชนิด (“ ” ‘ ’ กับ \" ') — ใช้แบบเดียวกับในไฟล์"
    if _loose(old).casefold() in _loose(work).casefold():
        return "ตัวพิมพ์เล็ก/ใหญ่ไม่ตรง"
    return ""


def _near_lines(work: str, old: str, limit: int = 3) -> list[str]:
    lines = work.split("\n")
    stripped = [line.strip() for line in lines]
    old_lines = old.split("\n")
    old_stripped = [line.strip() for line in old_lines]
    first = next((i for i, line in enumerate(old_stripped) if line), None)
    if first is None:
        return []
    probe = old_stripped[first]
    hints = []
    for start in [i for i, line in enumerate(stripped) if line == probe][:limit]:
        for offset in range(1, len(old_stripped) - first):
            position = start + offset
            want = old_stripped[first + offset]
            if position >= len(lines):
                hints.append(
                    f"บรรทัด {start + 1} ตรงกับบรรทัดแรกของ old_text แต่ไฟล์จบก่อนถึงบรรทัดที่ "
                    f"{first + offset + 1} ของ old_text"
                )
                break
            if stripped[position] != want:
                hints.append(
                    f'บรรทัด {start + 1} ตรงกับบรรทัดแรกของ old_text แต่บรรทัด {position + 1} ในไฟล์คือ '
                    f'"{_clip(lines[position])}" ส่วน old_text เขียนว่า "{_clip(old_lines[first + offset])}"'
                )
                break
        else:
            end = start + len(old_stripped) - first
            hints.append(f"บรรทัด {start + 1}-{end} ตรงกันถ้าไม่นับช่องว่างหัว/ท้ายบรรทัด")
    if hints:
        return hints
    candidates = list(dict.fromkeys(line for line in stripped if line))[:50000]
    for match in difflib.get_close_matches(probe, candidates, n=limit, cutoff=0.6):
        index = stripped.index(match)
        hints.append(f'{index + 1}: "{_clip(lines[index])}"')
    return hints


def _not_found(work: str, old: str, number: int, total: int) -> str:
    first = next((line.strip() for line in old.split("\n") if line.strip()), old)
    parts = [
        f"edits ข้อที่ {number}/{total}: ไม่พบ old_text ในไฟล์ "
        "(ต้องตรงทุกตัวอักษร รวมช่องว่าง ย่อหน้า และการขึ้นบรรทัด)",
        f'old_text บรรทัดแรก: "{_clip(first)}"',
    ]
    cause = _diagnose(work, old)
    if cause:
        parts.append(f"สาเหตุที่น่าจะเป็น: {cause}")
    near = _near_lines(work, old)
    if near:
        parts.append("บรรทัดที่ใกล้เคียงในไฟล์: " + " | ".join(near))
    parts.append(
        "วิธีแก้: อ่านช่วงบรรทัดนั้นด้วย read_text_file แล้วคัดลอกข้อความเดิมมาให้ตรงทุกตัว "
        "(ไม่ต้องใส่เลขบรรทัด 'N: ')"
    )
    if number > 1:
        parts.append(f"หมายเหตุ: ข้อที่ {number} ค้นในข้อความที่แก้ข้อก่อนหน้าแล้ว")
    parts.append(_DONE_NOTHING)
    return "\n".join(parts)


def _ambiguous(work: str, old: str, number: int, total: int, hits: int) -> str:
    lines: list[int] = []
    start = 0
    while len(lines) < 5:
        index = work.find(old, start)
        if index < 0:
            break
        lines.append(work.count("\n", 0, index) + 1)
        start = index + len(old)
    more = " …" if hits > len(lines) else ""
    return (
        f"edits ข้อที่ {number}/{total}: พบ old_text {hits} ตำแหน่ง (บรรทัด {', '.join(map(str, lines))}{more}) "
        "ให้ใส่บรรทัดรอบ ๆ เพิ่มใน old_text ให้เหลือตำแหน่งเดียว "
        'หรือใส่ "replace_all": true ถ้าต้องการแก้ทุกตำแหน่ง\n' + _DONE_NOTHING
    )


def apply_edits(
    path: str | Path,
    edits: Any,
    expected_sha256: str = "",
    *,
    action: str = "edit_text_file",
    source: str | None = None,
    principal: str | None = None,
    history: bool = True,
    max_bytes: int = MAX_TEXT_BYTES,
) -> dict[str, Any]:
    """Apply [{old_text, new_text, replace_all?}] to an existing file, all or nothing.

    The caller has already passed the path through the server's path guard."""
    target = Path(path)
    if not target.is_file():
        raise ValueError(
            f"ไม่พบไฟล์ หรือไม่ใช่ไฟล์: {target} — edit_text_file แก้ได้เฉพาะไฟล์ที่มีอยู่แล้ว "
            "ไฟล์ใหม่ให้ใช้ write_text_file"
        )
    size = target.stat().st_size
    if size > max_bytes:
        raise ValueError(f"ไฟล์ใหญ่ {size:,} ไบต์ เกิน {max_bytes:,} ไบต์ที่แก้ด้วย edit_text_file ได้")
    items = _normalize_edits(edits)
    expected = _clean_sha(expected_sha256)
    raw = target.read_bytes()
    sha_before = file_history.sha256_bytes(raw)
    if expected and expected != sha_before:
        raise ValueError(
            f"ไฟล์เปลี่ยนไปจากที่คาด (expected_sha256 {expected[:12]}… แต่ตอนนี้เป็น {sha_before[:12]}…) — "
            f"อ่านไฟล์ใหม่ด้วย read_text_file แล้วส่ง edits ตามเนื้อหาปัจจุบัน {_DONE_NOTHING}"
        )
    text, encoding, bom = decode_text(raw)
    style = _newline_style(text)
    # CRLF files are matched in LF form (models send \n) and converted back on write. Mixed
    # files cannot be normalised losslessly, so they are matched as-is with a CRLF retry.
    work = text.replace("\r\n", "\n") if style == "crlf" else text
    replacements: list[int] = []
    for number, (old, new, replace_all) in enumerate(items, start=1):
        if style != "mixed":
            old = old.replace("\r\n", "\n")
            new = new.replace("\r\n", "\n")
        hits = work.count(old)
        if not hits and style == "mixed" and "\n" in old and "\r\n" not in old:
            crlf_old = old.replace("\n", "\r\n")
            if crlf_old in work:
                old = crlf_old
                new = new.replace("\r\n", "\n").replace("\n", "\r\n")
                hits = work.count(old)
        if not hits:
            raise ValueError(_not_found(work, old, number, len(items)))
        if hits > 1 and not replace_all:
            raise ValueError(_ambiguous(work, old, number, len(items), hits))
        work = work.replace(old, new) if replace_all else work.replace(old, new, 1)
        replacements.append(hits if replace_all else 1)
    new_text = work.replace("\n", "\r\n") if style == "crlf" else work
    if new_text == text:
        return {
            "status": "unchanged",
            "path": str(target),
            "sha256": sha_before,
            "message": "edits ไม่ได้ทำให้ไฟล์เปลี่ยน (new_text เหมือนข้อความเดิม) จึงไม่ได้เขียนไฟล์",
        }
    data = encode_text(new_text, encoding, bom)
    if len(data) > max_bytes:
        raise ValueError(f"ผลลัพธ์ใหญ่ {len(data):,} ไบต์ เกิน {max_bytes:,} ไบต์ {_DONE_NOTHING}")
    sha_after = file_history.sha256_bytes(data)
    diff, truncated = unified_diff_text(text, new_text, target.name)
    # Narrow the window in which another writer's change could be overwritten.
    if file_history.sha256_file(target) != sha_before:
        raise ValueError(
            f"ไฟล์ถูกแก้จากที่อื่นระหว่างที่กำลังแก้ — อ่านไฟล์ใหม่แล้วส่ง edits อีกครั้ง {_DONE_NOTHING}"
        )
    change_id = file_history.backup(target, source, principal, action, data=raw) if history else None
    try:
        file_history.atomic_write_bytes(target, data)
    except Exception as exc:
        if change_id:
            file_history.mark_failed(change_id, str(exc))
        raise
    if change_id:
        file_history.record_after(change_id, sha_after=sha_after, size_after=len(data))
    return {
        "status": "edited",
        "path": str(target),
        "change_id": change_id,
        "sha256": sha_after,
        "sha256_before": sha_before,
        "replacements": replacements,
        "encoding": encoding + ("+bom" if bom else ""),
        "newline": style,
        "bytes": len(data),
        "diff": diff,
        "diff_truncated": truncated,
    }


def revert_change(
    change_id: str,
    *,
    path_check: Callable[[Path], Any] | None = None,
    source: str | None = None,
    principal: str | None = None,
) -> dict[str, Any]:
    """file_history.revert with a text diff of what the revert changed."""
    return file_history.revert(
        change_id, path_check=path_check, source=source, principal=principal, make_diff=_diff_bytes
    )


def change_diff(change_id: str, *, path_check: Callable[[Path], Any] | None = None) -> dict[str, Any]:
    """Diff from a change's backup to the file as it is now (for a change-list UI)."""
    entry = file_history.get_change(change_id)
    target = Path(str(entry.get("path", "")))
    if path_check is not None:
        checked = path_check(target)  # raises for a blocked path; may return the resolved one
        if checked is not None:
            target = Path(checked)
    before = file_history.backup_path(entry).read_bytes()
    current = target.read_bytes() if target.is_file() else b""
    diff, truncated = _diff_bytes(before, current, target.name)
    return {"change_id": entry["id"], "path": str(target), "diff": diff, "diff_truncated": truncated}


# ------------------------------------------------------------------------------ editor
def find_code() -> str | None:
    """Same candidates, same order as server._programs()["code"]."""
    for candidate in (str(CODE_EXE), shutil.which("code")):
        if candidate and Path(candidate).exists():
            return str(Path(candidate).resolve())
    return None


def open_in_editor(
    path: str | Path,
    line: int = 1,
    *,
    program: str | None = None,
    diff_with: str | Path | None = None,
    column: int = 0,
) -> dict[str, Any]:
    """Open a file in VS Code at a line (code -g file:line), or a diff of diff_with -> path.

    The caller has already passed both paths through the server's path guard."""
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"เปิดได้เฉพาะไฟล์ที่มีอยู่: {target} (เปิดโฟลเดอร์ให้ใช้ launch_program)")
    executable = program or find_code()
    if not executable:
        raise ValueError("ไม่พบ VS Code (Code.exe) ในเครื่องนี้ จึงเปิดไฟล์ไม่ได้")
    exe = Path(executable)
    if exe.suffix.lower() in {".cmd", ".bat"}:
        # bin\code.cmd routes arguments through cmd.exe; Code.exe sits one folder up.
        sibling = exe.parent.parent / "Code.exe"
        if sibling.exists():
            exe = sibling
    try:
        line = max(1, int(line or 1))
    except (TypeError, ValueError):
        line = 1
    if diff_with is not None:
        argv = [str(exe), "--diff", str(diff_with), str(target)]
    else:
        location = f"{target}:{line}" + (f":{int(column)}" if column and int(column) > 0 else "")
        argv = [str(exe), "-g", location]
    if exe.suffix.lower() in {".cmd", ".bat"} and any(ch in arg for arg in argv[1:] for ch in '&|<>^%!"'):
        raise ValueError(
            "ชื่อไฟล์มีอักขระพิเศษของ cmd (& | < > ^ % ! \") และพบ VS Code แค่แบบ code.cmd "
            "จึงไม่เปิด เพื่อกันคำสั่งแทรก"
        )
    # Set when the server itself was started from a VS Code terminal; Code.exe would then run
    # as plain Node and treat -g as a script name.
    env = {key: value for key, value in os.environ.items() if key.upper() != "ELECTRON_RUN_AS_NODE"}
    process = subprocess.Popen(
        argv,
        cwd=str(target.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        env=env,
    )
    result: dict[str, Any] = {
        "status": "launched",
        "command": subprocess.list2cmdline(argv),
        "pid": process.pid,
        "path": str(target),
    }
    if diff_with is None:
        result["line"] = line
    else:
        result["diff_with"] = str(diff_with)
    return result
