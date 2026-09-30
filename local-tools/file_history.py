"""Change history for text files the model edits or overwrites (added 2026-09-30).

Before edit_text_file / project_edit_text_file change a file, and before write_text_file /
project_write_text_file overwrite one, the file's current bytes are copied to
HISTORY_ROOT\\YYYYMMDD\\<change_id>-<name> and a line is appended to HISTORY_ROOT\\index.jsonl.
Until then an overwrite had no backup at all.

revert() puts those bytes back, but only while the file still has the hash the change
produced. If anything wrote to it afterwards, a revert would silently throw that later work
away, so it is refused with the reason instead. The current content is backed up before a
revert, so a revert is itself a change that can be reverted.

The index is append-only JSONL with up to three events per change id - "backup" (before the
write), "after" (the write landed) and "failed" (it did not) - merged when read. A crash
between the backup and the write leaves an entry that is never offered for revert. An
overwrite that raises after it already changed the file (write_text truncates first, so a
full disk leaves a cut-off file) is recorded as "after" with what it left, so its copy stays
listed, revertable and safe from pruning.

Retention: 30 days and 300 MB of copies, oldest dropped first. Pruning only ever deletes
files inside HISTORY_ROOT.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

HISTORY_ROOT = Path(os.environ.get("QWEN_FILE_HISTORY_ROOT", r"E:\qwen-projects\.file-history"))
INDEX_NAME = "index.jsonl"
MAX_AGE_DAYS = 30
MAX_TOTAL_BYTES = 300 * 1024 * 1024
# write_text_file may overwrite a file of any size with <= 2 MB of text. Copying a huge file
# would evict the whole budget, so such a file is overwritten without a copy (and says so).
MAX_BACKUP_BYTES = 25 * 1024 * 1024
# list_changes hashes the current file to say whether a revert would still work.
MAX_HASH_BYTES = 64 * 1024 * 1024
PRUNE_INTERVAL_SECONDS = 3600
# Antivirus and the search indexer briefly lock freshly written files on Windows.
REPLACE_ATTEMPTS = 5
REPLACE_RETRY_DELAY = 0.2

_LOCK = threading.RLock()
_last_prune: float | None = None
_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")

# Who is calling. The gateway sets this per request (Starlette's threadpool copies the
# context into the worker thread), so the tool functions keep their plain signatures.
CALLER: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "file_history_caller", default=("local", "")
)


def set_caller(source: str, principal: str = "") -> contextvars.Token:
    return CALLER.set((str(source or "local")[:40], str(principal or "")[:120]))


def _now() -> datetime:
    return datetime.now().astimezone()


def _path_key(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return (cleaned or "file")[:80]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str | None:
    """Hash of the file, or None if it does not exist."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except FileNotFoundError:
        return None
    return digest.hexdigest()


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Write data to path through a temp file in the same folder and os.replace.

    Readers see the old file or the new one, never a half-written one, and a failure leaves
    the old file untouched.
    """
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, target)
                return
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(REPLACE_RETRY_DELAY)
    except PermissionError as exc:
        raise PermissionError(
            f"เขียนไฟล์ไม่ได้: {target} — ไฟล์อาจเปิดค้างอยู่ในโปรแกรมอื่นหรือเป็นไฟล์อ่านอย่างเดียว "
            f"ไฟล์เดิมยังไม่ถูกแก้ ({exc})"
        ) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


# ------------------------------------------------------------------------------ index
def _index_path() -> Path:
    return HISTORY_ROOT / INDEX_NAME


def _append(record: dict[str, Any]) -> None:
    with _LOCK:
        HISTORY_ROOT.mkdir(parents=True, exist_ok=True)
        with open(_index_path(), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_records() -> list[dict[str, Any]]:
    path = _index_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and _ID_RE.match(str(row.get("id", ""))):
                rows.append(row)
    return rows


def _merged(rows: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """change_id -> entry, in index (chronological) order."""
    entries: dict[str, dict[str, Any]] = {}
    for row in _read_records() if rows is None else rows:
        change_id = row["id"]
        event = row.get("event")
        if event == "backup":
            entry = dict(row)
            entry.pop("event", None)
            entry.setdefault("sha_after", None)
            entry.setdefault("size_after", None)
            entry["state"] = "pending"
            entries[change_id] = entry
        elif change_id in entries:
            if event == "after":
                entries[change_id].update(
                    sha_after=row.get("sha_after"),
                    size_after=row.get("size_after"),
                    applied_time=row.get("time"),
                    state="applied",
                )
                if row.get("error"):
                    entries[change_id]["write_error"] = row["error"]
            elif event == "failed":
                entries[change_id].update(state="failed", error=row.get("error"))
    return entries


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def _blob(entry: dict[str, Any]) -> Path | None:
    """The entry's backup copy, only if it resolves inside HISTORY_ROOT."""
    relative = entry.get("backup")
    if not relative:
        return None
    root = HISTORY_ROOT.resolve()
    blob = (root / str(relative)).resolve()
    if root not in blob.parents:
        return None
    return blob


