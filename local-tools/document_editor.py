"""Edit existing Word, Excel and PowerPoint files and render pages for a visual check (added 2026-09-30).

The model sends a short list of operations instead of regenerating the whole document, which
is 100-400 output tokens instead of thousands at 1-2 tok/s on the CPU. Every edit is
all-or-nothing: the operations are applied in memory and the file is written once, so one
failing operation writes nothing.

The result goes to a new file in the project outputs folder by default, because python-docx and
openpyxl drop parts they do not understand (fields, SmartArt, shapes) when they save. in_place
overwrites the source only after file_history.backup() (or a fallback copy) kept the old bytes.

Thai text is often split across runs (spell-check marks, autocorrect, IME), so text matching
works on the paragraph's joined text and maps the hit back onto the runs: the replacement takes
the formatting of the run where the match starts.
"""
from __future__ import annotations

import copy
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import document_builder as DB

MAX_OPERATIONS = 50
MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_MARKDOWN_CHARS = 200_000
MAX_TEXT_CHARS = 20_000
MAX_RANGE_CELLS = 10_000
MAX_PREVIEW_PAGES = 6
CONVERT_TIMEOUT = 180
RECALC_TIMEOUT = 120
EDIT_KINDS = {".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx"}
MACRO_SUFFIXES = {".docm", ".dotm", ".xlsm", ".xltm", ".xlsb", ".xlam", ".pptm", ".potm", ".ppsm", ".ppam", ".sldm"}
PREVIEW_SUFFIXES = {".pdf", ".docx", ".doc", ".rtf", ".odt", ".pptx", ".ppt", ".odp", ".xlsx", ".xls", ".ods"}
EXCEL_ERRORS = {"#REF!", "#DIV/0!", "#NAME?", "#VALUE!", "#N/A", "#NUM!", "#NULL!"}

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _a(tag: str) -> str:
    return f"{{{A_NS}}}{tag}"


_W_P, _W_R, _W_T = _w("p"), _w("r"), _w("t")
_W_TAB, _W_BR, _W_CR = _w("tab"), _w("br"), _w("cr")
_W_SKIP = {_w("del"), _w("moveFrom")}
_A_P, _A_R, _A_T, _A_BR, _A_FLD = _a("p"), _a("r"), _a("t"), _a("br"), _a("fld")
_MC_FALLBACK = f"{{{MC_NS}}}Fallback"

_THAI = re.compile("[฀-๿]")
_ZERO_WIDTH = re.compile("[​‌‍﻿]")
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}\n]{1,80}?)\s*\}\}")
# a plain number typed as text by the model ("1500", "12,000.50"); leading zeros (codes,
# phone numbers) stay text, and so do bare runs of 12+ digits (a 13-digit Thai ID or an account
# number), which Excel would show as 1.1037E+12
_NUMBER = re.compile(r"^-?(0|[1-9]\d{0,10}|[1-9]\d{11,14}(?=\.\d)|[1-9]\d{0,2}(,\d{3}){1,4})(\.\d+)?$")


# ----------------------------------------------------------------------------------- common
def check_not_macro(path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix in MACRO_SUFFIXES:
        raise ValueError(f"ไม่รองรับไฟล์ที่มีมาโคร ({suffix}) เพื่อความปลอดภัย ให้บันทึกเป็น .docx/.xlsx/.pptx ก่อน")


def zip_names(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"ไฟล์ {path.name} เสียหรือไม่ใช่ไฟล์ Office (.docx/.xlsx/.pptx): {exc}") from exc
    if any(name.lower().endswith("vbaproject.bin") for name in names):
        raise ValueError(f"ไฟล์ {path.name} มีมาโคร (vbaProject.bin) ซ่อนอยู่ จึงไม่แก้ไข")
    return names


def _operations(operations: Any) -> list[dict[str, Any]]:
    if isinstance(operations, str):
        try:
            operations = json.loads(operations)
        except json.JSONDecodeError as exc:
            raise ValueError(f"operations ต้องเป็น JSON list: {exc}") from exc
    if isinstance(operations, dict):
        operations = [operations]
    if not isinstance(operations, list) or not operations:
        raise ValueError('operations ต้องเป็นรายการ [{"op": ...}, ...] อย่างน้อย 1 รายการ')
    if len(operations) > MAX_OPERATIONS:
        raise ValueError(f"operations ได้ไม่เกิน {MAX_OPERATIONS} รายการต่อครั้ง")
    for i, op in enumerate(operations, 1):
        if not isinstance(op, dict) or not str(op.get("op") or "").strip():
            raise ValueError(f'operation #{i} ต้องเป็น object ที่มี "op"')
    return operations


def _text(op: dict[str, Any], key: str, required: bool = True, limit: int = MAX_TEXT_CHARS) -> str:
    value = op.get(key)
    if value is None or (required and str(value) == ""):
        if required:
            raise ValueError(f"ต้องระบุ {key}")
        return ""
    value = str(value)
    if len(value) > limit:
        raise ValueError(f"{key} ยาวเกิน {limit} ตัวอักษร")
    return value


def _int(op: dict[str, Any], key: str, default: int | None = None) -> int:
    value = op.get(key, default)
    try:
        if isinstance(value, bool):
            raise TypeError
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} ต้องเป็นตัวเลข (เริ่มนับจาก 1)") from None


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() not in {"", "0", "false", "no", "off", "ไม่"}
    return bool(value)


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", _ZERO_WIDTH.sub("", text or "")).strip().casefold()


def loose_pattern(find: str) -> re.Pattern[str]:
    """Literal text, but tolerant of what differs invisibly in Thai documents: zero-width
    spaces between characters, any run of whitespace/NBSP, and SARA AM typed as NIKHAHIT+SARA AA."""
    find = _ZERO_WIDTH.sub("", find or "")
    if not find.strip():
        raise ValueError("find ต้องไม่ว่าง")
    tokens: list[str] = []
    for ch in re.findall(r"\s+|.", find, flags=re.S):
        if ch.isspace():
            tokens.append(r"[\s ]+")
        elif ch == "ำ":
            tokens.append("(?:ำ|ํา)")
        else:
            tokens.append(re.escape(ch))
    return re.compile("[​‌‍﻿]*".join(tokens))


