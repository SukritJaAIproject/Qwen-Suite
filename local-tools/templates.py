"""House templates for make_document (added 2026-09-30).

Templates live in E:\\qwen-projects\\_templates with an index, templates.json:
[{name, file, kind, placeholders, description, builtin, created_at}]. A user saves one from a
file they supply (.docx/.dotx/.pptx/.potx/.xlsx/.xltx; macro formats are refused), e.g. a memo
with the office letterhead. Letterheads and logos only ever come from the user's own files;
nothing here draws EGAT branding.

Two generic Thai templates are built by code on first use, so the installer ships no binaries:
  memo-th    บันทึกข้อความ (ส่วนราชการ / ที่ / วันที่ / เรื่อง / เรียน ... ลงชื่อ)
  report-th  รายงานผลการตรวจสอบ (ข้อมูลโครงการ, ผลการตรวจสอบ, สรุป, ลงชื่อ)

Using a template (document_builder.make(template=...)):
  docx  the template's styles, page setup, header and footer are kept. If its body has
        {{placeholders}} it is a form: the body stays and is filled. Otherwise it is a
        letterhead/sample: the body is cleared. Content (content_markdown, or placeholders.content
        when content_markdown is empty) goes where a {{content}} paragraph is, or at the end.
        Unfilled placeholders become a dotted line and are reported.
  pptx  its masters/layouts are used; its slides are kept (and filled) only if they have
        {{placeholders}}.
  xlsx  cells are filled (a cell that is only {{key}} is written like set_cells: numbers become
        numbers, unsafe formulas stay text); a sheet spec whose name exists appends rows below its data.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

import document_builder as DB
import document_editor as ED
from project_store import PROJECTS_ROOT

TEMPLATES_ROOT = PROJECTS_ROOT / "_templates"
INDEX_NAME = "templates.json"
MAX_TEMPLATE_BYTES = 25 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_TEMPLATES = 200
LEFTOVER = "\u2026" * 12
KINDS = {".docx": "docx", ".dotx": "docx", ".pptx": "pptx", ".potx": "pptx", ".xlsx": "xlsx", ".xltx": "xlsx"}
FORMAT_KIND = {"docx": "docx", "pdf": "docx", "pptx": "pptx", "xlsx": "xlsx"}
_TEMPLATE_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml":
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml":
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
}
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}\n]{1,80}?)\s*\}\}")
_CONTENT_KEYS = {"content", "body", "เนื้อหา"}


# ----------------------------------------------------------------------------------- index
def _root() -> Path:
    return Path(TEMPLATES_ROOT)


def _load_index() -> list[dict[str, Any]]:
    path = _root() / INDEX_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return []  # a damaged index must not block document creation; entries are re-added on save
    return [e for e in data if isinstance(e, dict) and e.get("name") and e.get("file")] if isinstance(data, list) else []


def _save_index(entries: list[dict[str, Any]]) -> None:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".{INDEX_NAME}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, root / INDEX_NAME)


def _slug(name: Any) -> str:
    text = re.sub(r"\s+", "-", str(name or "").strip().strip("{}").casefold())
    if not re.fullmatch(r"[0-9a-z\u0e00-\u0e7f][0-9a-z\u0e00-\u0e7f._-]{0,59}", text) or text.endswith("."):
        raise ValueError("ชื่อแม่แบบใช้ได้เฉพาะ ก-ฮ a-z 0-9 . _ - ยาวไม่เกิน 60 ตัว เช่น memo-egat")
    return text


def _upsert(entry: dict[str, Any]) -> None:
    entries = [e for e in _load_index() if e.get("name") != entry["name"]]
    entries.append(entry)
    entries.sort(key=lambda e: str(e.get("name")))
    _save_index(entries)


# ----------------------------------------------------------------------------------- scanning
def _normalize_package(src: Path, dest: Path) -> None:
    """Copy an OOXML package, turning a template content type (.dotx/.potx/.xltx) into the
    document type, which python-docx / python-pptx / openpyxl can open and save."""
    with zipfile.ZipFile(src) as zin:
        if sum(i.file_size for i in zin.infolist()) > MAX_UNPACKED_BYTES:
            raise ValueError("ไฟล์แม่แบบคลายออกแล้วใหญ่เกินไป")
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "[Content_Types].xml":
                    text = data.decode("utf-8")
                    for old, new in _TEMPLATE_TYPES.items():
                        text = text.replace(old, new)
                    data = text.encode("utf-8")
                zout.writestr(item, data)


def _xlsx_strings(wb: Any) -> list[str]:
    return [c.value for ws in wb.worksheets for c in ws._cells.values() if isinstance(c.value, str)]


def _scan(path: Path, kind: str) -> dict[str, Any]:
    """Open with the library that will use it (a broken file fails here, not at first use)."""
    try:
        if kind == "docx":
            from docx import Document

            doc = Document(str(path))
            texts = ED.docx_texts(doc)
            body_has = any(_PLACEHOLDER.search(t) for t in ED._docx_body_texts(doc))
        elif kind == "pptx":
            from pptx import Presentation

            prs = Presentation(str(path))
            texts = ED.pptx_texts(prs)
            body_has = any(_PLACEHOLDER.search(t) for t in texts)
        else:
            from openpyxl import load_workbook

            texts = _xlsx_strings(load_workbook(str(path)))
            body_has = True
    except Exception as exc:
        raise ValueError(f"เปิดไฟล์แม่แบบไม่ได้ (ไฟล์เสียหรือไม่ใช่ไฟล์ Office): {exc}") from exc
    keys = sorted({m.group(1).strip() for t in texts for m in _PLACEHOLDER.finditer(t)})
    return {"placeholders": keys, "form": body_has}


# ----------------------------------------------------------------------------------- builtins
def _thai_styles(doc: Any) -> None:
    from docx.shared import Pt

    normal = doc.styles["Normal"]
    normal.font.size = Pt(16)
    DB._docx_fonts(normal.element, 16)
    normal.paragraph_format.space_after = Pt(0)
    for name, size in (("Title", 26), ("Heading 1", 20), ("Heading 2", 18), ("Heading 3", 17)):
        try:
            DB._docx_fonts(doc.styles[name].element, size)
        except KeyError:
            pass


def _a4(doc: Any, top: float, bottom: float, left: float, right: float) -> None:
    from docx.shared import Cm

    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21.0), Cm(29.7)
    section.top_margin, section.bottom_margin = Cm(top), Cm(bottom)
    section.left_margin, section.right_margin = Cm(left), Cm(right)


def _line(container: Any, parts: list[tuple[str, bool, float | None]], align: Any = None, after: float = 0,
          indent_cm: float | None = None) -> Any:
    from docx.shared import Cm, Pt

    para = container.add_paragraph()
    for text, bold, size in parts:
        run = para.add_run(text)
        if bold:
            run.bold = True
            run.font.cs_bold = True
        DB._docx_fonts(run._r, size)
    if align is not None:
        para.alignment = align
    if after:
        para.paragraph_format.space_after = Pt(after)
    if indent_cm is not None:
        para.paragraph_format.left_indent = Cm(indent_cm)
    return para


def _bottom_rule(para: Any) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    ppr = para._p.get_or_add_pPr()
    border = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for key, value in (("w:val", "single"), ("w:sz", "6"), ("w:space", "4"), ("w:color", "auto")):
        bottom.set(qn(key), value)
    border.append(bottom)
    ppr.insert_element_before(border, "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
                              "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi",
                              "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing",
                              "w:mirrorIndents", "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
                              "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr",
                              "w:pPrChange")


def _build_memo(path: Path) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm

    doc = Document()
    _a4(doc, 2.0, 2.0, 3.0, 2.0)
    _thai_styles(doc)
    _line(doc, [("บันทึกข้อความ", True, 29)], WD_ALIGN_PARAGRAPH.CENTER, after=6)
    _line(doc, [("ส่วนราชการ", True, 20), ("  {{department}}", False, 16)])
    para = _line(doc, [("ที่", True, 20), ("  {{ref_no}}", False, 16), ("\t", False, 16),
                       ("วันที่", True, 20), ("  {{date}}", False, 16)])
    para.paragraph_format.tab_stops.add_tab_stop(Cm(8.0))
    _bottom_rule(_line(doc, [("เรื่อง", True, 20), ("  {{subject}}", False, 16)], after=6))
    _line(doc, [("เรียน", False, 16), ("  {{to}}", False, 16)], after=6)
    content = _line(doc, [("{{content}}", False, 16)])
    content.paragraph_format.first_line_indent = Cm(2.5)
    _line(doc, [("", False, 16)])
    _line(doc, [("", False, 16)])
    _line(doc, [("({{signer}})", False, 16)], WD_ALIGN_PARAGRAPH.CENTER, indent_cm=7.0)
    _line(doc, [("{{position}}", False, 16)], WD_ALIGN_PARAGRAPH.CENTER, indent_cm=7.0)
    doc.save(str(path))


def _page_field(para: Any) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "1"
    run.append(text)
    field.append(run)
    para._p.append(field)


def _build_report(path: Path) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm

    doc = Document()
    _a4(doc, 2.5, 2.0, 2.5, 2.0)
    _thai_styles(doc)
    section = doc.sections[0]
    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = header.add_run("รายงานผลการตรวจสอบ  เลขที่ {{report_no}}")
    DB._docx_fonts(run._r, 14)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    DB._docx_fonts(footer.add_run("หน้า ")._r, 14)
    _page_field(footer)
    _line(doc, [("รายงานผลการตรวจสอบ", True, 24)], WD_ALIGN_PARAGRAPH.CENTER)
    _line(doc, [("{{title}}", True, 20)], WD_ALIGN_PARAGRAPH.CENTER, after=12)
    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for label, key in (("โครงการ/สถานที่", "project"), ("เลขที่รายงาน", "report_no"),
                       ("วันที่ตรวจสอบ", "date"), ("ผู้ตรวจสอบ", "inspector")):
        cells = table.add_row().cells
        cells[0].width, cells[1].width = Cm(5.0), Cm(11.5)
        for cell, text, bold in ((cells[0], label, True), (cells[1], "{{" + key + "}}", False)):
            run = cell.paragraphs[0].add_run(text)
            if bold:
                run.bold = run.font.cs_bold = True
            DB._docx_fonts(run._r)
    _line(doc, [("", False, 16)])
    doc.add_heading("ผลการตรวจสอบ", level=1)
    _line(doc, [("{{content}}", False, 16)])
    doc.add_heading("สรุปและข้อเสนอแนะ", level=1)
    _line(doc, [("{{summary}}", False, 16)])
    _line(doc, [("", False, 16)])
    _line(doc, [("", False, 16)])
    signatures = doc.add_table(rows=2, cols=2)
    for col, (role, key) in enumerate((("ผู้ตรวจสอบ", "inspector"), ("ผู้อนุมัติ", "approver"))):
        for row, text in enumerate((f"ลงชื่อ ................................ {role}", "({{" + key + "}})")):
            para = signatures.cell(row, col).paragraphs[0]
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            DB._docx_fonts(para.add_run(text)._r)
    doc.save(str(path))


BUILTINS: dict[str, dict[str, Any]] = {
    "memo-th": {
        "kind": "docx",
        "description": "บันทึกข้อความราชการ: ส่วนราชการ ที่ วันที่ เรื่อง เรียน เนื้อหา และลงชื่อ",
        "title_key": "subject",
        # listed before first use (the file is built lazily); the body goes in content_markdown
        "placeholders": ["date", "department", "position", "ref_no", "signer", "subject", "to"],
        "builder": _build_memo,
    },
    "report-th": {
        "kind": "docx",
        "description": "รายงานผลการตรวจสอบ: ตารางข้อมูลโครงการ ผลการตรวจสอบ สรุปและข้อเสนอแนะ และช่องลงชื่อ",
        "title_key": "title",
        "placeholders": ["approver", "date", "inspector", "project", "report_no", "summary", "title"],
        "builder": _build_report,
    },
}


def _entry_public(entry: dict[str, Any]) -> dict[str, Any]:
    keys = ("name", "kind", "description", "placeholders", "builtin", "file", "created_at", "source_name")
    public = {k: entry[k] for k in keys if k in entry}
    if entry.get("kind") == "docx" and isinstance(public.get("placeholders"), list):
        # {{content}} in a docx template marks where content_markdown goes; it is not a field to fill
        public["placeholders"] = [k for k in public["placeholders"] if str(k).casefold() not in _CONTENT_KEYS]
    return public


def _ensure_builtin(slug: str) -> tuple[Path, dict[str, Any]]:
    spec = BUILTINS[slug]
    root = _root()
    path = root / f"{slug}.{spec['kind']}"
    entry = next((e for e in _load_index() if e.get("name") == slug), None)
    if not path.exists() or entry is None:
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / f".{slug}.{os.getpid()}.tmp.{spec['kind']}"
        try:
            spec["builder"](tmp)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        entry = {
            "name": slug,
            "file": path.name,
            "kind": spec["kind"],
            "description": spec["description"],
            "placeholders": _scan(path, spec["kind"])["placeholders"],
            "builtin": True,
            "title_key": spec["title_key"],
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _upsert(entry)
    return path, entry


# ----------------------------------------------------------------------------------- actions
def resolve(name: Any) -> tuple[Path, dict[str, Any]]:
    slug = _slug(name)
    for entry in _load_index():
        if entry.get("name") == slug and not entry.get("builtin"):
            path = _root() / str(entry["file"])
            if path.is_file():
                return path, entry
            raise ValueError(f"ไฟล์ของแม่แบบ '{slug}' หายไปจาก {_root()} ให้บันทึกแม่แบบใหม่")
    if slug in BUILTINS:
        return _ensure_builtin(slug)
    names = sorted({e["name"] for e in _load_index()} | set(BUILTINS))
    raise ValueError(f"ไม่พบแม่แบบ '{slug}' (มี: {', '.join(names)}) — ดูรายการด้วย list_templates")


def list_templates() -> dict[str, Any]:
    entries = {e["name"]: dict(e) for e in _load_index()}
    for slug, spec in BUILTINS.items():
        if slug not in entries:
            entries[slug] = {"name": slug, "kind": spec["kind"], "description": spec["description"],
                             "builtin": True, "placeholders": list(spec["placeholders"]),
                             "note": "สร้างอัตโนมัติเมื่อใช้ครั้งแรก"}
    return {
        "folder": str(_root()),
        "templates": [_entry_public(e) | ({"note": e["note"]} if "note" in e else {}) for e in
                      sorted(entries.values(), key=lambda e: (not e.get("builtin"), e["name"]))],
        "usage": 'make_document(project, format="docx"|"pdf"|"pptx"|"xlsx", template="<name>", '
                 'placeholders={"key": "ค่า"}, content_markdown="...")',
    }


def save_template(source: Path, name: Any, description: str = "", overwrite: bool = False) -> dict[str, Any]:
    source = Path(source)
    overwrite = overwrite is True or str(overwrite).strip().casefold() in {"1", "true", "yes", "on"}
    ED.check_not_macro(source)
    kind = KINDS.get(source.suffix.lower())
    if kind is None:
        raise ValueError("แม่แบบต้องเป็นไฟล์ .docx, .dotx, .pptx, .potx, .xlsx หรือ .xltx")
    if not source.is_file():
        raise FileNotFoundError(f"ไม่พบไฟล์: {source}")
    if source.stat().st_size > MAX_TEMPLATE_BYTES:
        raise ValueError(f"ไฟล์แม่แบบใหญ่เกิน {MAX_TEMPLATE_BYTES // (1024 * 1024)} MB")
    slug = _slug(name)
    if slug in BUILTINS:
        raise ValueError(f"'{slug}' เป็นชื่อแม่แบบในตัว ให้ใช้ชื่ออื่น")
    ED.zip_names(source)  # refuses hidden macros and broken files
    entries = _load_index()
    existing = next((e for e in entries if e.get("name") == slug), None)
    if existing and not overwrite:
        raise ValueError(f"มีแม่แบบชื่อ '{slug}' อยู่แล้ว ใช้ overwrite=true เพื่อแทนที่ หรือใช้ชื่ออื่น")
    if not existing and len(entries) >= MAX_TEMPLATES:
        raise ValueError(f"มีแม่แบบครบ {MAX_TEMPLATES} แบบแล้ว")
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="qwen-template-"))
    try:
        staged = work / f"template.{kind}"
        _normalize_package(source, staged)
        info = _scan(staged, kind)
        final = root / f"{DB._safe_name(slug, 'template')}.{kind}"
        replaced = None
        if existing:
            old = root / str(existing["file"])
            if old.exists():
                keep = root / ".old"
                keep.mkdir(exist_ok=True)
                replaced = DB._reserve(keep / f"{old.stem}-{time.strftime('%Y%m%d-%H%M%S')}{old.suffix}")
                os.replace(old, replaced)
        shutil.copyfile(staged, final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    entry = {
        "name": slug,
        "file": final.name,
        "kind": kind,
        "description": str(description or "").strip()[:300],
        "placeholders": info["placeholders"],
        "builtin": False,
        "source_name": source.name,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _upsert(entry)
    if kind == "docx":
        usage = ("มี {{ช่อง}} ในเนื้อหา จึงคงเนื้อหาเดิมไว้และเติมค่าให้" if info["form"] else
                 "ไม่มี {{ช่อง}} ในเนื้อหา: เมื่อใช้จะคงหัว/ท้ายกระดาษ สไตล์ และขนาดหน้า แล้วล้างเนื้อหาเดิมก่อนใส่เนื้อหาใหม่")
    elif kind == "pptx":
        usage = ("สไลด์เดิมมี {{ช่อง}} จึงคงไว้และเติมค่า" if info["form"] else
                 "เมื่อใช้จะใช้ธีม/layout ของไฟล์นี้ และไม่เอาสไลด์เดิมมา")
    else:
        usage = "เมื่อใช้จะเติม {{ช่อง}} ในเซลล์ และเพิ่มแถว/ชีตตาม sheets"
    result = {"status": "saved", "template": _entry_public(entry), "path": str(final), "how_it_is_used": usage}
    if replaced:
        result["previous_version"] = str(replaced)
    return result


# ----------------------------------------------------------------------------------- building
def _key(k: Any) -> str:
    return str(k).strip().strip("{}").strip()


def _values(entry: dict[str, Any], title: str, placeholders: dict[str, Any]) -> dict[str, str]:
    values = {_key(k): ("" if v is None else str(v)) for k, v in (placeholders or {}).items()}
    if title:
        values.setdefault("title", title)
        if entry.get("title_key"):
            values.setdefault(str(entry["title_key"]), title)
    # only a docx template's {{content}} is the content_markdown marker; elsewhere it is a plain field
    docx = str(entry.get("kind") or "docx") == "docx"
    return {k: v for k, v in values.items() if k and not (docx and k.casefold() in _CONTENT_KEYS)}


def _content_from_placeholders(placeholders: dict[str, Any], markdown: str, warnings: list[str]) -> tuple[dict[str, Any], str]:
    """placeholders.content/body/เนื้อหา on a docx template is the body: use it as content_markdown
    (it used to be dropped silently, leaving a memo without its text)."""
    given = [(k, v) for k, v in placeholders.items() if _key(k).casefold() in _CONTENT_KEYS]
    if not given:
        return placeholders, markdown
    rest = {k: v for k, v in placeholders.items() if _key(k).casefold() not in _CONTENT_KEYS}
    body = "\n\n".join(str(v) for _k, v in given if v is not None and str(v).strip())
    if not (markdown or "").strip():
        return rest, body
    if body:
        warnings.append("ไม่ได้ใช้ placeholders." + ", placeholders.".join(_key(k) for k, _v in given)
                        + " เพราะส่ง content_markdown มาแล้ว (เนื้อหาใส่ใน content_markdown ช่องเดียว)")
    return rest, markdown


def _docx_from_template(tpl: Path, out: Path, title: str, markdown: str, values: dict[str, str]) -> dict[str, Any]:
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(str(tpl))
    body = doc.element.body
    form = any(_PLACEHOLDER.search(t) for t in ED._docx_body_texts(doc))
    if not form:
        # a letterhead or sample document: keep page setup, header and footer, drop the old text
        for child in list(body):
            if child.tag != qn("w:sectPr"):
                body.remove(child)
    marker = None
    for p in body.iter(qn("w:p")):
        text = "".join(s.text for s in ED._docx_segments(p)).strip()
        m = _PLACEHOLDER.fullmatch(text)
        if m and m.group(1).strip().casefold() in _CONTENT_KEYS:
            marker = p
            for s in ED._docx_segments(p):
                if s.editable:
                    s.el.text = ""  # hide the marker from the placeholder fill below
            break
    info = ED.fill_docx_placeholders(doc, values, leftover=LEFTOVER)
    inherit = ED.doc_has_cs_font(doc)
    blocks = 0
    if not form and title:
        if ED._style(doc, "Title") is not None:
            heading = doc.add_heading("", level=0)
            ED._runs(heading, title, inherit)
        else:
            ED.render_markdown(doc, f"# {title}", inherit=inherit)
    if (markdown or "").strip():
        added: list[Any] = []
        blocks = ED.render_markdown(doc, markdown, anchor=marker, inherit=inherit, added=added)
        marker_ppr = marker.find(qn("w:pPr")) if marker is not None else None
        if marker_ppr is not None:
            # plain paragraphs take the marker's layout (e.g. the memo's first-line indent);
            # headings, lists and numbered items keep their own
            for el in added:
                if el.tag == qn("w:p") and el.find(qn("w:pPr")) is None:
                    el.insert(0, copy.deepcopy(marker_ppr))
    if marker is not None and marker.getparent() is not None:
        marker.getparent().remove(marker)
    doc.save(str(out))
    return {"blocks": blocks, "kept_template_body": form, "placeholders": info}


def _pptx_from_template(tpl: Path, out: Path, title: str, slides: list[Any], values: dict[str, str]) -> dict[str, Any]:
    from pptx import Presentation
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Emu

    if len(slides) > DB.MAX_SLIDES:
        raise ValueError(f"more than {DB.MAX_SLIDES} slides")
    prs = Presentation(str(tpl))
    font = ED.deck_font(prs)
    form = any(_PLACEHOLDER.search(t) for t in ED.pptx_texts(prs))
    info: dict[str, Any] = {}
    if form:
        info = ED.fill_pptx_placeholders(prs, values, leftover=LEFTOVER)
    else:
        # a theme/sample deck: keep masters and layouts, not its slides
        lst = prs.slides._sldIdLst
        for sld in list(lst):
            prs.part.drop_rel(sld.rId)
            lst.remove(sld)
        if title:
            layout = ED.pick_layout(prs, None, "title")
            slide = prs.slides.add_slide(layout)
            if slide.shapes.title is not None:
                slide.shapes.title.text = title
                if font:
                    for p in slide.shapes.title.text_frame.paragraphs:
                        for r in p.runs:
                            DB._pptx_font(r)
            sub = ED._body_placeholder(slide)
            if sub is not None:
                sub.text = time.strftime("%d/%m/%Y")
    skipped: list[str] = []
    width, height = prs.slide_width, prs.slide_height
    for spec in slides:
        if not isinstance(spec, dict):
            raise ValueError("each slide must be an object {title, bullets, notes?, chart?}")
        chart_spec = spec.get("chart") if isinstance(spec.get("chart"), dict) else None
        if chart_spec:
            data = DB._chart_data(chart_spec)
            layout = ED.pick_layout(prs, spec.get("layout"), "title_only")
            made = ED.pptx_add_slide(prs, {"title": spec.get("title"), "notes": spec.get("notes")}, font, layout)
            kind = {"line": XL_CHART_TYPE.LINE_MARKERS, "pie": XL_CHART_TYPE.PIE}.get(
                str(chart_spec.get("type") or "bar").lower(), XL_CHART_TYPE.COLUMN_CLUSTERED)
            made["slide"].shapes.add_chart(kind, Emu(int(width * 0.06)), Emu(int(height * 0.21)),
                                           Emu(int(width * 0.88)), Emu(int(height * 0.72)), data)
        else:
            made = ED.pptx_add_slide(prs, spec, font)
        skipped.extend(f"สไลด์ {len(prs.slides)}: {s}" for s in made["skipped_placeholders"])
    prs.save(str(out))
    result: dict[str, Any] = {"slides": len(prs.slides), "kept_template_slides": form}
    if info:
        result["placeholders"] = info
    if skipped:
        result["skipped_placeholders"] = skipped[:40]
    return result


def _xlsx_from_template(tpl: Path, out: Path, title: str, sheets: list[Any], values: dict[str, str],
                        raw: dict[str, Any] | None = None) -> dict[str, Any]:
    from openpyxl import load_workbook

    wb = load_workbook(str(tpl))
    counts: dict[str, int] = {}
    unfilled: set[str] = set()
    warnings: list[str] = []
    raw = raw or {}

    def repl(m: re.Match[str]) -> str:
        key = m.group(1).strip()
        if key in values:
            counts[key] = counts.get(key, 0) + 1
            return values[key]
        unfilled.add(key)
        return LEFTOVER

    for ws in wb.worksheets:
        for cell in list(ws._cells.values()):
            if isinstance(cell.value, str) and "{{" in cell.value and not cell.value.startswith("="):
                whole = _PLACEHOLDER.fullmatch(cell.value.strip())
                key = whole.group(1).strip() if whole else ""
                if key in values:
                    # the cell is just {{key}}: write the value the way set_cells does (1500 or
                    # "2,300" becomes a number the template's SUMs count; an unsafe "=..." stays text)
                    counts[key] = counts.get(key, 0) + 1
                    value = raw[key] if raw.get(key) is not None else values[key]
                    ED._write_cell(ws, cell.row, cell.column, value, warnings)
                    continue
                cell.value = _PLACEHOLDER.sub(repl, cell.value)
                if cell.value.startswith("="):
                    cell.data_type = "s"  # a value spliced into text is never a formula
    notes: list[str] = []
    changed: list[tuple[str, str]] = []
    total = 0
    for index, spec in enumerate(sheets, 1):
        if not isinstance(spec, dict):
            raise ValueError("each sheet must be an object {name, rows, chart?}")
        rows = spec.get("rows") or []
        if not isinstance(rows, list):
            raise ValueError("sheet rows must be a list of lists")
        total += len(rows)
        if total > DB.MAX_ROWS:
            raise ValueError(f"more than {DB.MAX_ROWS} rows")
        wanted = str(spec.get("name") or "").strip()
        existing = next((ws for ws in wb.worksheets if wanted and ws.title.casefold() == wanted.casefold()), None)
        if existing is not None:
            if rows:
                ED.xlsx_append_rows(wb, existing, rows, warnings, changed)
            if spec.get("chart"):
                notes.append(f"sheet {existing.title}: chart skipped (the template sheet keeps its own charts)")
        else:
            ws = ED.xlsx_new_sheet(wb, wanted or f"Sheet{len(wb.sheetnames) + 1}", rows, warnings, changed)
            DB._xlsx_chart(ws, [list(r) if isinstance(r, (list, tuple)) else [r] for r in rows], spec.get("chart"), title, notes)
    wb.save(str(out))
    result: dict[str, Any] = {
        "sheets": len(wb.sheetnames),
        "rows": total,
        "placeholders": {"filled": counts, "unfilled": sorted(unfilled),
                         "not_in_document": sorted(k for k in values if k not in counts and k != "title")},
    }
    if warnings:
        result["warnings"] = warnings
    if notes:
        result["notes"] = notes
    return result


def build_document(target: Path, fmt: str, template: str, title: str = "", content_markdown: str = "",
                   sheets: list[Any] | None = None, slides: list[Any] | None = None,
                   placeholders: dict[str, Any] | None = None) -> dict[str, Any]:
    tpl, entry = resolve(template)
    kind = str(entry.get("kind") or KINDS.get(tpl.suffix.lower(), ""))
    if FORMAT_KIND[fmt] != kind:
        raise ValueError(f"แม่แบบ '{entry['name']}' เป็นไฟล์ {kind} ใช้กับ format={fmt} ไม่ได้ "
                         f"(docx/pdf ใช้แม่แบบ docx, pptx ใช้ pptx, xlsx ใช้ xlsx)")
    placeholders = placeholders or {}
    warnings: list[str] = []
    if kind == "docx":
        placeholders, content_markdown = _content_from_placeholders(placeholders, content_markdown, warnings)
    values = _values(entry, title, placeholders)
    if fmt == "docx":
        details = _docx_from_template(tpl, target, title, content_markdown, values)
    elif fmt == "pptx":
        details = _pptx_from_template(tpl, target, title, slides or [], values)
    elif fmt == "xlsx":
        details = _xlsx_from_template(tpl, target, title, sheets or [], values,
                                      raw={_key(k): v for k, v in placeholders.items()})
    else:
        work = Path(tempfile.mkdtemp(prefix="qwen-tpl-pdf-"))
        try:
            docx = work / "document.docx"
            details = _docx_from_template(tpl, docx, title, content_markdown, values)
            pdf, engine = ED.convert_to_pdf(docx, work)
            shutil.copyfile(pdf, target)  # onto our own reserved placeholder
            details["engine"] = engine
        finally:
            shutil.rmtree(work, ignore_errors=True)
    details["template"] = entry["name"]
    if warnings:
        details["warnings"] = warnings + list(details.get("warnings") or [])
    info = details.get("placeholders") or {}
    if info.get("unfilled"):
        details["note"] = ("ช่องที่ยังไม่ได้เติมถูกแทนด้วยเส้นประ: " + ", ".join(info["unfilled"][:20])
                           + " — ส่ง placeholders เพิ่มแล้วสร้างใหม่ได้")
    return details
