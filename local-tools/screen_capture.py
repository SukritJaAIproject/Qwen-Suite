"""capture_screen: a PNG screenshot of the owner's desktop, e.g. to send to Telegram (added 2026-09-30).

local-tools is started at logon in the owner's interactive session (start-local-tools.ps1), so
PIL.ImageGrab sees the real desktop there. A locked workstation or a turned-off display gives a
black image (reported as a warning) or a grab error, never someone else's screen.

The PNG goes to the project's outputs folder under a name that is reserved atomically and never
overwrites anything (document_builder._reserve); a copy goes to the OpenClaw workspace so
Telegram can attach it (MEDIA:<telegram_attachment>). Captures wider than MAX_WIDTH px (all
monitors side by side) are scaled down to keep Telegram uploads small.

Monitors: EnumDisplayMonitors under per-monitor DPI awareness, the same awareness Pillow's grab
uses, so the rectangles are physical pixels in the virtual-desktop coordinates ImageGrab expects.
Numbering: 1 = primary monitor, then the others left to right (then top to bottom).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import re
import time
from pathlib import Path
from typing import Any

import document_builder as DB

MAX_WIDTH = 2560
MAX_PNG_BYTES = 9 * 1024 * 1024  # Telegram photos must stay under 10 MB
MIN_REGION = 8
_DPI_PER_MONITOR_AWARE = -3  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE (what Pillow's grab uses)
_MONITORINFOF_PRIMARY = 1


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", _RECT), ("rcWork", _RECT), ("dwFlags", wt.DWORD),
                ("szDevice", wt.WCHAR * 32)]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, wt.HMONITOR, wt.HDC, ctypes.POINTER(_RECT), wt.LPARAM)


def list_monitors() -> list[dict[str, Any]]:
    """[{number, primary, device, left, top, width, height}] in physical pixels, primary first."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # own instance: argtypes stay local
    user32.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.c_void_p, _MONITORENUMPROC, wt.LPARAM]
    user32.EnumDisplayMonitors.restype = wt.BOOL
    user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(_MONITORINFOEXW)]
    user32.GetMonitorInfoW.restype = wt.BOOL
    set_ctx = getattr(user32, "SetThreadDpiAwarenessContext", None)  # Windows 10 1607+
    if set_ctx is not None:
        set_ctx.argtypes = [ctypes.c_void_p]
        set_ctx.restype = ctypes.c_void_p
    found: list[dict[str, Any]] = []

    def callback(hmon, _hdc, _rect, _data):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append({"primary": bool(info.dwFlags & _MONITORINFOF_PRIMARY), "device": info.szDevice,
                          "left": r.left, "top": r.top, "width": r.right - r.left, "height": r.bottom - r.top})
        return 1

    previous = set_ctx(ctypes.c_void_p(_DPI_PER_MONITOR_AWARE)) if set_ctx is not None else None
    try:
        user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(callback), 0)
    finally:
        if set_ctx is not None and previous:
            set_ctx(ctypes.c_void_p(previous))
    found.sort(key=lambda m: (not m["primary"], m["left"], m["top"]))
    for number, mon in enumerate(found, 1):
        mon["number"] = number
    return found


def _pick_monitor(monitor: Any, monitors: list[dict[str, Any]]) -> dict[str, Any] | None:
    """None = the whole virtual desktop (all monitors)."""
    text = str(monitor if monitor is not None else "all").strip().lower()
    if text in ("", "all", "0", "ทั้งหมด", "ทุกจอ"):
        return None
    if text in ("primary", "main", "จอหลัก"):
        text = "1"
    text = text.removeprefix("monitor").removeprefix("จอ").strip()
    if not text.isdigit():
        raise ValueError("monitor must be 'all', 'primary' or a monitor number (1 = primary)")
    number = int(text)
    for mon in monitors:
        if mon["number"] == number:
            return mon
    raise ValueError(f"monitor {number} does not exist; this computer has {len(monitors)} monitor(s): "
                     + ", ".join(f"{m['number']}={m['width']}x{m['height']}{' primary' if m['primary'] else ''}"
                                 for m in monitors))