def _nearest(find: str, texts: list[str], n: int = 3) -> list[str]:
    target = _ZERO_WIDTH.sub("", find).strip()
    if not target:
        return []
    size = len(target)
    scored: list[tuple[float, str]] = []
    for text in texts[:3000]:
        text = _ZERO_WIDTH.sub("", text).strip()
        if not text:
            continue
        if len(text) <= size * 2:
            windows = [text]
        else:
            step = max(1, size // 3)
            windows = [text[i:i + size + 6] for i in range(0, len(text) - size + 1, step)][:300]
        for window in windows:
            scored.append((difflib.SequenceMatcher(None, target, window).ratio(), window.strip()))
    scored.sort(key=lambda item: -item[0])
    seen: list[str] = []
    for score, window in scored:
        if score < 0.45:
            break
        if window not in seen:
            seen.append(window)
        if len(seen) >= n:
            break
    return seen


def _not_found(find: str, texts: list[str]) -> ValueError:
    near = _nearest(find, texts)
    hint = (" ข้อความที่ใกล้เคียง: " + " | ".join(f'"{t[:80]}"' for t in near)) if near else ""
    return ValueError(f'ไม่พบข้อความ "{find[:120]}" ในเอกสาร{hint} (ตรวจตัวสะกด/ช่องว่างแล้วลองใหม่)')


# ------------------------------------------------------------------- run-spanning text engine
class _Seg:
    """One piece of a paragraph's text: an editable text node, or a fixed tab/break/field."""

    __slots__ = ("el", "text", "editable", "start", "new")

    def __init__(self, el: Any, text: str, editable: bool, start: int) -> None:
        self.el, self.text, self.editable, self.start, self.new = el, text, editable, start, text

    @property
    def end(self) -> int:
        return self.start + len(self.text)


def _docx_segments(p: Any) -> list[_Seg]:
    segs: list[_Seg] = []
    pos = 0
    for el in p.iter(_W_T, _W_TAB, _W_BR, _W_CR):
        parent = el.getparent()
        if parent is None or parent.tag != _W_R:
            continue  # w:tab inside w:tabs is a tab-stop definition, not text
        owner, cur = None, parent
        while cur is not None:
            if cur.tag in _W_SKIP:
                owner = None  # deleted text of a tracked change
                break
            if cur.tag == _W_P:
                owner = cur
                break
            cur = cur.getparent()
        if owner is not p:
            continue  # belongs to a text box nested inside this paragraph, or deleted
        if el.tag == _W_T:
            text, editable = el.text or "", True
        else:
            text, editable = ("\t" if el.tag == _W_TAB else "\n"), False
        segs.append(_Seg(el, text, editable, pos))
        pos += len(text)
    return segs


def _pptx_segments(p: Any) -> list[_Seg]:
    segs: list[_Seg] = []
    pos = 0
    for child in p:
        if child.tag == _A_R:
            t = child.find(_A_T)
            if t is not None:
                segs.append(_Seg(t, t.text or "", True, pos))
                pos += len(t.text or "")
        elif child.tag == _A_BR:
            segs.append(_Seg(child, "\n", False, pos))
            pos += 1
        elif child.tag == _A_FLD:  # slide numbers, dates: leave field text alone
            t = child.find(_A_T)
            text = (t.text or "") if t is not None else ""
            segs.append(_Seg(child, text, False, pos))
            pos += len(text)
    return segs


def _replace_segments(segs: list[_Seg], pattern: re.Pattern[str], repl: str | Callable[[re.Match[str]], str | None],
                      limit: int | None = None) -> tuple[int, list[_Seg], list[Any]]:
    """Replace matches of `pattern` in the joined text; the replacement goes into the first
    editable piece of each match, the rest of the matched text is cut from the other pieces."""
    full = "".join(s.text for s in segs)
    matches: list[tuple[int, int, str]] = []
    for m in pattern.finditer(full):
        if limit is not None and len(matches) >= limit:
            break
        if m.end() == m.start():
            continue
        rep = repl(m) if callable(repl) else repl
        if rep is None:
            continue
        matches.append((m.start(), m.end(), rep))
    removed: list[Any] = []
    done = 0
    # right to left, so the offsets of earlier matches stay valid
    for ms, me, rep in reversed(matches):
        overlap = [s for s in segs if s.start < me and s.end > ms]
        target = next((s for s in overlap if s.editable), None)
        if target is None:
            continue
        for s in overlap:
            if s.editable:
                a, b = max(ms, s.start) - s.start, min(me, s.end) - s.start
                s.new = s.new[:a] + (rep if s is target else "") + s.new[b:]
            elif s.start >= ms and s.end <= me and s.text in ("\t", "\n"):
                removed.append(s.el)
        done += 1
    return done, [s for s in segs if s.editable and s.new != s.text], removed


def _preserve(t: Any) -> None:
    t.set(XML_SPACE, "preserve")


def _set_w_text(t: Any, text: str) -> None:
    from docx.oxml import OxmlElement

    parts = text.split("\n")
    t.text = parts[0]
    _preserve(t)
    anchor = t
    for part in parts[1:]:
        br = OxmlElement("w:br")
        anchor.addnext(br)
        t2 = OxmlElement("w:t")
        t2.text = part
        _preserve(t2)
        br.addnext(t2)
        anchor = t2


def _set_a_text(t: Any, text: str) -> None:
    parts = text.split("\n")
    t.text = parts[0]
    run = t.getparent()
    anchor = run
    for part in parts[1:]:
        br = run.makeelement(_A_BR, {})
        rpr = run.find(_a("rPr"))
        if rpr is not None:
            br.append(copy.deepcopy(rpr))
        anchor.addnext(br)
        r2 = copy.deepcopy(run)
        r2.find(_A_T).text = part
        br.addnext(r2)
        anchor = r2


# ----------------------------------------------------------------------------------- docx
def doc_has_cs_font(doc: Any) -> bool:
    """True when the document already says which font Thai (complex script) text uses; new
    runs then inherit it. Otherwise Word falls back to a default font, so TH Sarabun New is set."""
    from docx.oxml.ns import qn

    styles = doc.styles.element
    candidates = []
    defaults = styles.find(qn("w:docDefaults"))
    if defaults is not None:
        candidates.extend(defaults.iter(qn("w:rFonts")))
    try:
        from docx.enum.style import WD_STYLE_TYPE

        normal = doc.styles.default(WD_STYLE_TYPE.PARAGRAPH)
        if normal is not None:
            candidates.extend(normal.element.iter(qn("w:rFonts")))
    except Exception:
        pass
    return any(f.get(qn("w:cs")) or f.get(qn("w:cstheme")) for f in candidates)


def _fix_thai_run(r: Any, inherit: bool) -> None:
    """A run that now holds Thai: give the complex-script slot a Thai font when nothing else
    does, and mirror bold/italic into w:bCs/w:iCs (Word reads those for Thai)."""
    from docx.oxml.ns import qn
    from docx.text.run import Run

    run = Run(r, None)
    rpr = r.find(qn("w:rPr"))
    fonts = rpr.find(qn("w:rFonts")) if rpr is not None else None
    has_cs = fonts is not None and (fonts.get(qn("w:cs")) or fonts.get(qn("w:cstheme")))
    if not has_cs and not inherit:
        r.get_or_add_rPr().get_or_add_rFonts().set(qn("w:cs"), DB.THAI_FONT)
    if run.bold and run.font.cs_bold is None:
        run.font.cs_bold = True
    if run.italic and run.font.cs_italic is None:
        run.font.cs_italic = True


def _commit_docx(modified: list[_Seg], removed: list[Any], inherit: bool) -> None:
    for s in modified:
        _set_w_text(s.el, s.new)
        if _THAI.search(s.new):
            _fix_thai_run(s.el.getparent(), inherit)
    for el in removed:
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def _in_fallback(el: Any) -> bool:
    cur = el.getparent()
    while cur is not None:
        if cur.tag == _MC_FALLBACK:
            return True
        cur = cur.getparent()
    return False


def _docx_roots(doc: Any, headers: bool = True) -> list[Any]:
    roots = [doc.element.body]
    if not headers:
        return roots
    seen: set[str] = set()
    for section in doc.sections:
        for hf in (section.header, section.first_page_header, section.even_page_header,
                   section.footer, section.first_page_footer, section.even_page_footer):
            # a linked header has no part of its own; touching ._element would create one
            if hf.is_linked_to_previous:
                continue
            key = str(hf.part.partname)
            if key in seen:
                continue
            seen.add(key)
            roots.append(hf._element)
    return roots


def _docx_paragraphs(doc: Any, headers: bool = True):
    for root in _docx_roots(doc, headers):
        for p in root.iter(_W_P):
            yield p


def docx_texts(doc: Any) -> list[str]:
    return ["".join(s.text for s in _docx_segments(p)) for p in _docx_paragraphs(doc)]


def docx_replace(doc: Any, pattern: re.Pattern[str], repl: Any, limit: int | None = None,
                 inherit: bool | None = None) -> int:
    if inherit is None:
        inherit = doc_has_cs_font(doc)
    total = 0
    for p in _docx_paragraphs(doc):
        # a text box is stored twice (mc:Choice and its VML mc:Fallback); both are edited so
        # they stay the same, but only the first copy is counted
        counted = not _in_fallback(p)
        if limit is not None and counted and total >= limit:
            break
        segs = _docx_segments(p)
        if not segs:
            continue
        remaining = None if limit is None or not counted else limit - total
        n, modified, removed = _replace_segments(segs, pattern, repl, remaining)
        _commit_docx(modified, removed, inherit)
        if counted:
            total += n
    return total


def fill_docx_placeholders(doc: Any, values: dict[str, Any], leftover: str | None = None) -> dict[str, Any]:
    """{{key}} -> value everywhere (body, tables, text boxes, headers, footers)."""
    values = {str(k).strip(): ("" if v is None else str(v)) for k, v in (values or {}).items()}
    counts: Counter[str] = Counter()

    def repl(m: re.Match[str]) -> str | None:
        key = m.group(1).strip()
        if key in values:
            counts[key] += 1
            return values[key]
        return None

    docx_replace(doc, _PLACEHOLDER, repl)
    remaining = sorted({m.group(1).strip() for t in docx_texts(doc) for m in _PLACEHOLDER.finditer(t)})
    if leftover is not None and remaining:
        docx_replace(doc, _PLACEHOLDER, lambda m: leftover)
    return {
        "filled": dict(counts),
        "not_in_document": sorted(k for k in values if k not in counts),
        "unfilled": remaining,
    }


def _docx_body_texts(doc: Any) -> list[str]:
    return ["".join(s.text for s in _docx_segments(p)) for p in doc.element.body.iter(_W_P)]


# markdown into an existing document ------------------------------------------------------
def _style(doc: Any, name: str) -> Any:
    try:
        return doc.styles[name]
    except KeyError:
        return None


def _runs(paragraph: Any, text: str, inherit: bool) -> None:
    from docx.oxml.ns import qn

    DB._add_runs(paragraph, text)
    for run in paragraph.runs:
        rpr = run._r.rPr
        fonts = rpr.find(qn("w:rFonts")) if rpr is not None else None
        if fonts is None:
            continue
        if inherit:
            rpr.remove(fonts)  # take the document's own fonts
        else:
            # keep the document's Latin font; only the Thai slot needs TH Sarabun New
            for attr in ("w:ascii", "w:hAnsi", "w:eastAsia"):
                if fonts.get(qn(attr)) is not None:
                    del fonts.attrib[qn(attr)]


def _grid_borders(table: Any) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tblpr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        for key, value in (("w:val", "single"), ("w:sz", "4"), ("w:space", "0"), ("w:color", "auto")):
            el.set(qn(key), value)
        borders.append(el)
    later = next((tblpr.find(qn(t)) for t in ("w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook")
                  if tblpr.find(qn(t)) is not None), None)
    if later is not None:
        later.addprevious(borders)  # schema order matters to Word
    else:
        tblpr.append(borders)


def render_markdown(doc: Any, markdown: str, anchor: Any = None, inherit: bool | None = None,
                    added: list[Any] | None = None) -> int:
    """Add markdown blocks at the end of the body, or right after `anchor` (a body element).
    Missing styles (user documents often lack List Bullet / Table Grid) fall back to direct
    formatting instead of failing. The new body elements are appended to `added` if given."""
    from docx.shared import Cm, Pt

    if len(markdown or "") > MAX_MARKDOWN_CHARS:
        raise ValueError(f"markdown ยาวเกิน {MAX_MARKDOWN_CHARS} ตัวอักษร")
    if inherit is None:
        inherit = doc_has_cs_font(doc)
    body = doc.element.body
    before = set(body)
    blocks = DB._blocks(markdown)
    for kind, value in blocks:
        if kind in ("h1", "h2", "h3", "h4"):
            level = int(kind[1])
            style = _style(doc, f"Heading {level}")
            para = doc.add_paragraph(style=style) if style is not None else doc.add_paragraph()
            _runs(para, value, inherit)
            if style is None:
                for run in para.runs:
                    run.bold = run.font.cs_bold = True
                    run.font.size = Pt({1: 20, 2: 18, 3: 17, 4: 16}[level])
        elif kind == "p":
            _runs(doc.add_paragraph(), value, inherit)
        elif kind == "ul":
            level, text = value
            style = _style(doc, "List Bullet" if level == 0 else f"List Bullet {level + 1}") or _style(doc, "List Bullet")
            if style is not None:
                para = doc.add_paragraph(style=style)
                _runs(para, text, inherit)
            else:
                para = doc.add_paragraph()
                para.paragraph_format.left_indent = Cm(0.63 * (level + 1))
                para.paragraph_format.first_line_indent = Cm(-0.5)
                _runs(para, f"• {text}", inherit)
        elif kind == "ol":
            number, text = value
            para = doc.add_paragraph()
            para.paragraph_format.left_indent = Cm(0.9)
            para.paragraph_format.first_line_indent = Cm(-0.6)
            _runs(para, f"{number}. {text}", inherit)
        elif kind == "hr":
            doc.add_paragraph("")
        elif kind == "table":
            width = max(len(r) for r in value)
            table = doc.add_table(rows=0, cols=width)
            if _style(doc, "Table Grid") is not None:
                table.style = "Table Grid"
            else:
                _grid_borders(table)
            for i, row in enumerate(value):
                cells = table.add_row().cells
                for j in range(width):
                    text = row[j] if j < len(row) else ""
                    _runs(cells[j].paragraphs[0], f"**{text}**" if i == 0 and text else text, inherit)
    new = [el for el in body if el not in before]
    if anchor is not None:
        for el in new:
            anchor.addnext(el)
            anchor = el
    if added is not None:
        added.extend(new)
    return len(blocks)


def _heading_level(paragraph: Any) -> int | None:
    from docx.oxml.ns import qn

    try:
        name = (paragraph.style.name or "") if paragraph.style is not None else ""
    except Exception:
        name = ""
    if name.casefold() == "title":
        return 0
    m = re.match(r"(?i)heading\s*(\d)", name)
    if m:
        return int(m.group(1))
    for holder in (paragraph._p.pPr, getattr(paragraph.style, "element", None)):
        if holder is None:
            continue
        lvl = holder.find(".//" + qn("w:outlineLvl"))
        if lvl is not None and str(lvl.get(qn("w:val"))).isdigit() and int(lvl.get(qn("w:val"))) < 9:
            return int(lvl.get(qn("w:val"))) + 1
    return None


def _find_heading(doc: Any, heading: str) -> tuple[Any, int | None]:
    want = _normal(heading)
    if not want:
        raise ValueError("ต้องระบุ heading")
    paragraphs = [(p, _normal(p.text), _heading_level(p)) for p in doc.paragraphs]
    for test in (lambda t, lvl: lvl is not None and t == want,
                 lambda t, lvl: lvl is not None and want in t,
                 lambda t, lvl: t == want):
        for p, text, level in paragraphs:
            if text and test(text, level):
                return p, level
    headings = [p.text.strip()[:60] for p, _, lvl in paragraphs if lvl is not None and p.text.strip()]
    near = _nearest(heading, headings) or headings[:8]
    raise ValueError(f'ไม่พบหัวข้อ "{heading[:80]}"' + (f" หัวข้อที่มี: {' | '.join(near)}" if near else " (เอกสารไม่มีหัวข้อที่ใช้สไตล์ Heading)"))


def _section_end(doc: Any, heading_p: Any, level: int | None) -> Any:
    """Last body element of the heading's section (before the next heading of the same or a higher level)."""
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph

    anchor = heading_p._p
    el = anchor.getnext()
    while el is not None and el.tag != qn("w:sectPr"):
        if el.tag == _W_P:
            other = _heading_level(Paragraph(el, heading_p._parent))
            if other is not None and (level is None or other <= level):
                break
        anchor = el
        el = el.getnext()
    return anchor


def _table_at(doc: Any, index: Any) -> Any:
    tables = doc.tables
    if not tables:
        raise ValueError("เอกสารนี้ไม่มีตาราง")
    i = _int({"table": index}, "table")
    if not 1 <= i <= len(tables):
        raise ValueError(f"ไม่มีตารางที่ {i} (เอกสารมี {len(tables)} ตาราง เริ่มนับจาก 1)")
    return tables[i - 1]


def _set_paragraph_text(p: Any, text: str, inherit: bool) -> None:
    """Put `text` into a paragraph, keeping the formatting of its first run (or of the paragraph mark)."""
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph

    segs = _docx_segments(p)
    first = next((s for s in segs if s.editable), None)
    if first is not None:
        _set_w_text(first.el, text)
        for s in segs:
            if s is first or s.el.getparent() is None:
                continue
            if s.editable:
                s.el.text = ""
            else:
                s.el.getparent().remove(s.el)
        if _THAI.search(text):
            _fix_thai_run(first.el.getparent(), inherit)
        return
    run = Paragraph(p, None).add_run(text)
    ppr = p.pPr
    mark = ppr.find(qn("w:rPr")) if ppr is not None else None
    if mark is not None:  # an empty cell's formatting lives on the paragraph mark
        rpr = copy.deepcopy(mark)
        # revision marks are valid on a paragraph mark but not on a run
        for tag in ("w:ins", "w:del", "w:moveFrom", "w:moveTo", "w:rPrChange"):
            for el in rpr.findall(qn(tag)):
                rpr.remove(el)
        run._r.insert(0, rpr)
    if _THAI.search(text):
        _fix_thai_run(run._r, inherit)


def _set_cell(cell: Any, text: str, inherit: bool) -> None:
    paragraphs = cell.paragraphs
    for extra in paragraphs[1:]:
        extra._p.getparent().remove(extra._p)
    _set_paragraph_text(paragraphs[0]._p, text, inherit)


def _docx_add_row(table: Any, cells: list[Any], after_row: int | None, inherit: bool) -> int:
    from docx.oxml.ns import qn
    from docx.table import _Cell

    rows = table._tbl.tr_lst
    if not rows:
        raise ValueError("ตารางนี้ไม่มีแถวให้ใช้เป็นแบบ")
    if after_row is not None and not 1 <= after_row <= len(rows):
        raise ValueError(f"after_row ต้องอยู่ระหว่าง 1-{len(rows)}")
    anchor = rows[(after_row or len(rows)) - 1]
    model = anchor
    header_copy = False
    if anchor is rows[0] and len(rows) > 1:
        model = rows[1]  # never copy the header row's look into a data row
    elif anchor is rows[0]:
        header_copy = True
    new_tr = copy.deepcopy(model)
    # keep the row's look, not its content: pictures, comment anchors, bookmarks and note
    # references would otherwise be duplicated (or share ids with the originals)
    for tag in ("w:vMerge", "w:tblHeader", "w:drawing", "w:pict", "w:object", "w:commentRangeStart",
                "w:commentRangeEnd", "w:commentReference", "w:bookmarkStart", "w:bookmarkEnd",
                "w:footnoteReference", "w:endnoteReference"):
        for el in list(new_tr.iter(qn(tag))):
            el.getparent().remove(el)
    if header_copy:
        for tag in ("w:b", "w:bCs", "w:shd"):
            for el in list(new_tr.iter(qn(tag))):
                el.getparent().remove(el)
    tcs = new_tr.findall(qn("w:tc"))
    if len(cells) > len(tcs):
        raise ValueError(f"แถวของตารางนี้มี {len(tcs)} ช่อง แต่ส่งมา {len(cells)} ค่า")
    anchor.addnext(new_tr)
    for i, tc in enumerate(tcs):
        _set_cell(_Cell(tc, table), "" if i >= len(cells) or cells[i] is None else str(cells[i]), inherit)
    return table._tbl.tr_lst.index(new_tr) + 1


def _single_text_run(r: Any) -> bool:
    from docx.oxml.ns import qn

    children = [c for c in r if c.tag != qn("w:rPr")]
    return len(children) == 1 and children[0].tag == _W_T


def _docx_comment(doc: Any, find: str, text: str, author: str, initials: str) -> dict[str, Any]:
    from docx.text.paragraph import Paragraph
    from docx.text.run import Run

    pattern = loose_pattern(find)
    for p in doc.element.body.iter(_W_P):
        if _in_fallback(p):
            continue
        segs = _docx_segments(p)
        full = "".join(s.text for s in segs)
        m = pattern.search(full)
        if not m:
            continue
        overlap = [s for s in segs if s.editable and s.start < m.end() and s.end > m.start()]
        if not overlap:
            continue
        first, last = overlap[0], overlap[-1]
        r1, r2 = first.el.getparent(), last.el.getparent()
        paragraph = Paragraph(p, None)
        exact = r1.getparent() is p and r2.getparent() is p and _single_text_run(r1) and _single_text_run(r2)
        if exact:
            # split the edge runs so the comment covers exactly the matched text
            b = m.end() - last.start
            t2 = r2.find(_W_T)
            if b < len(t2.text or ""):
                tail = copy.deepcopy(r2)
                tail.find(_W_T).text = t2.text[b:]
                _preserve(tail.find(_W_T))
                t2.text = t2.text[:b]
                _preserve(t2)
                r2.addnext(tail)
            a = m.start() - first.start
            t1 = r1.find(_W_T)
            if a > 0:
                head = copy.deepcopy(r1)
                head.find(_W_T).text = t1.text[:a]
                _preserve(head.find(_W_T))
                t1.text = t1.text[a:]
                _preserve(t1)
                r1.addprevious(head)
            runs = [Run(r1, paragraph), Run(r2, paragraph)]
        else:
            direct = [r for r in p if r.tag == _W_R]
            if not direct:
                continue
            runs = [Run(direct[0], paragraph), Run(direct[-1], paragraph)]
        doc.add_comment(runs, text=text, author=author, initials=initials)
        return {"anchored": "exact" if exact else "paragraph", "paragraph": full.strip()[:80]}
    raise _not_found(find, _docx_body_texts(doc))


def _edit_docx(src: Path, ops: list[dict[str, Any]], warnings: list[str]) -> tuple[Any, list[str], dict[str, Any]]:
    from docx import Document

    try:
        doc = Document(str(src))
    except Exception as exc:
        raise ValueError(f"เปิดไฟล์ Word ไม่ได้: {exc}") from exc
    inherit = doc_has_cs_font(doc)
    changes: list[str] = []
    for i, op in enumerate(ops, 1):
        name = str(op["op"]).strip().lower()
        try:
            if name == "replace_text":
                find = _text(op, "find")
                replace = _text(op, "replace", required=False)
                count = docx_replace(doc, loose_pattern(find), replace,
                                     None if _truthy(op.get("all"), True) else 1, inherit)
                if not count:
                    raise _not_found(find, docx_texts(doc))
                changes.append(f'แทนที่ "{find[:60]}" -> "{replace[:60]}" {count} แห่ง')
            elif name == "fill_placeholders":
                values = op.get("values")
                if not isinstance(values, dict) or not values:
                    raise ValueError('ต้องมี values: {"key": "ค่า"} สำหรับ {{key}}')
                info = fill_docx_placeholders(doc, values)
                if not info["filled"]:
                    raise ValueError("ไม่พบ {{placeholder}} ที่ตรงกับ values ในเอกสาร"
                                     + (f" (ที่มี: {', '.join(info['unfilled'][:20])})" if info["unfilled"] else ""))
                changes.append("เติมช่อง " + ", ".join(f"{k} x{v}" for k, v in info["filled"].items()))
                if info["not_in_document"]:
                    warnings.append("ไม่พบในเอกสาร: " + ", ".join(info["not_in_document"]))
                if info["unfilled"]:
                    warnings.append("ยังไม่ได้เติม: " + ", ".join(info["unfilled"][:20]))
            elif name == "append_markdown":
                count = render_markdown(doc, _text(op, "markdown", limit=MAX_MARKDOWN_CHARS), inherit=inherit)
                changes.append(f"ต่อท้ายเนื้อหา {count} บล็อก")
            elif name == "insert_after_heading":
                heading = _text(op, "heading")
                markdown = _text(op, "markdown", limit=MAX_MARKDOWN_CHARS)
                para, level = _find_heading(doc, heading)
                at = str(op.get("at") or "start").strip().lower()
                anchor = _section_end(doc, para, level) if at in ("end", "end_of_section") else para._p
                count = render_markdown(doc, markdown, anchor=anchor, inherit=inherit)
                where = "ท้ายหัวข้อ" if at in ("end", "end_of_section") else "ใต้หัวข้อ"
                changes.append(f'แทรก {count} บล็อก{where} "{para.text.strip()[:60]}"')
            elif name == "table_set_cell":
                table = _table_at(doc, op.get("table", 1))
                row, col = _int(op, "row"), _int(op, "col")
                try:
                    if row < 1 or col < 1:
                        raise IndexError
                    cell = table.cell(row - 1, col - 1)
                except IndexError:
                    raise ValueError(f"ไม่มีเซลล์แถว {row} คอลัมน์ {col} (ตารางมี {len(table.rows)} แถว {len(table.columns)} คอลัมน์)") from None
                _set_cell(cell, _text(op, "text", required=False), inherit)
                changes.append(f"ตาราง {op.get('table', 1)} แถว {row} คอลัมน์ {col} = \"{str(op.get('text') or '')[:60]}\"")
            elif name == "table_add_row":
                table = _table_at(doc, op.get("table", 1))
                cells = op.get("cells")
                if not isinstance(cells, list) or not cells:
                    raise ValueError("ต้องมี cells: [ค่าช่อง 1, ค่าช่อง 2, ...]")
                after = _int(op, "after_row") if op.get("after_row") not in (None, "") else None
                at = _docx_add_row(table, cells, after, inherit)
                changes.append(f"เพิ่มแถวในตาราง {op.get('table', 1)} (แถวที่ {at})")
            elif name == "add_comment":
                info = _docx_comment(doc, _text(op, "find"), _text(op, "text"),
                                     str(op.get("author") or "Qwen")[:60], str(op.get("initials") or "Q")[:9])
                changes.append(f'ใส่ความเห็นที่ "{info["paragraph"][:60]}" ({info["anchored"]})')
            else:
                raise ValueError("op ของ .docx ต้องเป็น replace_text, fill_placeholders, append_markdown, "
                                 "insert_after_heading, table_set_cell, table_add_row หรือ add_comment")
        except ValueError as exc:
            raise ValueError(f"operation #{i} ({name}): {exc} — ยังไม่ได้บันทึกไฟล์") from None
    return doc, changes, {}


# ----------------------------------------------------------------------------------- xlsx
def _xlsx_warnings(src: Path, names: list[str]) -> list[str]:
    lowered = [n.lower() for n in names]
    notes: list[str] = []
    shapes = False
    try:
        with zipfile.ZipFile(src) as archive:
            for name in names:
                low = name.lower()
                if low.startswith("xl/drawings/drawing") and low.endswith(".xml"):
                    data = archive.read(name)
                    if b"<xdr:sp" in data or b"<xdr:grpSp" in data or b"<xdr:cxnSp" in data:
                        shapes = True
                        break
    except (zipfile.BadZipFile, OSError, KeyError):
        pass
    if shapes:
        notes.append("ไฟล์มีรูปทรง/กล่องข้อความ ซึ่ง openpyxl จะไม่เก็บไว้ในไฟล์ใหม่")
    if any(n.startswith(("xl/slicers/", "xl/slicercaches/", "xl/timelines/")) for n in lowered):
        notes.append("slicer/timeline จะหายไปในไฟล์ใหม่")
    if any(n.startswith(("xl/ctrlprops/", "xl/activex/")) for n in lowered):
        notes.append("ปุ่ม/ตัวควบคุมฟอร์มจะหายไปในไฟล์ใหม่")
    if any(n.startswith("xl/pivottables/") for n in lowered):
        notes.append("มี PivotTable: เปิดใน Excel แล้วกด Refresh เพื่อให้ข้อมูลตรง")
    if any(n.startswith("xl/charts/") for n in lowered):
        notes.append("กราฟเดิมถูกเขียนใหม่ด้วย openpyxl รูปแบบบางส่วนอาจเปลี่ยน และไม่ขยายช่วงข้อมูลเอง")
    return notes


def _sheet(wb: Any, name: Any) -> Any:
    if name in (None, ""):
        return wb.active
    wanted = str(name).strip().strip("'")
    for ws in wb.worksheets:
        if ws.title == wanted:
            return ws
    for ws in wb.worksheets:
        if ws.title.casefold() == wanted.casefold():
            return ws
    raise ValueError(f"ไม่มีชีต '{wanted}' (มี: {', '.join(wb.sheetnames)})")


def _parse_ref(wb: Any, ref: Any, default_ws: Any) -> tuple[Any, str]:
    ref = str(ref).strip()
    ws = default_ws
    if "!" in ref:
        sheet_name, ref = ref.rsplit("!", 1)
        ws = _sheet(wb, sheet_name.strip().strip("'").replace("''", "'"))
    ref = ref.replace("$", "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}[1-9]\d{0,6}(:[A-Z]{1,3}[1-9]\d{0,6})?", ref):
        raise ValueError(f"อ้างอิงเซลล์ไม่ถูกต้อง: {ref} (ตัวอย่าง Sheet1!B3 หรือ B3:D5)")
    return ws, ref


def _cell_value(value: Any, where: str, warnings: list[str], text_cell: bool = False) -> tuple[Any, bool]:
    """-> (value, keep_as_text); text_cell: the cell is formatted as Text (@), so no number conversion"""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False), True
    if isinstance(value, str):
        if value.startswith("="):
            if DB._is_formula(value):
                return value, False
            if DB._UNSAFE_FORMULA.search(value):
                warnings.append(f"{where}: สูตรใช้ฟังก์ชันที่ไม่อนุญาต (เชื่อมต่อภายนอก) จึงเก็บเป็นข้อความ")
            return value, True
        stripped = value.strip()
        if not text_cell and _NUMBER.match(stripped):
            stripped = stripped.replace(",", "")
            return (float(stripped) if "." in stripped else int(stripped)), False
    return value, False