# ----------------------------------------------------------------------------- record
def backup(
    path: str | Path,
    source: str | None = None,
    principal: str | None = None,
    action: str = "",
    *,
    data: bytes | None = None,
    reverts: str | None = None,
) -> str:
    """Copy the file's current bytes (or `data`, the bytes the caller already read) into the
    history and return the new change_id. Raises instead of letting the caller write
    without a backup."""
    target = Path(path)
    try:
        inside = HISTORY_ROOT.resolve() in target.resolve().parents
    except OSError:
        inside = False
    if inside:
        # an edit here would rewrite the index under the history's own feet
        raise ValueError(f"ไฟล์ในโฟลเดอร์ประวัติการแก้ไข ({HISTORY_ROOT}) แก้หรือเขียนทับไม่ได้ — ไฟล์ยังไม่ถูกแก้")
    caller_source, caller_principal = CALLER.get()
    now = _now()
    change_id = f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    record: dict[str, Any] = {
        "id": change_id,
        "event": "backup",
        "time": now.isoformat(timespec="seconds"),
        "source": source or caller_source,
        "principal": caller_principal if principal is None else principal,
        "action": action,
        "path": str(target),
        "backup": None,
        "sha_before": None,
        "size_before": None,
    }
    if reverts:
        record["reverts"] = reverts
    blob: Path | None = None
    try:
        if data is None:
            size = target.stat().st_size
            if size > MAX_BACKUP_BYTES:
                record["size_before"] = size
                record["backup_skipped"] = (
                    f"ไฟล์เดิมใหญ่ {size:,} ไบต์ เกินขนาดที่สำรองได้ ({MAX_BACKUP_BYTES:,} ไบต์)"
                )
            else:
                data = target.read_bytes()
        if data is not None:
            relative = f"{now:%Y%m%d}/{change_id}-{_safe_name(target.name)}"
            blob = HISTORY_ROOT / relative
            blob.parent.mkdir(parents=True, exist_ok=True)
            with open(blob, "xb") as handle:
                handle.write(data)
            record.update(backup=relative, sha_before=sha256_bytes(data), size_before=len(data))
        _append(record)
    except OSError as exc:
        if blob is not None:
            try:
                blob.unlink(missing_ok=True)
            except OSError:
                pass
        raise RuntimeError(f"สำรองไฟล์เดิมไม่สำเร็จ จึงยังไม่แก้ไฟล์: {target} ({exc})") from exc
    _maybe_prune()
    return change_id


def record_after(
    change_id: str,
    path: str | Path | None = None,
    *,
    sha_after: str | None = None,
    size_after: int | None = None,
) -> None:
    """Mark a backed-up change as written. Pass the new hash, or the path to hash now.

    Without this call a change is never offered for revert (document_editor.py calls it
    positionally as record_after(change_id, path))."""
    if sha_after is None and path is not None:
        sha_after = sha256_file(path)
        try:
            size_after = Path(path).stat().st_size
        except OSError:
            size_after = None
    _append(
        {
            "id": change_id,
            "event": "after",
            "time": _now().isoformat(timespec="seconds"),
            "sha_after": sha_after,
            "size_after": size_after,
        }
    )


def mark_failed(change_id: str, error: str = "") -> None:
    try:
        _append(
            {
                "id": change_id,
                "event": "failed",
                "time": _now().isoformat(timespec="seconds"),
                "error": str(error)[:300],
            }
        )
    except OSError:
        pass


def _keep_partial_write(change_id: str, target: Path, error: str) -> bool:
    """After an in-place write raised: if the file no longer has the backed-up bytes, the write
    got part of the way. Record the change as written, with what the file holds now, so the
    copy is listed, can be reverted and is not pruned as failed. Otherwise (file untouched, or
    no copy to keep) mark it failed and return False."""
    try:
        entry = _merged().get(change_id) or {}
        if entry.get("backup") and entry.get("sha_before"):
            try:
                current = sha256_file(target)
                size = target.stat().st_size if current is not None else None
            except OSError:
                current, size = None, None  # unreadable now: keep the copy rather than drop it
            if current != entry["sha_before"]:
                _append(
                    {
                        "id": change_id,
                        "event": "after",
                        "time": _now().isoformat(timespec="seconds"),
                        "sha_after": current,
                        "size_after": size,
                        "error": str(error)[:300],
                    }
                )
                return True
    except OSError:
        pass
    mark_failed(change_id, error)
    return False


