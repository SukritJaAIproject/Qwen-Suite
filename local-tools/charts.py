"""make_chart: a PNG chart for Telegram and documents (added 2026-09-30).

webui-venv has no matplotlib and nothing may be pip-installed, so the drawing runs in
chart_worker.py under the user's Anaconda Python, found like server._programs() does:
anaconda3\\envs\\icare (matplotlib 3.9) first, then anaconda3 base (3.3, whose numpy only
loads with anaconda3\\Library\\bin on PATH). The data is validated here, passed in a temp JSON
file, and the worker gets a timeout; on a timeout the whole process tree is killed.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import document_builder as DB

WORKER = Path(__file__).with_name("chart_worker.py")
FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
FONT_REGULAR = FONTS / "THSarabunNew.ttf"
FONT_BOLD = FONTS / "THSarabunNew Bold.ttf"
# Thai-capable fallbacks if TH Sarabun New is missing
FALLBACK_FONTS = (FONTS / "LeelawUI.ttf", FONTS / "tahoma.ttf")
TIMEOUT = 90
TYPES = ("bar", "line", "pie", "scatter")
_TYPE_ALIASES = {"column": "bar", "bars": "bar", "แท่ง": "bar", "กราฟแท่ง": "bar", "เส้น": "line", "กราฟเส้น": "line",
                 "วงกลม": "pie", "พาย": "pie", "donut": "pie", "กระจาย": "scatter", "points": "scatter"}
MAX_SERIES = 8
MAX_CATEGORIES = 200
MAX_POINTS = 20_000
MAX_PIE_SLICES = 8
MAX_PNG_BYTES = 10 * 1024 * 1024


def _pythons() -> list[tuple[Path, Path | None]]:
    """(python.exe, conda prefix to put on PATH) in the order to try."""
    conda = Path(os.environ.get("USERPROFILE") or Path.home()) / "anaconda3"
    candidates: list[tuple[Path, Path | None]] = []
    override = os.environ.get("QWEN_CHART_PYTHON", "").strip()
    if override:
        candidates.append((Path(override), Path(override).parent))
    if importlib.util.find_spec("matplotlib") is not None:
        candidates.append((Path(sys.executable), None))
    candidates.append((conda / "envs" / "icare" / "python.exe", conda / "envs" / "icare"))
    candidates.append((conda / "python.exe", conda))
    seen: set[str] = set()
    found = []
    for exe, prefix in candidates:
        key = str(exe).casefold()
        if key not in seen and exe.exists():
            seen.add(key)
            found.append((exe, prefix))
    return found


def _env(prefix: Path | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"}}
    if prefix is not None:
        # what "conda activate" adds; without Library\bin numpy's DLLs do not load on anaconda base
        extra = [prefix, prefix / "Library" / "mingw-w64" / "bin", prefix / "Library" / "usr" / "bin",
                 prefix / "Library" / "bin", prefix / "Scripts"]
        env["PATH"] = os.pathsep.join([str(p) for p in extra] + [env.get("PATH", "")])
    env.update({"MPLBACKEND": "Agg", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    return env


def _number(value: Any, where: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{where}: ต้องเป็นตัวเลข")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "").replace("฿", "").replace("บาท", "").replace(" ", "")
        try:
            number = float(text)
        except ValueError:
            raise ValueError(f"{where}: '{value}' ไม่ใช่ตัวเลข") from None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _is_header(row: list[Any], next_row: list[Any] | None) -> bool:
    """Row 1 is a header when a value cell is not a number, or when its value cells are all
    text (years such as "2568", "2569") while the next row has real numbers."""
    for cell in row[1:]:
        try:
            _number(cell, "")
        except ValueError:
            return True
    texts = all(isinstance(c, str) for c in row[1:])
    numbers = next_row is not None and any(isinstance(c, (int, float)) and not isinstance(c, bool) for c in next_row[1:])
    return bool(row[1:]) and texts and numbers


def _category_series(data: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    if "rows" in data:
        rows = data["rows"]
        if not isinstance(rows, list) or not all(isinstance(r, (list, tuple)) for r in rows) or not rows:
            raise ValueError("rows ต้องเป็นตาราง [[หมวด, ค่า1, ค่า2], ...] (แถวแรกเป็นหัวตารางได้)")
        rows = [list(r) for r in rows]
        header = rows[0] if _is_header(rows[0], rows[1] if len(rows) > 1 else None) else None
        body = rows[1:] if header else rows
        width = max(len(r) for r in rows)
        if width < 2 or not body:
            raise ValueError("rows ต้องมีอย่างน้อย 2 คอลัมน์ (หมวด, ค่า) และมีข้อมูล")
        categories = [str(r[0]) for r in body]
        series = [{"name": str(header[j]) if header and j < len(header) else (f"ชุดที่ {j}" if width > 2 else ""),
                   "values": [r[j] if j < len(r) else None for r in body]} for j in range(1, width)]
    else:
        categories = data.get("categories", data.get("labels"))
        series = data.get("series")
        if series is None and "values" in data:
            series = [{"name": str(data.get("name") or ""), "values": data["values"]}]
        if isinstance(series, dict):
            series = [{"name": str(k), "values": v} for k, v in series.items()]
        if not isinstance(categories, list) or not categories:
            raise ValueError('ต้องมี categories (หรือ labels): ["Q1", "Q2", ...]')
        if not isinstance(series, list) or not series:
            raise ValueError('ต้องมี series: [{"name": ..., "values": [...]}] หรือ values: [...]')
        categories = [str(c) for c in categories]
    if len(categories) > MAX_CATEGORIES:
        raise ValueError(f"หมวดได้ไม่เกิน {MAX_CATEGORIES} หมวด")
    if len(series) > MAX_SERIES:
        raise ValueError(f"ชุดข้อมูลได้ไม่เกิน {MAX_SERIES} ชุด (รวมชุดเล็กเป็น 'อื่น ๆ' หรือแยกเป็นหลายกราฟ)")
    clean = []
    for i, s in enumerate(series, 1):
        if not isinstance(s, dict) or not isinstance(s.get("values"), list):
            raise ValueError(f'series ชุดที่ {i} ต้องเป็น {{"name": ..., "values": [...]}}')
        if len(s["values"]) != len(categories):
            raise ValueError(f"series ชุดที่ {i} มี {len(s['values'])} ค่า แต่มี {len(categories)} หมวด (ต้องเท่ากัน)")
        clean.append({"name": str(s.get("name") or "")[:80],
                      "values": [_number(v, f"series {i} ค่าที่ {j}") for j, v in enumerate(s["values"], 1)]})
    return categories, clean


def _scatter_series(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("series")
    if raw is None:
        raw = [{"name": str(data.get("name") or ""), **{k: data[k] for k in ("x", "y", "points") if k in data}}]
    if not isinstance(raw, list) or not raw:
        raise ValueError('scatter ต้องมี series: [{"name": ..., "x": [...], "y": [...]}] หรือ x/y หรือ points [[x, y], ...]')
    if len(raw) > MAX_SERIES:
        raise ValueError(f"ชุดข้อมูลได้ไม่เกิน {MAX_SERIES} ชุด")
    clean = []
    total = 0
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            raise ValueError(f"series ชุดที่ {i} ต้องเป็น object")
        if isinstance(s.get("points"), list):
            pairs = s["points"]
            if not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in pairs):
                raise ValueError(f"points ของชุดที่ {i} ต้องเป็น [[x, y], ...]")
            xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
        else:
            xs, ys = s.get("x"), s.get("y")
        if not isinstance(xs, list) or not isinstance(ys, list) or len(xs) != len(ys) or not xs:
            raise ValueError(f"ชุดที่ {i}: x และ y ต้องเป็นรายการตัวเลขที่ยาวเท่ากัน")
        points = [(_number(x, f"ชุด {i} x"), _number(y, f"ชุด {i} y")) for x, y in zip(xs, ys)]
        points = [(x, y) for x, y in points if x is not None and y is not None]
        total += len(points)
        if total > MAX_POINTS:
            raise ValueError(f"จุดข้อมูลได้ไม่เกิน {MAX_POINTS} จุด")
        clean.append({"name": str(s.get("name") or "")[:80], "x": [p[0] for p in points], "y": [p[1] for p in points]})
    return clean


def normalize(chart_type: Any, data: Any) -> dict[str, Any]:
    kind = str(chart_type or "bar").strip().lower()
    kind = _TYPE_ALIASES.get(kind, kind)
    if kind not in TYPES:
        raise ValueError("type ต้องเป็น bar, line, pie หรือ scatter")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"data ต้องเป็น JSON: {exc}") from exc
    if isinstance(data, list):
        data = {"rows": data}
    if not isinstance(data, dict) or not data:
        raise ValueError('data ต้องเป็น {"categories": [...], "series": [{"name", "values"}]} หรือ {"rows": [[...]]}')
    spec: dict[str, Any] = {"type": kind}
    if kind == "scatter":
        spec["series"] = _scatter_series(data)
        return spec
    categories, series = _category_series(data)
    if kind == "pie":
        if len(series) != 1:
            raise ValueError("กราฟวงกลมใช้ข้อมูลชุดเดียว (values เดียว)")
        pairs = [(c, v or 0.0) for c, v in zip(categories, series[0]["values"])]
        if any(v < 0 for _c, v in pairs):
            raise ValueError("กราฟวงกลมใช้ค่าติดลบไม่ได้")
        if sum(v for _c, v in pairs) <= 0:
            raise ValueError("กราฟวงกลมต้องมีผลรวมมากกว่า 0")
        if len(pairs) > MAX_PIE_SLICES:
            # a 9th colour would repeat a slot: fold the smallest slices into one
            pairs.sort(key=lambda p: -p[1])
            keep = pairs[:MAX_PIE_SLICES - 1]
            keep.append(("อื่น ๆ", sum(v for _c, v in pairs[MAX_PIE_SLICES - 1:])))
            pairs = keep
        categories = [c for c, _v in pairs]
        series = [{"name": series[0]["name"], "values": [v for _c, v in pairs]}]
    spec["categories"] = [c[:60] for c in categories]
    spec["series"] = series
    return spec


def _png_size(path: Path) -> tuple[int, int]:
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("ไฟล์ที่ได้ไม่ใช่ PNG")
    return struct.unpack(">II", head[16:24])


def _kill_tree(proc: subprocess.Popen) -> None:
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def make_chart(out_dir: Path, chart_type: Any, data: Any, title: str = "", output_name: str = "",
               x_label: str = "", y_label: str = "", timeout: int = TIMEOUT) -> dict[str, Any]:
    spec = normalize(chart_type, data)
    spec.update({"title": str(title or "")[:200], "x_label": str(x_label or "")[:120], "y_label": str(y_label or "")[:120],
                 "dpi": 150, "size": [10, 6]})
    pythons = _pythons()
    if not pythons:
        raise RuntimeError("ไม่พบ Python ที่มี matplotlib (anaconda3\\envs\\icare หรือ anaconda3) จึงวาดกราฟไม่ได้")
    font = FONT_REGULAR if FONT_REGULAR.exists() else next((f for f in FALLBACK_FONTS if f.exists()), None)
    spec["font_regular"] = str(font) if font else ""
    spec["font_bold"] = str(FONT_BOLD) if font == FONT_REGULAR and FONT_BOLD.exists() else ""
    out_dir.mkdir(parents=True, exist_ok=True)
    name = str(output_name or title or "").strip()
    if name.lower().endswith(".png"):
        name = name[:-4]
    target = DB._reserve(out_dir / f"{DB._safe_name(name, 'chart-' + time.strftime('%Y%m%d-%H%M%S'))[:100]}.png")
    spec["output"] = str(target)
    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="qwen-chart-"))
    result: dict[str, Any] = {}
    used: Path | None = None
    try:
        spec_path, result_path = work / "spec.json", work / "result.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        failures: list[str] = []
        for exe, prefix in pythons:
            result_path.unlink(missing_ok=True)
            proc = subprocess.Popen([str(exe), "-X", "utf8", str(WORKER), str(spec_path), str(result_path)],
                                    cwd=str(work), env=_env(prefix), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                _out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                raise RuntimeError(f"วาดกราฟนานเกิน {timeout} วินาที จึงหยุด") from None
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result = {}
            if proc.returncode == 0 and result.get("ok"):
                used = exe
                break
            if proc.returncode == 2:
                raise ValueError(f"ข้อมูลกราฟไม่ถูกต้อง: {result.get('error') or 'ไม่ทราบสาเหตุ'}")
            if result.get("error"):
                detail = str(result["error"])
            else:
                lines = err.decode("utf-8", "replace").strip().splitlines()
                detail = lines[-1] if lines else f"exit {proc.returncode}"
            failures.append(f"{exe}: {detail[:300]}")
        else:
            raise RuntimeError("วาดกราฟไม่สำเร็จ: " + " | ".join(failures)[-800:])
        if target.stat().st_size == 0:
            raise RuntimeError("วาดกราฟไม่สำเร็จ (ไฟล์ว่าง)")
        if target.stat().st_size > MAX_PNG_BYTES:
            raise RuntimeError("ไฟล์กราฟใหญ่เกิน 10 MB")
        width, height = _png_size(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    telegram = DB._telegram_copy(target)
    out: dict[str, Any] = {
        "status": "created",
        "type": spec["type"],
        "path": str(target),
        "width": width,
        "height": height,
        "bytes": target.stat().st_size,
        "seconds": round(time.monotonic() - started, 2),
        "python": str(used),
        "font": result.get("font"),
        "telegram_attachment": str(telegram) if telegram else None,
        "media": [f"MEDIA:{telegram}"] if telegram else [],
        "delivery": "Tell the user the path. On Telegram, end the reply with a line of its own: MEDIA:<telegram_attachment>",
    }
    if result.get("warnings"):
        out["warnings"] = result["warnings"]
    if not font:
        out.setdefault("warnings", []).append("ไม่พบฟอนต์ภาษาไทย ตัวอักษรไทยอาจแสดงเป็นกล่อง")
    return out