def _write_cell(ws: Any, row: int, col: int, value: Any, warnings: list[str]) -> Any:
    from openpyxl.cell.cell import MergedCell
    from openpyxl.utils import get_column_letter

    where = f"{ws.title}!{get_column_letter(col)}{row}"
    cell = ws.cell(row=row, column=col)
    if isinstance(cell, MergedCell):
        raise ValueError(f"เซลล์ {where} อยู่ในช่วงเซลล์ที่ผสาน (merged) ให้เขียนที่เซลล์มุมซ้ายบนของช่วงแทน")
    value, as_text = _cell_value(value, where, warnings, text_cell=cell.number_format == "@")
    cell.value = value
    if as_text and isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"
    return cell


def _last_row(ws: Any) -> int:
    rows = [r for (r, _c), cell in ws._cells.items() if cell.value not in (None, "")]
    return max(rows) if rows else 0


_SUM_TO = re.compile(r"(?i)(?<![!\w])SUM\(\s*\$?([A-Z]{1,3})\$?(\d+)\s*:\s*\$?([A-Z]{1,3})\$?(\d+)\s*\)")


def _total_row(ws: Any, last: int) -> int | None:
    """The last used row is a totals row when one of its formulas SUMs the rows right above it."""
    if last < 3:
        return None
    for (r, _c), cell in ws._cells.items():
        if r != last or not isinstance(cell.value, str) or not cell.value.startswith("="):
            continue
        for m in _SUM_TO.finditer(cell.value):
            if int(m.group(4)) == last - 1 and int(m.group(2)) < last - 1:
                return last
    return None