class _RecordedWrite:
    change_id: str | None = None


@contextlib.contextmanager
def recorded_write(
    path: str | Path,
    action: str,
    source: str | None = None,
    principal: str | None = None,
) -> Iterator[_RecordedWrite]:
    """Wrap an in-place overwrite: back up first if the file exists, then record the result.

        with file_history.recorded_write(file_path, "write_text_file") as change:
            file_path.write_text(content, encoding="utf-8")
        change.change_id  # None when the file was new

    The write need not be atomic: if it raises after changing the file (write_text truncates
    first, so a full disk leaves a cut-off file), the change stays revertable and the error
    names its change_id.
    """
    handle = _RecordedWrite()
    target = Path(path)
    if target.is_file():
        handle.change_id = backup(target, source, principal, action)
    try:
        yield handle
    except BaseException as exc:
        if handle.change_id and _keep_partial_write(handle.change_id, target, str(exc)):
            if isinstance(exc, Exception):
                message = (
                    f"เขียนไฟล์ไม่สำเร็จกลางทาง ({exc}) ไฟล์ {target} อาจเหลือเนื้อหาไม่ครบ — "
                    f"เนื้อหาเดิมสำรองไว้แล้ว ย้อนกลับด้วย revert_file_change change_id={handle.change_id} "
                    "(ถ้าดิสก์เต็ม ให้เคลียร์พื้นที่ก่อน)"
                )
                raise (OSError if isinstance(exc, OSError) else RuntimeError)(message) from exc
        raise
    if handle.change_id:
        record_after(handle.change_id, path=target)


# ------------------------------------------------------------------------------- read
def get_change(change_id: str) -> dict[str, Any]:
    cleaned = str(change_id or "").strip()
    if not _ID_RE.match(cleaned):
        raise ValueError(
            f"change_id ไม่ถูกต้อง: '{cleaned[:40]}' — ใช้รหัสจาก list_file_changes "
            "(รูปแบบ YYYYMMDD-HHMMSS-xxxxxx)"
        )
    entry = _merged().get(cleaned)
    if not entry:
        raise FileNotFoundError(
            f"ไม่พบ change_id {cleaned} (อาจถูกลบตามอายุ {MAX_AGE_DAYS} วันหรือพื้นที่ "
            f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB แล้ว) — ดูรายการด้วย list_file_changes"
        )
    return entry


def backup_path(entry: dict[str, Any]) -> Path:
    """The existing backup copy of a change (validated to be inside HISTORY_ROOT)."""
    if not entry.get("backup"):
        reason = entry.get("backup_skipped") or "ไม่ได้เก็บสำเนาไว้"
        raise ValueError(f"การเปลี่ยนแปลง {entry.get('id')} ไม่มีสำเนาไฟล์เดิม: {reason}")
    blob = _blob(entry)
    if blob is None:
        raise ValueError(f"ตำแหน่งสำเนาของ {entry.get('id')} ใน index ไม่ถูกต้อง จึงไม่ใช้")
    if not blob.is_file():
        raise FileNotFoundError(f"สำเนาของ {entry.get('id')} ถูกลบไปแล้ว (เกินอายุหรือพื้นที่เก็บ)")
    return blob


def _current_sha(path: str) -> str | None:
    try:
        if Path(path).stat().st_size > MAX_HASH_BYTES:
            return "unknown"
    except OSError:
        return None
    try:
        return sha256_file(path)
    except OSError:
        return "unknown"