def _parse_region(region: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
    """'x0,y0,x1,y1' in pixels of the chosen capture, or 0-1 fractions ('0.5,0,1,0.5' = top-right quarter)."""
    if region is None or (isinstance(region, str) and not region.strip()):
        return None
    if isinstance(region, (list, tuple)):
        parts = [str(v) for v in region]
    else:
        parts = [p for p in re.split(r"[,\s]+", str(region).strip()) if p]
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise ValueError("region must be four numbers 'x0,y0,x1,y1' (pixels, or 0-1 fractions)") from None
    if len(values) != 4:
        raise ValueError("region must be four numbers 'x0,y0,x1,y1' (pixels, or 0-1 fractions)")
    if all(0.0 <= v <= 1.0 for v in values):
        values = [values[0] * width, values[1] * height, values[2] * width, values[3] * height]
    x0, y0, x1, y1 = (int(round(v)) for v in values)
    x0, x1 = max(0, min(x0, width)), max(0, min(x1, width))
    y0, y1 = max(0, min(y0, height)), max(0, min(y1, height))
    if x1 - x0 < MIN_REGION or y1 - y0 < MIN_REGION:
        raise ValueError(f"region is empty or outside the {width}x{height} capture (x1>x0, y1>y0 needed)")
    return x0, y0, x1, y1


def _grab(bbox: tuple[int, int, int, int] | None):
    from PIL import ImageGrab

    try:
        return ImageGrab.grab(bbox=bbox, all_screens=True)
    except OSError as exc:
        raise RuntimeError("จับภาพหน้าจอไม่ได้ (หน้าจออาจล็อกอยู่ หรือ local-tools ไม่ได้รันใน session ของผู้ใช้): "
                           f"{exc}") from None


def _scale(image, max_width: int):
    from PIL import Image

    if image.width <= max_width:
        return image
    height = max(1, round(image.height * max_width / image.width))
    return image.resize((max_width, height), Image.LANCZOS)


TELEGRAM_COPY_DAYS = 7


def _prune_telegram_copies() -> None:
    """Screenshots copied for Telegram are only needed until the bot has sent them."""
    folder = getattr(DB, "TELEGRAM_OUTPUTS", None)
    if not folder or not Path(folder).is_dir():
        return
    cutoff = time.time() - TELEGRAM_COPY_DAYS * 86400
    for old in Path(folder).glob("screen-*.png"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def capture(out_dir: Path, monitor: Any = "all", region: Any = "", output_name: str = "",
            telegram_copy: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    monitors = list_monitors()
    chosen = _pick_monitor(monitor, monitors)
    bbox = None
    if chosen is not None:
        bbox = (chosen["left"], chosen["top"], chosen["left"] + chosen["width"], chosen["top"] + chosen["height"])
    image = _grab(bbox)
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")
    screen_size = image.size
    box = _parse_region(region, image.width, image.height)
    if box:
        image = image.crop(box)
    captured_size = image.size
    warnings: list[str] = []
    if image.getbbox() is None:
        warnings.append("ภาพเป็นสีดำทั้งหมด: หน้าจออาจล็อกอยู่หรือจอปิด")
    image = _scale(image, MAX_WIDTH)

    out_dir.mkdir(parents=True, exist_ok=True)
    name = str(output_name or "").strip()
    if name.lower().endswith(".png"):
        name = name[:-4]
    suffix = "" if chosen is None else f"-mon{chosen['number']}"
    fallback = f"screen-{time.strftime('%Y%m%d-%H%M%S')}{suffix}"
    target = DB._reserve(out_dir / f"{DB._safe_name(name, fallback)[:100]}.png")
    try:
        image.save(target, "PNG")
        while target.stat().st_size > MAX_PNG_BYTES and image.width > 800:
            image = _scale(image, int(image.width * 0.75))
            image.save(target, "PNG")
        if target.stat().st_size == 0:
            raise RuntimeError("บันทึกภาพหน้าจอไม่สำเร็จ (ไฟล์ว่าง)")
    except BaseException:
        target.unlink(missing_ok=True)  # only our own reserved placeholder
        raise
    telegram = None
    if telegram_copy:
        _prune_telegram_copies()
        telegram = DB._telegram_copy(target)
    result: dict[str, Any] = {
        "status": "captured",
        "path": str(target),
        "monitor": "all" if chosen is None else chosen["number"],
        "monitors": [{k: m[k] for k in ("number", "primary", "width", "height", "left", "top")} for m in monitors],
        "screen_size": list(screen_size),
        "captured_size": list(captured_size),
        "width": image.width,
        "height": image.height,
        "scaled": image.size != captured_size,
        "bytes": target.stat().st_size,
        "seconds": round(time.monotonic() - started, 2),
        "telegram_attachment": str(telegram) if telegram else None,
        "media": [f"MEDIA:{telegram}"] if telegram else [],
        "delivery": ("Tell the user the path. On Telegram, end the reply with a line of its own: "
                     "MEDIA:<telegram_attachment>. The screenshot may show private work: send it only to the "
                     "owner who asked, never to anyone else."),
    }
    if box:
        result["region"] = list(box)
    if warnings:
        result["warnings"] = warnings
    return result