def _referenced_elsewhere(wb: Any, ws: Any) -> bool:
    """Formulas in other sheets or defined names that point into this sheet would not follow a moved row."""
    title = ws.title.casefold()
    for other in wb.worksheets:
        if other is ws:
            continue
        for cell in other._cells.values():
            if isinstance(cell.value, str) and cell.value.startswith("=") and title in cell.value.casefold():
                return True
    try:
        names = wb.defined_names.values() if hasattr(wb.defined_names, "values") else wb.defined_names.definedName
        for dn in names:
            if title in str(getattr(dn, "attr_text", "") or "").casefold():
                return True
    except Exception:
        return True
    return False


_REF_CELL = re.compile(r"^(\$?[A-Z]{1,3}\$?)(\d+)$")
_REF_ROW = re.compile(r"^(\$?)(\d+)$")


def _shift_ref(ref: str, total: int, n: int, grow_end: bool) -> str:
    """A same-sheet reference after n rows are inserted at row `total` (Excel's insert-rows rule:
    rows >= total move down, a range spanning the insertion grows). grow_end: in the totals row,
    a range ending right above it (B2:B9) also grows to take in the new rows."""
    parts = ref.split(":")
    parsed = [(_REF_CELL.match(p.upper()) or _REF_ROW.match(p)) for p in parts]
    if len(parts) > 2 or not all(parsed):
        return ref  # a defined name, a table or a whole column: no row to move
    before = [int(m.group(2)) for m in parsed]
    rows = list(before)
    if grow_end and len(rows) == 2 and rows[1] == total - 1 and rows[0] <= total - 1:
        rows[1] += n
    else:
        rows = [r + n if r >= total else r for r in rows]
    if rows == before:
        return ref
    return ":".join(f"{m.group(1)}{r}" for m, r in zip(parsed, rows))