def list_changes(path: str | Path = "", limit: int = 20) -> list[dict[str, Any]]:
    """Written changes, newest first, optionally for one file."""
    try:
        limit = max(1, min(int(limit or 20), 200))
    except (TypeError, ValueError):
        limit = 20
    entries = [entry for entry in _merged().values() if entry.get("state") == "applied"]
    if str(path).strip():
        key = _path_key(path)
        entries = [entry for entry in entries if _path_key(entry.get("path", "")) == key]
    entries = entries[::-1][:limit]
    current: dict[str, str | None] = {}
    rows = []
    for entry in entries:
        key = _path_key(entry.get("path", ""))
        if key not in current:
            current[key] = _current_sha(str(entry.get("path", "")))
        now_sha = current[key]
        blob = _blob(entry)
        has_backup = bool(blob and blob.is_file())
        row = {
            "change_id": entry["id"],
            "time": entry.get("time"),
            "action": entry.get("action"),
            "source": entry.get("source"),
            "path": entry.get("path"),
            "size_before": entry.get("size_before"),
            "size_after": entry.get("size_after"),
            "sha_before": entry.get("sha_before"),
            "sha_after": entry.get("sha_after"),
            "has_backup": has_backup,
            "revertable_now": has_backup and now_sha is not None and now_sha == entry.get("sha_after"),
            "already_reverted": now_sha is not None and now_sha == entry.get("sha_before"),
        }
        if entry.get("reverts"):
            row["reverts"] = entry["reverts"]
        if entry.get("backup_skipped"):
            row["backup_skipped"] = entry["backup_skipped"]
        if entry.get("write_error"):
            row["write_error"] = entry["write_error"]
        rows.append(row)
    return rows


# ----------------------------------------------------------------------------- revert
def _drift_message(entry: dict[str, Any], current_sha: str | None) -> str:
    change_id = entry["id"]
    path = entry.get("path")
    if current_sha is None:
        return f"ไฟล์ {path} ไม่มีอยู่แล้ว (ถูกลบหรือย้าย) จึงไม่ย้อนการเปลี่ยนแปลง {change_id}"
    ordered = list(_merged().values())
    key = _path_key(path or "")
    position = next((i for i, item in enumerate(ordered) if item["id"] == change_id), len(ordered))
    newer = [
        item
        for item in ordered[position + 1:]
        if item.get("state") == "applied" and _path_key(item.get("path", "")) == key
    ]
    if newer and newer[-1].get("sha_after") == current_sha:
        chain = ", ".join(item["id"] for item in reversed(newer[-10:]))
        return (
            f"หลังการเปลี่ยนแปลง {change_id} ไฟล์ {path} ถูกแก้ต่ออีก {len(newer)} ครั้ง "
            f"ให้ย้อนจากล่าสุดก่อนตามลำดับ: {chain} แล้วค่อยย้อน {change_id} — ไฟล์ยังไม่ถูกแก้"
        )
    after = str(entry.get("sha_after") or "")[:12]
    return (
        f"ไฟล์ {path} ถูกแก้จากที่อื่นหลังการเปลี่ยนแปลง {change_id} (sha256 ตอนนี้ {current_sha[:12]}… "
        f"ไม่ตรงกับ {after}… ที่บันทึกไว้) จึงไม่ย้อน เพื่อไม่ให้งานที่แก้ทีหลังหาย — "
        f"ดูความต่างด้วย open_in_editor change_id={change_id} แล้วแก้เฉพาะจุดด้วย edit_text_file — ไฟล์ยังไม่ถูกแก้"
    )


def revert(
    change_id: str,
    *,
    path_check: Callable[[Path], Any] | None = None,
    source: str | None = None,
    principal: str | None = None,
    make_diff: Callable[[bytes, bytes, str], tuple[str, bool]] | None = None,
) -> dict[str, Any]:
    """Restore the bytes a change replaced. path_check (the server's path guard) runs on the
    target first, since the index is a file on disk and could have been edited."""
    entry = get_change(change_id)
    change_id = entry["id"]
    if entry.get("state") != "applied":
        raise ValueError(f"การเปลี่ยนแปลง {change_id} ไม่ได้ถูกเขียนลงไฟล์สำเร็จ จึงไม่มีอะไรให้ย้อน")
    target = Path(str(entry.get("path", "")))
    if path_check is not None:
        checked = path_check(target)  # raises for a blocked path; may return the resolved one
        if checked is not None:
            target = Path(checked)
    blob = backup_path(entry)
    with _LOCK:
        current = target.read_bytes() if target.is_file() else None
        current_sha = sha256_bytes(current) if current is not None else None
        if current_sha is not None and current_sha == entry.get("sha_before"):
            return {
                "status": "already_reverted",
                "change_id": change_id,
                "path": str(target),
                "sha256": current_sha,
                "message": "ไฟล์เป็นเนื้อหาก่อนการเปลี่ยนแปลงนี้อยู่แล้ว ไม่ได้เขียนอะไร",
            }
        if current_sha is None or current_sha != entry.get("sha_after"):
            raise ValueError(_drift_message(entry, current_sha))
        restored = blob.read_bytes()
        if sha256_bytes(restored) != entry.get("sha_before"):
            raise ValueError(
                f"สำเนาของ {change_id} ไม่ตรงกับ sha256 ที่บันทึกไว้ (ไฟล์สำเนาถูกแก้) จึงไม่ย้อน — ไฟล์ยังไม่ถูกแก้"
            )
        new_id = backup(
            target, source, principal, "revert_file_change", data=current, reverts=change_id
        )
        try:
            atomic_write_bytes(target, restored)
        except Exception as exc:
            mark_failed(new_id, str(exc))
            raise
        record_after(new_id, sha_after=entry.get("sha_before"), size_after=len(restored))
    result: dict[str, Any] = {
        "status": "reverted",
        "reverted_change_id": change_id,
        "change_id": new_id,
        "path": str(target),
        "sha256": entry.get("sha_before"),
        "bytes": len(restored),
        "message": f"ย้อนแล้ว ถ้าจะกลับไปเป็นแบบก่อนย้อน ใช้ revert_file_change กับ change_id {new_id}",
    }
    if make_diff is not None:
        diff, truncated = make_diff(current, restored, target.name)
        result["diff"] = diff
        result["diff_truncated"] = truncated
    return result


