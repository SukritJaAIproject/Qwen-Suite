"""PDF page tools: merge, split, extract, rotate, AcroForm fields and page images (added 2026-09-30).

Drawing reviews need single sheets on the phone (e.g. sheet 11 of a 40-sheet drawing set, or a
zoomed corner of it), and paperwork needs merged/filled PDFs. Everything runs on PyMuPDF, which
webui-venv already has; no external program is started.

New PDFs and PNGs always go to the project outputs folder under a freshly reserved name
(document_builder._reserve), never over an input. PNGs are also copied into the OpenClaw
workspace so Telegram can attach them (MEDIA: lines).

Thai in a form field: the field's own font (Helvetica in nearly every AcroForm) cannot encode
Thai, so its stored appearance comes out blank on phones and in previews. Such a value is
drawn onto the page instead, shaped, in TH Sarabun New, and that one field stops being a field.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

import document_builder as DB

MAX_INPUT_BYTES = 200 * 1024 * 1024
MAX_INPUTS = 20
MAX_OUTPUT_PAGES = 2000
MAX_RENDER_PAGES = 20
MAX_SPLIT_PARTS = 20
MAX_FIELDS = 500
MIN_DPI, MAX_DPI = 36, 300
MAX_PNG_BYTES = 10 * 1024 * 1024
# an A1 sheet at 300 dpi is ~70 MP (~200 MB of RAM for one pixmap); larger renders get a lower dpi
MAX_PIXELS = 40_000_000
_FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
THAI_FONTS = (_FONTS / "THSarabunNew.ttf", _FONTS / "tahoma.ttf")
OPS = ("merge", "split", "extract_pages", "rotate", "list_form_fields", "fill_form", "render_page")
_ALIASES = {"extract": "extract_pages", "render": "render_page", "fill": "fill_form",
            "list_fields": "list_form_fields", "form_fields": "list_form_fields", "combine": "merge"}


def input_list(inputs: Any) -> list[Any]:
    """inputs as the model sends it: one path, a JSON list, or a list of paths / {path_alias, relative_path}."""
    if isinstance(inputs, str):
        text = inputs.strip()
        if text.startswith("["):
            try:
                inputs = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"inputs ไม่ใช่ JSON list ที่ถูกต้อง: {exc}") from exc
        else:
            inputs = [text] if text else []
    elif isinstance(inputs, dict):
        inputs = [inputs]
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("inputs ต้องมีไฟล์ PDF อย่างน้อย 1 ไฟล์ (path ภายใน path_alias ของโปรเจกต์)")
    if len(inputs) > MAX_INPUTS:
        raise ValueError(f"inputs ได้ไม่เกิน {MAX_INPUTS} ไฟล์ต่อครั้ง")
    for item in inputs:
        if not isinstance(item, (str, dict)) or (isinstance(item, str) and not item.strip()):
            raise ValueError('แต่ละ input ต้องเป็น path หรือ {"path_alias": ..., "relative_path": ...}')
    return inputs


def page_ranges(spec: Any, count: int) -> list[tuple[int, int]]:
    """'1-3,5,8-,last' -> [(1,3),(5,5),(8,count),(count,count)] (1-based, inclusive)."""
    text = str(spec if spec is not None else "").strip().lower().replace(" ", "")
    if text in ("", "all", "ทั้งหมด"):
        return [(1, count)]
    ranges: list[tuple[int, int]] = []
    for part in text.split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d+|last)(-(\d+|last)?)?", part)
        if not m:
            raise ValueError(f"รูปแบบหน้าไม่ถูกต้อง '{spec}' (ตัวอย่าง: 1-3,5,8- หรือ all)")
        a = count if m.group(1) == "last" else int(m.group(1))
        if m.group(2):
            b = count if m.group(3) in (None, "last") else int(m.group(3))
        else:
            b = a
        if not (1 <= a <= count and 1 <= b <= count):
            raise ValueError(f"หน้า {part} อยู่นอกช่วง (ไฟล์มี {count} หน้า)")
        if b < a:
            raise ValueError(f"ช่วงหน้า {part} กลับด้าน ให้เขียนจากน้อยไปมาก")
        ranges.append((a, b))
    if not ranges:
        raise ValueError(f"รูปแบบหน้าไม่ถูกต้อง '{spec}'")
    return ranges


def page_list(spec: Any, count: int) -> list[int]:
    pages: list[int] = []
    for a, b in page_ranges(spec, count):
        pages.extend(range(a, b + 1))
        if len(pages) > MAX_OUTPUT_PAGES:
            raise ValueError(f"เลือกหน้าได้ไม่เกิน {MAX_OUTPUT_PAGES} หน้า")
    return pages


def check_input(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"ไม่พบไฟล์: {path}")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"ไฟล์ {path.name} ใหญ่เกิน {MAX_INPUT_BYTES // (1024 * 1024)} MB")
    with open(path, "rb") as fh:
        head = fh.read(1024)
    if b"%PDF" not in head:
        raise ValueError(f"ไฟล์ {path.name} ไม่ใช่ PDF")


def open_pdf(path: Path):
    import pymupdf

    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:
        raise ValueError(f"เปิดไฟล์ PDF {path.name} ไม่ได้ (ไฟล์อาจเสีย): {exc}") from exc
    if not doc.is_pdf:
        doc.close()
        raise ValueError(f"ไฟล์ {path.name} ไม่ใช่ PDF")
    if doc.needs_pass:
        doc.close()
        raise ValueError(f"ไฟล์ {path.name} ถูกเข้ารหัสด้วยรหัสผ่าน จึงเปิดไม่ได้ ให้ผู้ใช้ปลดรหัสผ่านก่อน")
    return doc


def _has_xfa(doc: Any) -> bool:
    try:
        kind, _value = doc.xref_get_key(doc.pdf_catalog(), "AcroForm/XFA")
        return kind not in ("null", "")
    except Exception:
        return False


def _stem(name: str, fallback: str, suffix: str) -> str:
    name = str(name or "").strip()
    if name.lower().endswith(suffix):
        name = name[: -len(suffix)]
    return DB._safe_name(name, fallback)[:100]


def _save(doc: Any, target: Path) -> None:
    try:
        doc.save(str(target), garbage=3, deflate=True)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _clip(spec: Any, rect: Any):
    import pymupdf

    parts = [p for p in re.split(r"[,\s;]+", str(spec).strip()) if p]
    try:
        values = [float(p) for p in parts]
    except ValueError:
        values = []
    if len(values) != 4:
        raise ValueError("clip ต้องเป็น 'x0,y0,x1,y1' หน่วย point จากมุมซ้ายบนของหน้า หรือสัดส่วน 0-1 เช่น 0.5,0.5,1,1 (ครึ่งล่างขวา)")
    if all(0 <= v <= 1 for v in values):
        x0, y0, x1, y1 = values
        values = [rect.x0 + x0 * rect.width, rect.y0 + y0 * rect.height, rect.x0 + x1 * rect.width, rect.y0 + y1 * rect.height]
    box = pymupdf.Rect(*values) & rect
    if box.is_empty or box.width < 2 or box.height < 2:
        raise ValueError(f"clip อยู่นอกหน้ากระดาษ (หน้ามีขนาด {rect.width:.0f}x{rect.height:.0f} pt)")
    return box


def render_pages(doc: Any, pages: list[int], dpi: int, clip: Any, out_dir: Path, stem: str) -> list[dict[str, Any]]:
    """Render pages to PNG files in out_dir; each gets a Telegram copy."""
    import pymupdf

    out_dir.mkdir(parents=True, exist_ok=True)
    safe = DB._safe_name(stem, "page")[:80]
    results: list[dict[str, Any]] = []
    for number in pages:
        page = doc[number - 1]
        rect = page.rect
        box = _clip(clip, rect) if str(clip or "").strip() else None
        area = box or rect
        zoom = dpi / 72
        note = None
        if area.width * area.height * zoom * zoom > MAX_PIXELS:
            zoom = math.sqrt(MAX_PIXELS / (area.width * area.height))
            note = f"ลดความละเอียดเหลือ {round(zoom * 72)} dpi เพราะหน้าใหญ่"
        for _attempt in range(4):
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=box, alpha=False)
            data = pix.tobytes("png")
            if len(data) <= MAX_PNG_BYTES:
                break
            zoom *= max(0.5, math.sqrt(MAX_PNG_BYTES / len(data)) * 0.9)
            note = f"ลดความละเอียดเหลือ {round(zoom * 72)} dpi เพื่อให้ไฟล์ไม่เกิน 10 MB"
        else:
            raise ValueError(f"ภาพหน้า {number} ใหญ่เกิน 10 MB แม้ลดความละเอียดแล้ว ให้ใช้ clip เลือกบางส่วน")
        target = DB._reserve(out_dir / f"{safe}-p{number}{'-zoom' if box else ''}.png")
        try:
            target.write_bytes(data)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        telegram = DB._telegram_copy(target)
        item: dict[str, Any] = {
            "page": number,
            "path": str(target),
            "width": pix.width,
            "height": pix.height,
            "dpi": round(zoom * 72),
            "page_size_pt": [round(rect.width, 1), round(rect.height, 1)],
            "telegram_attachment": str(telegram) if telegram else None,
        }
        if box:
            item["clip_pt"] = [round(v, 1) for v in (box.x0, box.y0, box.x1, box.y1)]
        if note:
            item["note"] = note
        results.append(item)
    return results


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on", "x", "/", "✓", "✔", "ใช่", "เลือก", "checked"}
    return bool(value)


def _choice_values(widget: Any) -> list[str]:
    out = []
    for item in widget.choice_values or []:
        if isinstance(item, (list, tuple)):
            out.extend(str(x) for x in item)
        else:
            out.append(str(item))
    return out


def _field_info(page_number: int, w: Any) -> dict[str, Any]:
    import pymupdf

    info: dict[str, Any] = {
        "name": w.field_name,
        "type": w.field_type_string,
        "value": w.field_value,
        "page": page_number,
    }
    if w.field_label:
        info["label"] = w.field_label
    if w.field_type in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
        try:
            info["on_value"] = w.on_state()
        except Exception:
            pass
    if w.field_type in (pymupdf.PDF_WIDGET_TYPE_COMBOBOX, pymupdf.PDF_WIDGET_TYPE_LISTBOX):
        info["options"] = _choice_values(w)[:50]
    if w.field_flags & 1:
        info["read_only"] = True
    return info


def list_form_fields(path: Path) -> dict[str, Any]:
    import pymupdf

    check_input(path)
    doc = open_pdf(path)
    try:
        fields: list[dict[str, Any]] = []
        radios: dict[str, dict[str, Any]] = {}
        total = 0
        for page in doc:
            for w in page.widgets():
                if w.field_type == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
                    # one entry per radio group, with its choices, instead of one per button
                    group = radios.get(w.field_name)
                    on = str(w.on_state())
                    if group is None:
                        total += 1
                        group = radios[w.field_name] = {"name": w.field_name, "type": w.field_type_string,
                                                        "value": "Off", "options": [], "page": page.number + 1}
                        if len(fields) < MAX_FIELDS:
                            fields.append(group)
                    if on not in group["options"]:
                        group["options"].append(on)
                    if w.field_value not in (None, False, "Off", ""):
                        group["value"] = on
                    continue
                total += 1
                if len(fields) < MAX_FIELDS:
                    fields.append(_field_info(page.number + 1, w))
        xfa = _has_xfa(doc)
        result: dict[str, Any] = {"status": "ok", "file": str(path), "count": total, "fields": fields}
        if xfa:
            result["xfa"] = True
        if not total:
            result["note"] = ("ฟอร์มนี้เป็นแบบ XFA ซึ่งไม่รองรับการกรอก" if xfa else
                              "ไฟล์นี้ไม่มีช่องกรอก (AcroForm) — PDF สแกนหรือ PDF ทั่วไปจะไม่มีช่องฟอร์ม")
        elif total > MAX_FIELDS:
            result["note"] = f"แสดง {MAX_FIELDS} ช่องแรกจาก {total} ช่อง"
        return result
    finally:
        doc.close()


def _stamp(page: Any, rect: Any, text: str, fontsize: float) -> None:
    """Draw a field value onto the page with TH Sarabun New. insert_htmlbox shapes the text
    (HarfBuzz), so stacked Thai marks such as in นี้ / ผู้ sit correctly; insert_textbox does not."""
    import html

    import pymupdf

    font = next((f for f in THAI_FONTS if f.exists()), None)
    # TH Sarabun looks about 3/4 the size of Helvetica at the same point size
    size = fontsize * 1.3 if fontsize and fontsize > 4 else max(8.0, min(rect.height * 0.9, 24.0))
    face = f"@font-face {{font-family: qwenthai; src: url({font.name});}} " if font else ""
    css = face + f"* {{font-family: {'qwenthai' if font else 'sans-serif'}; font-size: {size:.1f}pt; margin: 0; line-height: 1.1;}}"
    pad = max(0.0, (rect.height - size * 1.25) / 2) if rect.height < size * 2.5 else 1.0
    box = pymupdf.Rect(rect.x0 + 2, rect.y0 + pad, rect.x1 - 1, rect.y1)
    archive = pymupdf.Archive(str(font.parent)) if font else None
    page.insert_htmlbox(box, html.escape(text).replace("\n", "<br>"), css=css, archive=archive)


def _fill(doc: Any, fields: dict[str, Any]) -> tuple[list[str], list[str], dict[str, str]]:
    import pymupdf

    widgets = [(page, w) for page in doc for w in page.widgets()]
    names = {w.field_name for _p, w in widgets}
    if not widgets:
        if _has_xfa(doc):
            raise ValueError("ฟอร์มนี้เป็นแบบ XFA (Adobe LiveCycle) ซึ่งไม่รองรับการกรอก")
        raise ValueError("ไฟล์นี้ไม่มีช่องกรอก (AcroForm) — PDF สแกนต้องพิมพ์ทับหรือกรอกด้วยมือ")
    unknown = [k for k in fields if k not in names]
    if unknown:
        raise ValueError(f"ไม่พบช่องชื่อ {', '.join(map(str, unknown[:10]))} — ช่องที่มี: {', '.join(sorted(map(str, names))[:40])}")
    radio_options: dict[str, list[str]] = {}
    for _p, w in widgets:
        if w.field_type == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
            radio_options.setdefault(w.field_name, []).append(str(w.on_state()))
    for name, options in radio_options.items():
        if name in fields and not isinstance(fields[name], bool) and \
                str(fields[name]).casefold() not in {o.casefold() for o in options}:
            raise ValueError(f"ค่า '{fields[name]}' ไม่ใช่ตัวเลือกของช่อง {name}: {', '.join(options)}")
    notes: list[str] = []
    filled: list[str] = []
    stamped: dict[str, str] = {}
    for page in doc:
        stamps: list[tuple[int, Any, str, float]] = []
        for w in page.widgets():
            if w.field_name not in fields:
                continue
            value = fields[w.field_name]
            kind = w.field_type
            if w.field_flags & 1:
                notes.append(f"ช่อง {w.field_name} เป็นแบบอ่านอย่างเดียว จึงไม่กรอก")
                continue
            # PyMuPDF takes booleans for buttons: the string "Off" is truthy and would tick the box
            if kind == pymupdf.PDF_WIDGET_TYPE_CHECKBOX:
                w.field_value = _truthy(value)
            elif kind == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
                on = str(w.on_state())
                if value is True:
                    chosen = len(radio_options.get(w.field_name, [])) == 1
                else:
                    chosen = str(value).casefold() == on.casefold()
                w.field_value = chosen
            elif kind in (pymupdf.PDF_WIDGET_TYPE_COMBOBOX, pymupdf.PDF_WIDGET_TYPE_LISTBOX):
                options = _choice_values(w)
                text = "" if value is None else str(value)
                editable = kind == pymupdf.PDF_WIDGET_TYPE_COMBOBOX and bool(w.field_flags & (1 << 18))
                if options and text and text not in options and not editable:
                    raise ValueError(f"ค่า '{text}' ไม่อยู่ในตัวเลือกของช่อง {w.field_name}: {', '.join(options[:20])}")
                if kind == pymupdf.PDF_WIDGET_TYPE_COMBOBOX and any(ord(ch) > 255 for ch in text):
                    # MuPDF lays a Thai dropdown value out below the field's clip box: the stored
                    # appearance is blank on phones and previews, so draw it like a Thai text field
                    stamps.append((w.xref, pymupdf.Rect(w.rect), text, float(w.text_fontsize or 0)))
                    stamped[w.field_name] = text
                    if w.field_name not in filled:
                        filled.append(w.field_name)
                    continue
                w.field_value = text
            elif kind == pymupdf.PDF_WIDGET_TYPE_TEXT:
                text = "" if value is None else str(value)
                if w.text_maxlen and len(text) > w.text_maxlen:
                    raise ValueError(f"ช่อง {w.field_name} รับได้ไม่เกิน {w.text_maxlen} ตัวอักษร")
                if any(ord(ch) > 255 for ch in text):
                    # the form's font (Helvetica) cannot encode Thai: its appearance would be
                    # blank in every viewer that draws the stored appearance (phones, previews)
                    stamps.append((w.xref, pymupdf.Rect(w.rect), text, float(w.text_fontsize or 0)))
                    stamped[w.field_name] = text
                    if w.field_name not in filled:
                        filled.append(w.field_name)
                    continue
                w.field_value = text
            else:
                notes.append(f"ช่อง {w.field_name} ({w.field_type_string}) กรอกด้วยเครื่องมือนี้ไม่ได้")
                continue
            w.update()
            if w.field_name not in filled:
                filled.append(w.field_name)
        for xref, rect, text, size in stamps:
            widget = next((w for w in page.widgets() if w.xref == xref), None)
            if widget is not None:
                page.delete_widget(widget)
            _stamp(page, rect, text, size)
    if stamped:
        notes.append("ช่อง " + ", ".join(stamped) + " มีอักษรไทยซึ่งฟอนต์ของฟอร์มแสดงไม่ได้ จึงพิมพ์ข้อความลงบนหน้าเอกสาร"
                     "ด้วย TH Sarabun New แทน (ช่องนี้ไม่เป็นช่องกรอกแล้ว)")
    return filled, notes, stamped


def run(op: str, paths: list[Path], out_dir: Path, pages: str = "", output_name: str = "", angle: int = 90,
        fields: Any = None, dpi: int = 150, clip: str = "", flatten: bool = False) -> dict[str, Any]:
    op = _ALIASES.get((op or "").strip().lower(), (op or "").strip().lower())
    flatten = flatten is True or str(flatten).strip().casefold() in {"1", "true", "yes", "on"}
    if op not in OPS:
        raise ValueError("op ต้องเป็นหนึ่งใน: " + ", ".join(OPS))
    if not paths:
        raise ValueError("inputs ต้องมีไฟล์ PDF อย่างน้อย 1 ไฟล์")
    for path in paths:
        check_input(path)
    if op == "list_form_fields":
        return list_form_fields(paths[0])
    if op != "merge" and len(paths) > 1:
        raise ValueError(f"{op} ทำกับไฟล์เดียว (inputs มี {len(paths)} ไฟล์)")
    started = time.monotonic()
    out_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    outputs: list[dict[str, Any]] = []

    import pymupdf

    if op == "render_page":
        try:
            dpi = int(dpi or 150)
        except (TypeError, ValueError):
            dpi = 150
        if dpi > MAX_DPI or dpi < MIN_DPI:
            notes.append(f"dpi ปรับเป็น {min(max(dpi, MIN_DPI), MAX_DPI)} (ช่วงที่รับ {MIN_DPI}-{MAX_DPI})")
            dpi = min(max(dpi, MIN_DPI), MAX_DPI)
        doc = open_pdf(paths[0])
        try:
            wanted = page_list(pages or "1", doc.page_count)
            if len(wanted) > MAX_RENDER_PAGES:
                raise ValueError(f"แสดงภาพได้ครั้งละไม่เกิน {MAX_RENDER_PAGES} หน้า (ขอมา {len(wanted)} หน้า)")
            images = render_pages(doc, wanted, dpi, clip, out_dir, _stem(output_name, paths[0].stem, ".png"))
            page_count = doc.page_count
        finally:
            doc.close()
        result = {
            "status": "rendered",
            "op": op,
            "file": str(paths[0]),
            "page_count": page_count,
            "images": images,
            "media": [f"MEDIA:{i['telegram_attachment']}" for i in images if i.get("telegram_attachment")],
            "seconds": round(time.monotonic() - started, 2),
            "delivery": "On Telegram, end the reply with the media lines, one per line, exactly as given. Elsewhere tell the user the image paths.",
        }
        if notes:
            result["notes"] = notes
        return result

    if op == "merge":
        if len(paths) < 2:
            raise ValueError("merge ต้องมีไฟล์อย่างน้อย 2 ไฟล์ใน inputs")
        if str(pages or "").strip():
            notes.append("merge รวมทุกหน้า ไม่ได้ใช้ pages (ใช้ extract_pages ก่อนถ้าต้องการบางหน้า)")
        out = pymupdf.open()
        parts = []
        try:
            for path in paths:
                src = open_pdf(path)
                try:
                    if out.page_count + src.page_count > MAX_OUTPUT_PAGES:
                        raise ValueError(f"ไฟล์รวมจะเกิน {MAX_OUTPUT_PAGES} หน้า")
                    out.insert_pdf(src)
                    parts.append({"file": path.name, "pages": src.page_count})
                finally:
                    src.close()
            target = DB._reserve(out_dir / f"{_stem(output_name, paths[0].stem + '-merged', '.pdf')}.pdf")
            _save(out, target)
            outputs.append({"path": str(target), "pages": out.page_count, "parts": parts})
        finally:
            out.close()
    else:
        src = open_pdf(paths[0])
        try:
            count = src.page_count
            base = _stem(output_name, paths[0].stem, ".pdf")
            if op == "split":
                ranges = page_ranges(pages, count) if str(pages or "").strip() else [(n, n) for n in range(1, count + 1)]
                if len(ranges) > MAX_SPLIT_PARTS:
                    raise ValueError(f"แยกได้ครั้งละไม่เกิน {MAX_SPLIT_PARTS} ไฟล์ ให้ระบุ pages เป็นช่วง เช่น 1-10,11-20")
                for a, b in ranges:
                    part = pymupdf.open()
                    try:
                        part.insert_pdf(src, from_page=a - 1, to_page=b - 1)
                        target = DB._reserve(out_dir / f"{base}-p{a}{'' if a == b else f'-{b}'}.pdf")
                        _save(part, target)
                        outputs.append({"path": str(target), "pages": f"{a}" if a == b else f"{a}-{b}"})
                    finally:
                        part.close()
            elif op == "extract_pages":
                if not str(pages or "").strip():
                    raise ValueError("extract_pages ต้องระบุ pages เช่น 1-3,5")
                ranges = page_ranges(pages, count)
                if sum(b - a + 1 for a, b in ranges) > MAX_OUTPUT_PAGES:
                    raise ValueError(f"เลือกได้ไม่เกิน {MAX_OUTPUT_PAGES} หน้า")
                part = pymupdf.open()
                try:
                    for a, b in ranges:
                        part.insert_pdf(src, from_page=a - 1, to_page=b - 1)
                    label = re.sub(r"[^0-9a-z,-]", "", str(pages).lower()).replace(",", "_")[:40]
                    name = base if output_name else f"{base}-p{label}"
                    target = DB._reserve(out_dir / f"{name}.pdf")
                    _save(part, target)
                    outputs.append({"path": str(target), "pages": part.page_count})
                finally:
                    part.close()
            elif op == "rotate":
                try:
                    turn = int(angle) % 360
                except (TypeError, ValueError):
                    turn = -1
                if turn not in (90, 180, 270):
                    raise ValueError("angle ต้องเป็น 90, 180, 270 หรือ -90")
                selected = page_list(pages, count) if str(pages or "").strip() else list(range(1, count + 1))
                for number in selected:
                    page = src[number - 1]
                    page.set_rotation((page.rotation + turn) % 360)
                name = base if output_name else f"{base}-rotated"
                target = DB._reserve(out_dir / f"{name}.pdf")
                _save(src, target)
                outputs.append({"path": str(target), "pages": count, "rotated_pages": len(selected), "angle": turn})
            elif op == "fill_form":
                if isinstance(fields, str):
                    try:
                        fields = json.loads(fields)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"fields ต้องเป็น JSON object: {exc}") from exc
                if not isinstance(fields, dict) or not fields:
                    raise ValueError('fill_form ต้องมี fields: {"ชื่อช่อง": "ค่า"} (ดูชื่อช่องด้วย list_form_fields)')
                if len(fields) > MAX_FIELDS:
                    raise ValueError(f"กรอกได้ไม่เกิน {MAX_FIELDS} ช่องต่อครั้ง")
                filled, fill_notes, stamped = _fill(src, fields)
                notes.extend(fill_notes)
                if _has_xfa(src):
                    # hybrid form: without this Acrobat shows the (unfilled) XFA layer instead
                    src.xref_set_key(src.pdf_catalog(), "AcroForm/XFA", "null")
                    notes.append("ลบชั้น XFA ออก เพื่อให้โปรแกรมอ่าน PDF แสดงค่าที่กรอกใน AcroForm")
                if flatten:
                    if hasattr(src, "bake"):
                        src.bake(annots=False, widgets=True)
                        notes.append("รวมช่องฟอร์มลงในหน้าเอกสารแล้ว (flatten) แก้ค่าในช่องไม่ได้อีก")
                    else:
                        notes.append("PyMuPDF รุ่นนี้ flatten ไม่ได้ ช่องฟอร์มยังแก้ไขได้")
                name = base if output_name else f"{base}-filled"
                target = DB._reserve(out_dir / f"{name}.pdf")
                _save(src, target)
                check = open_pdf(target)
                readback: dict[str, Any] = {}
                try:
                    for page in check:
                        for w in page.widgets():
                            if w.field_name not in filled:
                                continue
                            if w.field_type == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
                                if w.field_value not in (None, False, "Off", ""):
                                    readback[w.field_name] = str(w.on_state())
                                else:
                                    readback.setdefault(w.field_name, "Off")
                            else:
                                readback[w.field_name] = w.field_value
                finally:
                    check.close()
                readback.update(stamped)
                item = {"path": str(target), "pages": count, "filled": readback}
                if stamped:
                    item["stamped_on_page"] = sorted(stamped)
                outputs.append(item)
        finally:
            src.close()

    for item in outputs:
        telegram = DB._telegram_copy(Path(item["path"]))
        item["telegram_attachment"] = str(telegram) if telegram else None
    result: dict[str, Any] = {
        "status": "created",
        "op": op,
        "outputs": outputs,
        "media": [f"MEDIA:{o['telegram_attachment']}" for o in outputs if o.get("telegram_attachment")],
        "seconds": round(time.monotonic() - started, 2),
        "delivery": "Tell the user the output path(s). On Telegram, end the reply with the media lines, one per line, exactly as given.",
    }
    if notes:
        result["notes"] = notes
    return result