def _shift_formula(formula: str, title: str, total: int, n: int, grow_end: bool) -> str:
    from openpyxl.formula.tokenizer import Token, Tokenizer

    out = ["="]
    for token in Tokenizer(formula).items:
        value = token.value
        if token.type == Token.OPERAND and token.subtype == Token.RANGE:
            sheet, bang, ref = value.rpartition("!")
            if not bang:
                value = _shift_ref(ref, total, n, grow_end)
            elif not sheet.startswith("[") and sheet.strip("'").replace("''", "'").casefold() == title.casefold():
                value = sheet + bang + _shift_ref(ref, total, n, grow_end)
        out.append(value)
    return "".join(out)


def _row_shifts(ws: Any, total: int, n: int) -> dict[tuple[int, int], str] | None:
    """The new text of every formula on the sheet once n rows go in at row `total`: references to
    the totals row (=B2/B$10, a summary =B10, =B10-C10 inside the totals row) follow it down.
    None when a formula cannot be parsed (the caller then appends below the totals row)."""
    shifted: dict[tuple[int, int], str] = {}
    for (r, c), cell in list(ws._cells.items()):
        # data_type "f": text that merely starts with "=" (a label, a refused WEBSERVICE) stays text
        if cell.data_type == "f" and isinstance(cell.value, str) and cell.value.startswith("="):
            try:
                shifted[(r, c)] = _shift_formula(cell.value, ws.title, total, n, grow_end=r == total)
            except Exception:
                return None
    return shifted


def xlsx_append_rows(wb: Any, ws: Any, rows: list[Any], warnings: list[str], changed: list[tuple[str, str]],
                     position: str = "auto", fill_formulas: bool = True) -> dict[str, Any]:
    """Append rows after the last used row. When that row is a totals row (=SUM(B2:B10) right
    below the data), the new rows go above it and its SUM ranges grow to include them."""
    from openpyxl.formula.translate import Translator
    from openpyxl.utils import get_column_letter

    rows = [list(r) if isinstance(r, (list, tuple)) else [r] for r in rows]
    if not rows:
        raise ValueError("rows ต้องมีอย่างน้อย 1 แถว")
    if len(rows) > DB.MAX_ROWS:
        raise ValueError(f"เพิ่มได้ไม่เกิน {DB.MAX_ROWS} แถว")
    n = len(rows)
    last = _last_row(ws)
    total = _total_row(ws, last) if position != "end" else None
    shifted = _row_shifts(ws, total, n) if total else None
    if total and (shifted is None or _referenced_elsewhere(wb, ws) or any(
            t.ref and int(re.sub(r"^[A-Z]+", "", t.ref.split(":")[-1])) >= total for t in ws.tables.values())):
        warnings.append(f"ชีต {ws.title}: มีสูตร/ตารางอื่นอ้างถึงแถวรวม จึงเพิ่มแถวต่อท้ายแถวรวมแทน")
        total = None
    first = (total or last + 1)
    width = max(ws.max_column, max(len(r) for r in rows))
    moved_to = None
    if total:
        ws.move_range(f"A{total}:{get_column_letter(width)}{total}", rows=n, cols=0, translate=False)
        moved_to = total + n
        # every formula on the sheet, the moved totals row included, now points where it did before
        for (r, c), text in shifted.items():
            cell = ws.cell(row=moved_to if r == total else r, column=c)
            if text != cell.value:
                cell.value = text
            elif cell.row != moved_to:
                continue
            changed.append((ws.title, cell.coordinate))
        for merged in list(ws.merged_cells.ranges):
            if merged.min_row == total and merged.max_row == total:
                merged.shift(0, n)
        if ws.row_dimensions[total].height is not None:
            ws.row_dimensions[moved_to].height = ws.row_dimensions[total].height
            ws.row_dimensions[total].height = None
    model_row = first - 1 if first - 1 >= 2 else None  # row 1 is usually the header: do not copy its look
    if model_row and not total and _total_row(ws, model_row):
        model_row = None  # appending below a totals row: neither its look nor its SUMs belong in data rows
    for offset, values in enumerate(rows):
        r = first + offset
        for c in range(1, width + 1):
            value = values[c - 1] if c - 1 < len(values) else None
            above = ws.cell(row=model_row, column=c) if model_row else None
            if value is None and fill_formulas and above is not None and isinstance(above.value, str) \
                    and above.value.startswith("=") and DB._is_formula(above.value):
                value = Translator(above.value, origin=above.coordinate).translate_formula(f"{get_column_letter(c)}{r}")
            if value is None and above is None:
                continue
            cell = ws.cell(row=r, column=c)
            if above is not None and above.has_style:
                cell._style = copy.copy(above._style)  # first, so a Text (@) column keeps IDs as text
            if value is not None:
                cell = _write_cell(ws, r, c, value, warnings)
                changed.append((ws.title, cell.coordinate))
    end = first + n - 1
    for table in ws.tables.values():
        start, _, stop = table.ref.partition(":")
        stop_row = int(re.sub(r"^[A-Z]+", "", stop or start))
        if stop_row == first - 1:
            table.ref = f"{start}:{re.sub(r'[0-9]+$', '', stop)}{end}"
            if table.autoFilter is not None:
                table.autoFilter.ref = table.ref
    if ws.auto_filter.ref:
        start, _, stop = ws.auto_filter.ref.partition(":")
        if stop and int(re.sub(r"^[A-Z]+", "", stop)) == first - 1:
            ws.auto_filter.ref = f"{start}:{re.sub(r'[0-9]+$', '', stop)}{end}"
    if getattr(ws, "_charts", None):
        warnings.append(f"ชีต {ws.title}: กราฟเดิมไม่ขยายช่วงข้อมูลให้เอง")
    return {"sheet": ws.title, "first_row": first, "last_row": end, "total_row": moved_to}