# ------------------------------------------------------------------------------ prune
def prune(
    *,
    now: datetime | None = None,
    max_age_days: float | None = None,
    max_total_bytes: int | None = None,
) -> dict[str, Any]:
    """Drop entries older than max_age_days, failed/abandoned ones, then the oldest until the
    copies fit in max_total_bytes. Rewrites the index; deletes only files inside HISTORY_ROOT."""
    global _last_prune
    now = now or _now()
    max_age = MAX_AGE_DAYS if max_age_days is None else max_age_days
    budget = MAX_TOTAL_BYTES if max_total_bytes is None else max_total_bytes
    cutoff = now - timedelta(days=max_age)
    with _LOCK:
        _last_prune = time.monotonic()
        if not HISTORY_ROOT.is_dir():
            return {"removed": 0, "kept": 0, "bytes": 0}
        root = HISTORY_ROOT.resolve()
        rows = _read_records()
        entries = _merged(rows)
        drop: set[str] = set()
        sizes: dict[str, int] = {}
        for change_id, entry in entries.items():
            when = _parse_time(entry.get("time"))
            if when is None or when < cutoff:
                drop.add(change_id)
            elif entry.get("state") == "failed":
                drop.add(change_id)
            elif entry.get("state") == "pending" and when < now - timedelta(days=1):
                drop.add(change_id)
            blob = _blob(entry)
            try:
                sizes[change_id] = blob.stat().st_size if blob else 0
            except OSError:
                sizes[change_id] = 0
        total = sum(size for change_id, size in sizes.items() if change_id not in drop)
        for change_id in entries:  # oldest first
            if total <= budget:
                break
            if change_id not in drop:
                drop.add(change_id)
                total -= sizes.get(change_id, 0)

        for change_id in drop:
            blob = _blob(entries[change_id])
            if blob is not None:
                try:
                    blob.unlink(missing_ok=True)
                except OSError:
                    pass
        # "after"/"failed" lines of an id whose backup line is gone are dropped as well
        kept_rows = [row for row in rows if row["id"] in entries and row["id"] not in drop]
        if len(kept_rows) != len(rows):
            temporary = _index_path().with_suffix(".jsonl.tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                for row in kept_rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            os.replace(temporary, _index_path())

        # copies no entry refers to (a crash between copy and index line) once a day old
        referenced: set[str] = set()
        for change_id, entry in entries.items():
            kept_blob = _blob(entry) if change_id not in drop else None
            if kept_blob is not None:
                referenced.add(_path_key(kept_blob))
        stale = time.time() - 86400
        for folder in root.iterdir():
            if not (folder.is_dir() and re.fullmatch(r"\d{8}", folder.name)):
                continue
            for item in folder.iterdir():
                try:
                    if (
                        item.is_file()
                        and _path_key(item.resolve()) not in referenced
                        and item.stat().st_mtime < stale
                    ):
                        item.unlink()
                except OSError:
                    continue
            try:
                folder.rmdir()  # only succeeds when empty
            except OSError:
                pass
    return {"removed": len(drop), "kept": len(entries) - len(drop), "bytes": total}


def _maybe_prune() -> None:
    if _last_prune is not None and time.monotonic() - _last_prune < PRUNE_INTERVAL_SECONDS:
        return
    try:
        prune()
    except Exception:
        pass  # retention must never break a write