def xlsx_new_sheet(wb: Any, name: Any, rows: list[Any], warnings: list[str], changed: list[tuple[str, str]]) -> Any:
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    title = DB._sheet_name(str(name or ""), len(wb.sheetnames) + 1)
    if title.casefold() in {s.casefold() for s in wb.sheetnames}:
        raise ValueError(f"มีชีตชื่อ '{title}' อยู่แล้ว")
    rows = [list(r) if isinstance(r, (list, tuple)) else [r] for r in (rows or [])]
    if len(rows) > DB.MAX_ROWS:
        raise ValueError(f"ชีตใหม่ได้ไม่เกิน {DB.MAX_ROWS} แถว")
    ws = wb.create_sheet(title)
    for r, row in enumerate(rows, 1):
        for c, value in enumerate(row, 1):
            cell = _write_cell(ws, r, c, value, warnings)
            if r == 1:
                cell.font = Font(bold=True)
            changed.append((ws.title, cell.coordinate))
    if rows:
        widths: dict[int, int] = {}
        for row in rows[:200]:
            for j, value in enumerate(row, 1):
                widths[j] = max(widths.get(j, 8), min(60, len(str(value)) + 2))
        for j, w in widths.items():
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.freeze_panes = "A2"
    return ws


def _edit_xlsx(src: Path, ops: list[dict[str, Any]], warnings: list[str]) -> tuple[Any, list[str], dict[str, Any]]:
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter, range_boundaries

    try:
        wb = load_workbook(str(src))
    except Exception as exc:
        raise ValueError(f"เปิดไฟล์ Excel ไม่ได้: {exc}") from exc
    changed: list[tuple[str, str]] = []
    totals: list[tuple[str, str]] = []
    changes: list[str] = []
    for i, op in enumerate(ops, 1):
        name = str(op["op"]).strip().lower()
        try:
            if name == "set_cells":
                cells = op.get("cells") if op.get("cells") is not None else op.get("values")
                if not isinstance(cells, dict) or not cells:
                    raise ValueError('ต้องมี cells: {"Sheet1!B3": 100, "B4": "=SUM(B1:B3)"}')
                default_ws = _sheet(wb, op.get("sheet"))
                count = 0
                for ref, value in cells.items():
                    ws, rng = _parse_ref(wb, ref, default_ws)
                    min_col, min_row, max_col, max_row = range_boundaries(rng)
                    block = isinstance(value, list) and bool(value) and all(isinstance(r, list) for r in value)
                    if block:
                        # a 2-D list is written from the top-left cell; a range only bounds it
                        if ":" in rng and (len(value) > max_row - min_row + 1 or max(len(r) for r in value) > max_col - min_col + 1):
                            raise ValueError(f"ข้อมูลใหญ่กว่าช่วง {rng}")
                        targets = [(min_row + dr, min_col + dc, v) for dr, rv in enumerate(value) for dc, v in enumerate(rv)]
                    else:
                        # one value for a single cell, or the same value for every cell of a range
                        targets = [(r, c, value) for r in range(min_row, max_row + 1) for c in range(min_col, max_col + 1)]
                    if len(targets) > MAX_RANGE_CELLS:
                        raise ValueError(f"เขียนได้ไม่เกิน {MAX_RANGE_CELLS} เซลล์ต่อรายการ")
                    for r, c, v in targets:
                        _write_cell(ws, r, c, v, warnings)
                        changed.append((ws.title, f"{get_column_letter(c)}{r}"))
                        count += 1
                changes.append(f"ตั้งค่า {count} เซลล์")
            elif name == "append_rows":
                ws = _sheet(wb, op.get("sheet"))
                rows = op.get("rows")
                if not isinstance(rows, list) or not rows:
                    raise ValueError("ต้องมี rows: [[...], [...]]")
                info = xlsx_append_rows(wb, ws, rows, warnings, changed,
                                        position=str(op.get("position") or "auto").lower(),
                                        fill_formulas=_truthy(op.get("fill_formulas"), True))
                text = f"เพิ่ม {len(rows)} แถวในชีต {ws.title} (แถว {info['first_row']}-{info['last_row']})"
                if info["total_row"]:
                    text += f" และเลื่อนแถวรวมไปแถว {info['total_row']} พร้อมขยายช่วง SUM"
                    totals.extend((ws.title, c.coordinate) for c in ws[info["total_row"]] if c.value not in (None, ""))
                changes.append(text)
            elif name == "add_sheet":
                ws = xlsx_new_sheet(wb, op.get("name"), op.get("rows") or [], warnings, changed)
                changes.append(f"เพิ่มชีต {ws.title}")
            else:
                raise ValueError("op ของ .xlsx ต้องเป็น set_cells, append_rows หรือ add_sheet")
        except ValueError as exc:
            raise ValueError(f"operation #{i} ({name}): {exc} — ยังไม่ได้บันทึกไฟล์") from None
    return wb, changes, {"changed": changed, "totals": totals}


_LO_RECALC_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="ODFRecalcMode" oor:op="fuse"><value>0</value></prop></item>
</oor:items>
"""


def soffice(args: list[str], timeout: int) -> bool:
    """Run LibreOffice headless with a throw-away profile (always recalculating formulas on
    load). Returns False on timeout, after killing the whole process tree (soffice.exe starts
    soffice.bin)."""
    if not DB.SOFFICE.exists():
        return False
    profile = Path(tempfile.mkdtemp(prefix="qwen-lo-"))
    try:
        user = profile / "user"
        user.mkdir(parents=True)
        (user / "registrymodifications.xcu").write_text(_LO_RECALC_XCU, encoding="utf-8")
        cmd = [str(DB.SOFFICE), f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
               "--nologo", "--nodefault", "--nolockcheck", *args]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            return False
        return True
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def xlsx_recalc(path: Path, cells: list[tuple[str, str]], totals: list[tuple[str, str]] | None = None,
                timeout: int = RECALC_TIMEOUT) -> dict[str, Any]:
    """Recalculate a copy with LibreOffice and report the changed cells' results and any error cells."""
    from openpyxl import load_workbook

    if not DB.SOFFICE.exists():
        return {"status": "skipped", "reason": "ไม่พบ LibreOffice จึงไม่ได้ตรวจผลการคำนวณ"}
    work = Path(tempfile.mkdtemp(prefix="qwen-recalc-"))
    try:
        staged = work / "in" / "book.xlsx"
        staged.parent.mkdir()
        shutil.copyfile(path, staged)
        out = work / "out"
        out.mkdir()
        started = time.monotonic()
        finished = soffice(["--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", str(out), str(staged)], timeout)
        result = out / "book.xlsx"
        if not finished or not result.exists() or result.stat().st_size == 0:
            return {"status": "failed", "reason": "LibreOffice คำนวณไม่สำเร็จหรือหมดเวลา"}
        wb = load_workbook(str(result), data_only=True)
        values: dict[str, Any] = {}
        seen: set[tuple[str, str]] = set()
        for sheet, coord in list(totals or []) + list(cells):
            if (sheet, coord) in seen or len(values) >= 60 or sheet not in wb.sheetnames:
                continue
            seen.add((sheet, coord))
            values[f"{sheet}!{coord}"] = wb[sheet][coord].value
        errors: list[str] = []
        error_count = scanned = 0
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    scanned += 1
                    if isinstance(cell.value, str) and cell.value.strip() in EXCEL_ERRORS:
                        error_count += 1
                        if len(errors) < 30:
                            errors.append(f"{ws.title}!{cell.coordinate}: {cell.value.strip()}")
                if scanned > 500_000:
                    break
        report: dict[str, Any] = {
            "status": "checked",
            "engine": "LibreOffice",
            "seconds": round(time.monotonic() - started, 1),
            "values": values,
            "errors": errors,
            "error_count": error_count,
            "note": "คำนวณซ้ำด้วย LibreOffice (ไม่ใช่ Excel) ผลของบางสูตร เช่น dynamic array อาจต่างจาก Excel",
        }
        if totals:
            report["totals"] = {f"{s}!{c}": values.get(f"{s}!{c}") for s, c in totals}
        return report
    except Exception as exc:
        return {"status": "failed", "reason": f"ตรวจผลการคำนวณไม่สำเร็จ: {exc}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ----------------------------------------------------------------------------------- pptx
def _pptx_paragraphs(prs: Any, notes: bool = True):
    for index, slide in enumerate(prs.slides, 1):
        for p in slide._element.iter(_A_P):
            yield index, p
        if notes and slide.has_notes_slide:
            for p in slide.notes_slide._element.iter(_A_P):
                yield index, p


def pptx_replace(prs: Any, pattern: re.Pattern[str], repl: Any, limit: int | None = None, notes: bool = True) -> int:
    total = 0
    for _index, p in _pptx_paragraphs(prs, notes):
        if limit is not None and total >= limit:
            break
        segs = _pptx_segments(p)
        if not segs:
            continue
        n, modified, removed = _replace_segments(segs, pattern, repl, None if limit is None else limit - total)
        for s in modified:
            _set_a_text(s.el, s.new)
        for el in removed:
            if el.getparent() is not None:
                el.getparent().remove(el)
        total += n
    return total


def pptx_texts(prs: Any) -> list[str]:
    return ["".join(s.text for s in _pptx_segments(p)) for _i, p in _pptx_paragraphs(prs)]


def fill_pptx_placeholders(prs: Any, values: dict[str, Any], leftover: str | None = None) -> dict[str, Any]:
    values = {str(k).strip(): ("" if v is None else str(v)) for k, v in (values or {}).items()}
    counts: Counter[str] = Counter()

    def repl(m: re.Match[str]) -> str | None:
        key = m.group(1).strip()
        if key in values:
            counts[key] += 1
            return values[key]
        return None

    pptx_replace(prs, _PLACEHOLDER, repl)
    remaining = sorted({m.group(1).strip() for t in pptx_texts(prs) for m in _PLACEHOLDER.finditer(t)})
    if leftover is not None and remaining:
        pptx_replace(prs, _PLACEHOLDER, lambda m: leftover)
    return {"filled": dict(counts), "not_in_document": sorted(k for k in values if k not in counts), "unfilled": remaining}


def _ph_types(layout: Any) -> set[int]:
    types = set()
    for ph in layout.placeholders:
        try:
            types.add(int(ph.placeholder_format.type))
        except (TypeError, ValueError, AttributeError):
            pass
    return types


def pick_layout(prs: Any, want: Any = None, purpose: str = "content") -> Any:
    """A layout by 1-based number or name, or the first one fit for the purpose
    (content = title + body, title = title slide, title_only = title and nothing else)."""
    from pptx.enum.shapes import PP_PLACEHOLDER as PH

    layouts = list(prs.slide_layouts)
    if not layouts:
        raise ValueError("ไฟล์นี้ไม่มี slide layout")
    names = ", ".join(f"{i}. {l.name}" for i, l in enumerate(layouts, 1))
    if want not in (None, ""):
        text = str(want).strip()
        if text.isdigit():
            i = int(text)
            if 1 <= i <= len(layouts):
                return layouts[i - 1]
            raise ValueError(f"ไม่มี layout ที่ {i} (มี: {names})")
        for layout in layouts:
            if layout.name.casefold() == text.casefold():
                return layout
        for layout in layouts:
            if text.casefold() in layout.name.casefold():
                return layout
        raise ValueError(f"ไม่พบ layout '{text}' (มี: {names})")
    title_types = {int(PH.TITLE), int(PH.CENTER_TITLE)}
    body_types = {int(PH.BODY), int(PH.OBJECT)}
    content_like = body_types | {int(PH.PICTURE), int(PH.CHART), int(PH.TABLE), int(PH.MEDIA_CLIP), int(PH.ORG_CHART)}
    preferred = {"content": "title and content", "title": "title slide", "title_only": "title only"}[purpose]
    for layout in layouts:
        if layout.name.casefold() == preferred:
            return layout
    for layout in layouts:
        types = _ph_types(layout)
        if purpose == "content" and types & title_types and types & body_types:
            return layout
        if purpose == "title" and int(PH.CENTER_TITLE) in types:
            return layout
        if purpose == "title_only" and types & title_types and not types & content_like:
            return layout
    fallback = {"content": 1, "title": 0, "title_only": 5}[purpose]
    return layouts[min(fallback, len(layouts) - 1)]


def deck_font(prs: Any) -> str | None:
    """TH Sarabun New when the deck sets it on its runs (decks made by make_document do);
    otherwise new text inherits the template's own fonts."""
    counts: Counter[str] = Counter()
    for slide in list(prs.slides)[:10]:
        for rpr in slide._element.iter(_a("rPr")):
            latin = rpr.find(_a("latin"))
            if latin is not None and latin.get("typeface"):
                counts[latin.get("typeface")] += 1
    top = counts.most_common(1)
    return top[0][0] if top and top[0][0] == DB.THAI_FONT else None


def _body_placeholder(slide: Any) -> Any:
    from pptx.enum.shapes import PP_PLACEHOLDER as PH

    for wanted in ({int(PH.BODY), int(PH.OBJECT)}, {int(PH.SUBTITLE)}):
        for ph in slide.placeholders:
            try:
                if int(ph.placeholder_format.type) in wanted and ph.has_text_frame:
                    return ph
            except (TypeError, ValueError, AttributeError):
                continue
    return None


def _fill_text_frame(tf: Any, items: list[tuple[str, int]], font: str | None) -> None:
    tf.clear()
    for i, (text, level) in enumerate(items):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.level = max(0, min(level, 4))
        run = para.add_run()
        run.text = text
        if font:
            DB._pptx_font(run)


def pptx_add_slide(prs: Any, spec: dict[str, Any], font: str | None, layout: Any = None) -> dict[str, Any]:
    from pptx.util import Emu

    layout = layout if layout is not None else pick_layout(prs, spec.get("layout"), "content")
    slide = prs.slides.add_slide(layout)
    notes_out: list[str] = []
    title = str(spec.get("title") or "")
    title_ph = slide.shapes.title
    width, height = prs.slide_width, prs.slide_height
    if title:
        if title_ph is not None:
            title_ph.text = title
            if font:
                for p in title_ph.text_frame.paragraphs:
                    for r in p.runs:
                        DB._pptx_font(r)
        else:
            box = slide.shapes.add_textbox(Emu(int(width * 0.06)), Emu(int(height * 0.05)), Emu(int(width * 0.88)), Emu(int(height * 0.14)))
            _fill_text_frame(box.text_frame, [(title, 0)], font)
            notes_out.append("layout ไม่มีช่องหัวเรื่อง จึงใส่หัวเรื่องเป็นกล่องข้อความ")
    items = DB._bullet_items(spec.get("bullets") or [])
    body = _body_placeholder(slide)
    if items:
        if body is not None:
            _fill_text_frame(body.text_frame, items, font)
        else:
            box = slide.shapes.add_textbox(Emu(int(width * 0.06)), Emu(int(height * 0.22)), Emu(int(width * 0.88)), Emu(int(height * 0.7)))
            box.text_frame.word_wrap = True
            _fill_text_frame(box.text_frame, items, font)
            notes_out.append("layout ไม่มีช่องเนื้อหา จึงใส่ bullet เป็นกล่องข้อความ")
    if spec.get("notes"):
        slide.notes_slide.notes_text_frame.text = str(spec["notes"])[:MAX_TEXT_CHARS]
    used = [x._element for x in (title_ph, body if items else None) if x is not None]
    skipped = []
    for ph in slide.placeholders:
        if any(ph._element is el for el in used) or (ph.has_text_frame and ph.text_frame.text.strip()):
            continue
        skipped.append(f"{ph.name} ({ph.placeholder_format.type})")
    return {"slide": slide, "layout": layout.name, "skipped_placeholders": skipped, "notes": notes_out}


def _move_last_slide(prs: Any, position: int) -> None:
    lst = prs.slides._sldIdLst
    items = list(lst)
    if not 1 <= position <= len(items):
        raise ValueError(f"position ต้องอยู่ระหว่าง 1-{len(items)}")
    target = items[-1]  # the slide add_slide just appended
    lst.remove(target)
    lst.insert(position - 1, target)


def _edit_pptx(src: Path, ops: list[dict[str, Any]], warnings: list[str]) -> tuple[Any, list[str], dict[str, Any]]:
    from pptx import Presentation

    try:
        prs = Presentation(str(src))
    except Exception as exc:
        raise ValueError(f"เปิดไฟล์ PowerPoint ไม่ได้: {exc}") from exc
    font = deck_font(prs)
    changes: list[str] = []
    for i, op in enumerate(ops, 1):
        name = str(op["op"]).strip().lower()
        try:
            if name == "replace_text":
                find = _text(op, "find")
                replace = _text(op, "replace", required=False)
                count = pptx_replace(prs, loose_pattern(find), replace,
                                     None if _truthy(op.get("all"), True) else 1, _truthy(op.get("include_notes"), True))
                if not count:
                    raise _not_found(find, pptx_texts(prs))
                changes.append(f'แทนที่ "{find[:60]}" -> "{replace[:60]}" {count} แห่ง')
            elif name == "fill_placeholders":
                values = op.get("values")
                if not isinstance(values, dict) or not values:
                    raise ValueError('ต้องมี values: {"key": "ค่า"} สำหรับ {{key}}')
                info = fill_pptx_placeholders(prs, values)
                if not info["filled"]:
                    raise ValueError("ไม่พบ {{placeholder}} ที่ตรงกับ values ในไฟล์")
                changes.append("เติมช่อง " + ", ".join(f"{k} x{v}" for k, v in info["filled"].items()))
                if info["unfilled"]:
                    warnings.append("ยังไม่ได้เติม: " + ", ".join(info["unfilled"][:20]))
            elif name == "add_slide":
                if len(prs.slides) >= DB.MAX_SLIDES * 3:
                    raise ValueError("ไฟล์มีสไลด์มากเกินไป")
                info = pptx_add_slide(prs, op, font)
                where = len(prs.slides)
                if op.get("position") not in (None, ""):
                    where = _int(op, "position")
                    _move_last_slide(prs, where)
                text = f'เพิ่มสไลด์ที่ {where} "{str(op.get("title") or "")[:60]}" (layout: {info["layout"]})'
                changes.append(text)
                warnings.extend(info["notes"])
                if info["skipped_placeholders"]:
                    warnings.append(f"สไลด์ {where}: ช่องที่ไม่ได้เติม: " + ", ".join(info["skipped_placeholders"]))
            elif name == "set_notes":
                index = _int(op, "slide")
                if not 1 <= index <= len(prs.slides):
                    raise ValueError(f"ไม่มีสไลด์ที่ {index} (มี {len(prs.slides)} สไลด์)")
                prs.slides[index - 1].notes_slide.notes_text_frame.text = _text(op, "notes", required=False)
                changes.append(f"ตั้งโน้ตผู้บรรยายสไลด์ {index}")
            else:
                raise ValueError("op ของ .pptx ต้องเป็น replace_text, fill_placeholders, add_slide หรือ set_notes")
        except ValueError as exc:
            raise ValueError(f"operation #{i} ({name}): {exc} — ยังไม่ได้บันทึกไฟล์") from None
    return prs, changes, {}


# ----------------------------------------------------------------------------------- entry
def _backup(src: Path, out_dir: Path, source: str, principal: str) -> dict[str, Any]:
    try:
        import file_history
    except ImportError:
        file_history = None
    if file_history is not None and hasattr(file_history, "backup"):
        limit = getattr(file_history, "MAX_BACKUP_BYTES", None)
        if isinstance(limit, int) and src.stat().st_size > limit:
            # file_history records such a file without a copy; an in-place edit would then be unrecoverable
            raise ValueError(f"ไฟล์ใหญ่เกิน {limit // (1024 * 1024)} MB จึงสำรองไม่ได้ ให้แก้แบบสร้างไฟล์ใหม่ (in_place=false)")
        try:
            # None lets file_history take the caller the gateway recorded for this request
            change_id = file_history.backup(src, source or None, principal or None, "edit_document")
        except Exception as exc:
            raise ValueError(f"สำรองไฟล์เดิมไม่สำเร็จ จึงไม่แก้ทับไฟล์ ({exc})") from exc
        return {"via": "file_history", "change_id": change_id}
    # file_history is not installed yet: keep a plain copy in the project outputs instead
    folder = out_dir / "_backups"
    folder.mkdir(parents=True, exist_ok=True)
    dest = DB._reserve(folder / f"{DB._safe_name(src.stem, 'file')[:80]}-before-{time.strftime('%Y%m%d-%H%M%S')}{src.suffix.lower()}")
    shutil.copyfile(src, dest)
    return {"via": "copy", "path": str(dest)}


def _record(backup: dict[str, Any], path: Path | None, error: str = "") -> None:
    """Tell file_history the write landed (so the change can be reverted) or failed."""
    if backup.get("via") != "file_history":
        return
    try:
        import file_history

        if error:
            hook = getattr(file_history, "mark_failed", None)
            if callable(hook):
                hook(backup["change_id"], error)
        else:
            hook = getattr(file_history, "record_after", None)
            if callable(hook):
                hook(backup["change_id"], path)
    except Exception:
        pass


def edit(src: Path, out_dir: Path, operations: Any, output_name: str = "", in_place: bool = False,
         source: str = "", principal: str = "") -> dict[str, Any]:
    src = Path(src)
    in_place = _truthy(in_place, False)  # the gateway passes raw JSON: "false" must stay false
    check_not_macro(src)
    kind = EDIT_KINDS.get(src.suffix.lower())
    if kind is None:
        raise ValueError(f"แก้ได้เฉพาะ .docx, .xlsx, .pptx (ไฟล์นี้เป็น {src.suffix or 'ไม่มีนามสกุล'}) — "
                         "ไฟล์ .doc/.xls/.ppt แบบเก่าให้บันทึกเป็นแบบใหม่ก่อน")
    if not src.is_file():
        raise FileNotFoundError(f"ไม่พบไฟล์: {src}")
    if src.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError(f"ไฟล์ใหญ่เกิน {MAX_SOURCE_BYTES // (1024 * 1024)} MB")
    ops = _operations(operations)
    names = zip_names(src)
    started = time.monotonic()
    warnings: list[str] = []
    if kind == "xlsx":
        warnings.extend(_xlsx_warnings(src, names))
    editor = {"docx": _edit_docx, "xlsx": _edit_xlsx, "pptx": _edit_pptx}[kind]
    obj, changes, extra = editor(src, ops, warnings)

    backup: dict[str, Any] | None = None
    if in_place:
        backup = _backup(src, out_dir, source, principal)
        tmp = DB._reserve(src.with_name(f"~qwen-{DB._safe_name(src.stem, 'file')[:60]}{src.suffix.lower()}"))
        try:
            obj.save(str(tmp))
            os.replace(tmp, src)
        except PermissionError:
            tmp.unlink(missing_ok=True)
            _record(backup, None, "file locked")
            raise ValueError("ไฟล์ถูกเปิดอยู่ในโปรแกรมอื่น (เช่น Word/Excel) ปิดไฟล์ก่อนแล้วลองใหม่ หรือแก้แบบไม่ in_place") from None
        except BaseException as exc:
            tmp.unlink(missing_ok=True)
            _record(backup, None, str(exc) or type(exc).__name__)
            raise
        target = src
        _record(backup, target)
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        name = str(output_name or "").strip() or f"{src.stem}-edit-{time.strftime('%H%M%S')}"
        if name.lower().endswith(src.suffix.lower()):
            name = name[: -len(src.suffix)]
        target = DB._reserve(out_dir / f"{DB._safe_name(name, 'edited')}{src.suffix.lower()}")
        try:
            obj.save(str(target))
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    result: dict[str, Any] = {
        "status": "edited",
        "format": kind,
        "source": str(src),
        "path": str(target),
        "in_place": bool(in_place),
        "changes": changes,
    }
    if warnings:
        result["warnings"] = warnings
    if backup:
        result["backup"] = backup
    if kind == "xlsx":
        result["recalc"] = xlsx_recalc(target, extra.get("changed", []), extra.get("totals"))
    telegram = DB._telegram_copy(target)
    result.update({
        "bytes": target.stat().st_size,
        "seconds": round(time.monotonic() - started, 2),
        "telegram_attachment": str(telegram) if telegram else None,
        "delivery": "Tell the user what changed and the path. On Telegram, end the reply with a line of its own: MEDIA:<telegram_attachment>. To show pages, call render_document_preview on the new file.",
    })
    return result


# ----------------------------------------------------------------------------------- preview
def convert_to_pdf(src: Path, work: Path, timeout: int = CONVERT_TIMEOUT) -> tuple[Path, str]:
    """Any Office file -> PDF inside `work` (LibreOffice; Word for Word files if that fails)."""
    suffix = src.suffix.lower()
    if suffix == ".pdf":
        return src, "none"
    staged = work / f"input{suffix}"  # an ASCII name: soffice and COM both dislike some paths
    shutil.copyfile(src, staged)
    out = work / "pdf"
    out.mkdir(exist_ok=True)
    if soffice(["--convert-to", "pdf", "--outdir", str(out), str(staged)], timeout):
        pdf = out / "input.pdf"
        if pdf.exists() and pdf.stat().st_size > 0:
            return pdf, "libreoffice"
    if suffix in {".docx", ".doc", ".rtf", ".odt"}:
        pdf = DB._pdf_word(staged, work / "word.pdf")
        if pdf is not None:
            return pdf, "word"
    raise RuntimeError("แปลงเอกสารเป็น PDF ไม่สำเร็จ (LibreOffice/Word) หรือหมดเวลา")


def render_preview(src: Path, out_dir: Path, pages: str = "1-3", dpi: int = 110) -> dict[str, Any]:
    import pdf_tools as PT

    src = Path(src)
    check_not_macro(src)
    suffix = src.suffix.lower()
    if suffix not in PREVIEW_SUFFIXES:
        raise ValueError("แสดงภาพได้เฉพาะ " + ", ".join(sorted(PREVIEW_SUFFIXES)))
    if not src.is_file():
        raise FileNotFoundError(f"ไม่พบไฟล์: {src}")
    if src.stat().st_size > MAX_SOURCE_BYTES * 4:
        raise ValueError("ไฟล์ใหญ่เกินไปสำหรับการแสดงภาพ")
    if suffix in {".docx", ".xlsx", ".pptx"}:
        zip_names(src)
    try:
        dpi = int(dpi or 110)
    except (TypeError, ValueError):
        dpi = 110
    dpi = max(50, min(dpi, 200))
    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="qwen-preview-"))
    doc = None
    notes: list[str] = []
    try:
        pdf, engine = convert_to_pdf(src, work)
        doc = PT.open_pdf(pdf)
        count = doc.page_count
        wanted = PT.page_list(pages or "1-3", count)
        if len(wanted) > MAX_PREVIEW_PAGES:
            notes.append(f"แสดงได้ครั้งละ {MAX_PREVIEW_PAGES} หน้า จึงแสดงหน้า {wanted[0]}-{wanted[MAX_PREVIEW_PAGES - 1]} ก่อน")
            wanted = wanted[:MAX_PREVIEW_PAGES]
        rendered = PT.render_pages(doc, wanted, dpi, "", out_dir / "previews", src.stem)
    finally:
        if doc is not None:
            doc.close()
        shutil.rmtree(work, ignore_errors=True)
    result: dict[str, Any] = {
        "status": "rendered",
        "source": str(src),
        "page_count": count,
        "pages": [r["page"] for r in rendered],
        "images": rendered,
        "converted_with": engine,
        "seconds": round(time.monotonic() - started, 2),
        "media": [f"MEDIA:{r['telegram_attachment']}" for r in rendered if r.get("telegram_attachment")],
        "delivery": "On Telegram, end the reply with the media lines, one per line, exactly as given. Elsewhere tell the user the image paths.",
    }
    if notes:
        result["notes"] = notes
    return result
