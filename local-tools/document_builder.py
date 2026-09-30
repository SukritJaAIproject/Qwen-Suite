"""Build Word, Excel, PowerPoint and PDF files from a compact spec (added 2026-09-30).

The model writes markdown (docx/pdf) or small JSON lists (xlsx sheets, pptx slides), which is
far cheaper at 1-2 tok/s than verbose document JSON. Thai text uses TH Sarabun New for the
complex-script font slot (w:cs / a:cs) as well, or Word and PowerPoint fall back to a default
font for Thai; bold/italic are set for the complex-script slot too (w:bCs / w:iCs), or Thai
stays regular. PDFs are made from the docx by LibreOffice (isolated profile, timeout) or, if
that fails, by Word through COM in a child process with a timeout.

Files are never overwritten: the name is reserved atomically (O_EXCL), and an existing name
gets -2, -3, ... appended, so two requests running at once cannot land on the same file.

template="<name>" builds from a saved house template instead (templates.py): its styles,
header/footer and layouts are kept, {{placeholders}} are filled, and the content is added.
With template empty the behaviour is exactly the one above.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

THAI_FONT = "TH Sarabun New"
SOFFICE = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")
# <install root>\openclaw-workspace\outputs (this file is <install root>\local-tools\...)
TELEGRAM_OUTPUTS = Path(__file__).resolve().parent.parent / "openclaw-workspace" / "outputs"
MAX_ROWS = 20000
MAX_SLIDES = 60
WORD_TIMEOUT = 120
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
             *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


def _safe_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "").strip()).strip(" .")
    cleaned = (cleaned or fallback)[:120]
    # Windows device names (COM3, NUL, aux.txt ...) open the device instead of a file.
    if cleaned.split(".")[0].strip().upper() in _RESERVED:
        cleaned = "_" + cleaned
    return cleaned


def _sheet_name(name: str, index: int) -> str:
    cleaned = re.sub(r"[\\/?*\[\]:]", "_", (name or "").strip()).strip("'")
    return (cleaned or f"Sheet{index}")[:31]


def _reserve(path: Path) -> Path:
    """Create an empty file under a free name and return it (atomic: O_CREAT | O_EXCL)."""
    for n in range(1, 1000):
        candidate = path if n == 1 else path.with_name(f"{path.stem}-{n}{path.suffix}")
        try:
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError(f"too many files named {path.name}")


# ----------------------------------------------------------------------------------- markdown
_INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)")


def _inline_runs(text: str) -> list[tuple[str, str]]:
    """'**bold** and *it* `code`' -> [(style, text)] with style in {'', 'b', 'i', 'c'}."""
    runs: list[tuple[str, str]] = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            runs.append(("b", part[2:-2]))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            runs.append(("c", part[1:-1]))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            runs.append(("i", part[1:-1]))
        else:
            runs.append(("", part))
    return runs


def _blocks(markdown: str) -> list[tuple[str, Any]]:
    """Very small markdown reader: headings, bullets, numbered items, tables, rules, paragraphs."""
    blocks: list[tuple[str, Any]] = []
    para: list[str] = []
    table: list[list[str]] = []

    def flush_para() -> None:
        if para:
            blocks.append(("p", " ".join(s.strip() for s in para)))
            para.clear()

    def flush_table() -> None:
        if table:
            blocks.append(("table", [row[:] for row in table]))
            table.clear()

    for raw in (markdown or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1:
            flush_para()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c or "--") for c in cells):
                continue  # separator row
            table.append(cells)
            continue
        flush_table()
        if not stripped:
            flush_para()
            continue
        # a rule must be checked before bullets: "---" / "***" would otherwise look like one
        if re.fullmatch(r"(-\s*){3,}|(\*\s*){3,}|(_\s*){3,}", stripped):
            flush_para()
            blocks.append(("hr", None))
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            flush_para()
            blocks.append((f"h{len(heading.group(1))}", heading.group(2).strip()))
            continue
        bullet = re.match(r"^([-*+])\s+(.*)$", stripped)
        if bullet:
            flush_para()
            level = min((len(line) - len(line.lstrip(" "))) // 2, 2)
            blocks.append(("ul", (level, bullet.group(2))))
            continue
        numbered = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if numbered:
            flush_para()
            blocks.append(("ol", (numbered.group(1), numbered.group(2))))
            continue
        para.append(stripped)
    flush_para()
    flush_table()
    return blocks


# ----------------------------------------------------------------------------------- docx
def _docx_fonts(run_or_style_element, size_pt: float | None = None) -> None:
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    rpr = run_or_style_element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attr), THAI_FONT)
    # Theme font references win over explicit names (the Heading styles use them), so drop them.
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if fonts.get(qn(attr)) is not None:
            del fonts.attrib[qn(attr)]
    if size_pt:
        for tag in ("w:sz", "w:szCs"):
            el = rpr.find(qn(tag))
            if el is None:
                el = OxmlElement(tag)
                rpr.append(el)
            el.set(qn("w:val"), str(int(size_pt * 2)))


def _add_runs(paragraph, text: str) -> None:
    for style, part in _inline_runs(text):
        run = paragraph.add_run(part)
        run.bold = style == "b" or None
        run.italic = style == "i" or None
        # Thai is a complex script: Word/LibreOffice read w:bCs / w:iCs for it, not w:b / w:i.
        run.font.cs_bold = run.bold
        run.font.cs_italic = run.italic
        _docx_fonts(run._r)
        if style == "c":
            from docx.shared import RGBColor
            run.font.color.rgb = RGBColor(0x33, 0x33, 0x99)


def build_docx(path: Path, title: str, markdown: str) -> None:
    from docx import Document
    from docx.shared import Cm, Pt

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.size = Pt(16)
    _docx_fonts(normal.element, 16)
    for name, size in (("Title", 26), ("Heading 1", 22), ("Heading 2", 19), ("Heading 3", 17), ("Heading 4", 16)):
        try:
            _docx_fonts(doc.styles[name].element, size)
        except KeyError:
            pass
    if title:
        doc.add_heading(title, level=0)
    for kind, value in _blocks(markdown):
        if kind in ("h1", "h2", "h3", "h4"):
            doc.add_heading(value, level=int(kind[1]))
        elif kind == "p":
            _add_runs(doc.add_paragraph(), value)
        elif kind == "ul":
            level, text = value
            style = "List Bullet" if level == 0 else f"List Bullet {level + 1}"
            try:
                para = doc.add_paragraph(style=style)
            except KeyError:
                para = doc.add_paragraph(style="List Bullet")
            _add_runs(para, text)
        elif kind == "ol":
            # The markdown's own number, as text: Word's shared "List Number" list would keep
            # counting across headings (a second list would start at 3).
            number, text = value
            para = doc.add_paragraph()
            para.paragraph_format.left_indent = Cm(0.9)
            para.paragraph_format.first_line_indent = Cm(-0.6)
            _add_runs(para, f"{number}. {text}")
        elif kind == "hr":
            doc.add_paragraph("")
        elif kind == "table":
            width = max(len(r) for r in value)
            table = doc.add_table(rows=0, cols=width)
            table.style = "Table Grid"
            for i, row in enumerate(value):
                cells = table.add_row().cells
                for j in range(width):
                    para = cells[j].paragraphs[0]
                    text = row[j] if j < len(row) else ""
                    _add_runs(para, f"**{text}**" if i == 0 and text else text)
    doc.save(str(path))


# ----------------------------------------------------------------------------------- xlsx
# A cell is a formula only when it looks like one (=SUM(...), =A1*2, =(B2+C2)/2, =-A1); other
# text that starts with "=" (e.g. "=== Summary ===") stays text. Functions that reach outside
# the workbook are never written as formulas.
_FORMULA = re.compile(r"^=[A-Za-z_$(\-+\d]")
_UNSAFE_FORMULA = re.compile(r"(?i)\b(WEBSERVICE|FILTERXML|HYPERLINK|CALL|REGISTER(\.ID)?|EXEC|RTD|DDE\w*|IMPORT\w*)\s*\(|\|")


def _is_formula(value: Any) -> bool:
    return isinstance(value, str) and bool(_FORMULA.match(value)) and not _UNSAFE_FORMULA.search(value)


def _xlsx_chart(ws, rows: list[list[Any]], chart_spec: Any, title: str, notes: list[str]) -> None:
    """A native chart beside rows written from A1 (row 1 = series names, column x_col = categories)."""
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.utils import get_column_letter

    if not (isinstance(chart_spec, dict) and len(rows) >= 2):
        return
    width = max(len(r) for r in rows)
    x_col = int(chart_spec.get("x_col") or 1)
    y_cols = [int(c) for c in (chart_spec.get("y_cols") or [c for c in range(1, width + 1) if c != x_col])]
    y_cols = [c for c in y_cols if 1 <= c <= width and c != x_col]
    if width < 2 or not 1 <= x_col <= width or not y_cols:
        notes.append(f"sheet {ws.title}: chart skipped (needs a category column and at least one value column)")
        return
    kind = str(chart_spec.get("type") or "bar").lower()
    chart = {"line": LineChart, "pie": PieChart}.get(kind, BarChart)()
    chart.title = str(chart_spec.get("title") or title or ws.title)
    last = len(rows)
    for col in y_cols:
        chart.add_data(Reference(ws, min_col=col, min_row=1, max_row=last), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=x_col, min_row=2, max_row=last))
    ws.add_chart(chart, f"{get_column_letter(width + 2)}2")


def build_xlsx(path: Path, title: str, sheets: list[dict[str, Any]]) -> dict[str, Any]:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    if not sheets:
        raise ValueError("xlsx needs sheets: [{name, rows: [[...], ...], chart?}]")
    wb = Workbook()
    wb.remove(wb.active)
    total = 0
    notes: list[str] = []
    for index, sheet in enumerate(sheets, 1):
        if not isinstance(sheet, dict):
            raise ValueError("each sheet must be an object {name, rows, chart?}")
        ws = wb.create_sheet(_sheet_name(str(sheet.get("name") or ""), index))
        rows = sheet.get("rows") or []
        if not isinstance(rows, list):
            raise ValueError("sheet rows must be a list of lists")
        rows = [list(r) if isinstance(r, (list, tuple)) else [r] for r in rows]
        for r, row in enumerate(rows, 1):
            total += 1
            if total > MAX_ROWS:
                raise ValueError(f"more than {MAX_ROWS} rows")
            for c, value in enumerate(row, 1):
                if isinstance(value, (dict, list, tuple)):
                    value = json.dumps(value, ensure_ascii=False)
                cell = ws.cell(row=r, column=c, value=value)
                if isinstance(value, str) and value.startswith("=") and not _is_formula(value):
                    cell.data_type = "s"  # keep as text
        if rows:
            for cell in ws[1]:
                cell.font = Font(bold=True)
            widths: dict[int, int] = {}
            for row in rows[:200]:
                for j, value in enumerate(row, 1):
                    widths[j] = max(widths.get(j, 8), min(60, len(str(value)) + 2))
            for j, w in widths.items():
                ws.column_dimensions[get_column_letter(j)].width = w
            ws.freeze_panes = "A2"
        _xlsx_chart(ws, rows, sheet.get("chart"), title, notes)
    wb.save(str(path))
    result: dict[str, Any] = {"sheets": len(sheets), "rows": total}
    if notes:
        result["notes"] = notes
    return result


# ----------------------------------------------------------------------------------- pptx
def _pptx_font(run, size: int | None = None) -> None:
    from pptx.oxml.ns import qn
    from pptx.util import Pt

    run.font.name = THAI_FONT
    if size:
        run.font.size = Pt(size)
    rpr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        el = rpr.find(qn(tag))
        if el is None:
            el = rpr.makeelement(qn(tag), {})
            rpr.append(el)
        el.set("typeface", THAI_FONT)


def _widen_template(prs, ratio: float) -> None:
    """The default template is 4:3; stretch the master's and layouts' placeholders to 16:9."""
    shapes = list(prs.slide_master.placeholders)
    for layout in prs.slide_layouts:
        shapes.extend(layout.placeholders)
    for shape in shapes:
        if shape.left is not None and shape.width is not None:
            shape.left, shape.width = int(shape.left * ratio), int(shape.width * ratio)


def _bullet_items(raw: Any) -> list[tuple[str, int]]:
    if isinstance(raw, str):
        raw = [line for line in raw.splitlines() if line.strip()]
    if not isinstance(raw, list):
        raise ValueError("slide bullets must be a list of strings or {text, level}")
    items = []
    for item in raw:
        if isinstance(item, dict):
            items.append((str(item.get("text") or ""), int(item.get("level") or 0)))
        else:
            items.append((str(item), 0))
    return items


def _chart_data(spec: dict[str, Any]):
    from pptx.chart.data import CategoryChartData

    categories = [str(c) for c in spec.get("categories") or []]
    series = spec.get("series") or []
    if not categories or not isinstance(series, list) or not series:
        raise ValueError("a slide chart needs categories [..] and series [{name, values [..]}]")
    data = CategoryChartData()
    data.categories = categories
    for s in series:
        values = s.get("values") if isinstance(s, dict) else None
        if not isinstance(values, list) or len(values) != len(categories):
            raise ValueError(f"each chart series needs {len(categories)} values, one per category")
        data.add_series(str(s.get("name") or ""), [float(v) for v in values])
    return data


def build_pptx(path: Path, title: str, slides: list[dict[str, Any]]) -> dict[str, Any]:
    from pptx import Presentation
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    if not slides:
        raise ValueError("pptx needs slides: [{title, bullets: [...], notes?, chart?}]")
    if len(slides) > MAX_SLIDES:
        raise ValueError(f"more than {MAX_SLIDES} slides")
    prs = Presentation()
    old_width = prs.slide_width
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    _widen_template(prs, prs.slide_width / old_width)
    if title:
        s = prs.slides.add_slide(prs.slide_layouts[0])
        s.shapes.title.text = title
        for p in s.shapes.title.text_frame.paragraphs:
            for r in p.runs:
                _pptx_font(r, 44)
        if len(s.placeholders) > 1:
            s.placeholders[1].text = time.strftime("%d/%m/%Y")
    for spec in slides:
        if not isinstance(spec, dict):
            raise ValueError("each slide must be an object {title, bullets, notes?, chart?}")
        chart_spec = spec.get("chart") if isinstance(spec.get("chart"), dict) else None
        chart_data = _chart_data(chart_spec) if chart_spec else None
        items = [] if chart_spec else _bullet_items(spec.get("bullets") or [])
        layout = prs.slide_layouts[5 if chart_spec else 1]
        s = prs.slides.add_slide(layout)
        s.shapes.title.text = str(spec.get("title") or "")
        for p in s.shapes.title.text_frame.paragraphs:
            for r in p.runs:
                _pptx_font(r, 36)
        if chart_spec:
            kind = {"line": XL_CHART_TYPE.LINE_MARKERS, "pie": XL_CHART_TYPE.PIE}.get(str(chart_spec.get("type") or "bar").lower(), XL_CHART_TYPE.COLUMN_CLUSTERED)
            s.shapes.add_chart(kind, Inches(0.8), Inches(1.6), Inches(11.7), Inches(5.4), chart_data)
        else:
            body = s.placeholders[1].text_frame
            body.clear()
            for i, (text, level) in enumerate(items):
                para = body.paragraphs[0] if i == 0 else body.add_paragraph()
                para.level = max(0, min(level, 4))
                run = para.add_run()
                run.text = text
                _pptx_font(run, 24 if level == 0 else 20)
        notes = spec.get("notes")
        if notes:
            s.notes_slide.notes_text_frame.text = str(notes)
    prs.save(str(path))
    return {"slides": len(prs.slides)}


# ----------------------------------------------------------------------------------- pdf
def _pdf_libreoffice(docx: Path, out_dir: Path, timeout: int = 180) -> Path | None:
    if not SOFFICE.exists():
        return None
    profile = Path(tempfile.mkdtemp(prefix="qwen-lo-"))
    try:
        cmd = [str(SOFFICE), f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
               "--convert-to", "pdf", "--outdir", str(out_dir), str(docx)]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            return None
        pdf = out_dir / (docx.stem + ".pdf")
        return pdf if pdf.exists() and pdf.stat().st_size > 0 else None
    finally:
        shutil.rmtree(profile, ignore_errors=True)


# Word runs in a child process: a sign-in/activation/repair dialog would otherwise block the
# tool server forever. The child records the WINWORD.EXE it started, so a timeout kills only
# that automation instance, never a Word window the user has open.
_WORD_CHILD = r"""
import json, sys, psutil, pythoncom, win32com.client
docx, pdf, pidfile = sys.argv[1:4]
def words():
    return {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "winword.exe"}
before = words()
pythoncom.CoInitialize()
word = win32com.client.DispatchEx("Word.Application")
open(pidfile, "w").write(json.dumps(sorted(words() - before)))
try:
    word.Visible = False
    word.DisplayAlerts = 0
    d = word.Documents.Open(docx, ReadOnly=True, AddToRecentFiles=False)
    d.SaveAs2(pdf, FileFormat=17)
    d.Close(False)
finally:
    word.Quit()
"""


def _pdf_word(docx: Path, pdf: Path, timeout: int = WORD_TIMEOUT) -> Path | None:
    try:
        import psutil
    except ImportError:
        return None
    pidfile = pdf.with_suffix(".wordpid")
    proc = subprocess.Popen([sys.executable, "-c", _WORD_CHILD, str(docx), str(pdf), str(pidfile)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        try:
            for pid in json.loads(pidfile.read_text()):
                p = psutil.Process(pid)
                if p.name().lower() == "winword.exe" and "automation" in " ".join(p.cmdline()).lower():
                    p.kill()
        except (OSError, ValueError, psutil.Error):
            pass
        return None
    return pdf if pdf.exists() and pdf.stat().st_size > 0 else None


def build_pdf(path: Path, title: str, markdown: str) -> str:
    work = Path(tempfile.mkdtemp(prefix="qwen-pdf-"))
    try:
        docx = work / "document.docx"
        build_docx(docx, title, markdown)
        made = _pdf_libreoffice(docx, work)
        engine = "libreoffice"
        if made is None:
            made = _pdf_word(docx, work / "document.pdf")
            engine = "word"
        if made is None:
            raise RuntimeError("PDF conversion failed (LibreOffice and Word); the .docx can still be made with format=docx")
        shutil.copyfile(made, path)  # onto our own reserved placeholder
        return engine
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ----------------------------------------------------------------------------------- entry
def _telegram_copy(target: Path) -> Path | None:
    """A copy inside the OpenClaw workspace, so Telegram can attach it (workspace-only files)."""
    telegram = None
    try:
        TELEGRAM_OUTPUTS.mkdir(parents=True, exist_ok=True)
        telegram = _reserve(TELEGRAM_OUTPUTS / target.name)
        shutil.copyfile(target, telegram)
    except OSError:
        if telegram is not None:
            try:
                telegram.unlink()
            except OSError:
                pass
        telegram = None
    return telegram


def make(out_dir: Path, fmt: str, title: str = "", content_markdown: str = "", sheets: Any = None,
         slides: Any = None, output_name: str = "", template: str = "", placeholders: Any = None) -> dict[str, Any]:
    fmt = (fmt or "").strip().lower().lstrip(".")
    if fmt not in {"docx", "xlsx", "pptx", "pdf"}:
        raise ValueError("format must be docx, xlsx, pptx or pdf")
    template = (template or "").strip()
    if isinstance(placeholders, str) and placeholders.strip():
        try:
            placeholders = json.loads(placeholders)
        except json.JSONDecodeError as exc:
            raise ValueError(f"placeholders must be a JSON object: {exc}") from exc
    if placeholders is not None and placeholders != "" and not isinstance(placeholders, dict):
        raise ValueError('placeholders must be an object {"key": "value"} for {{key}} in the template')
    if template:
        # a template can carry the whole document (a memo form); its placeholders are enough
        if fmt in {"docx", "pdf"} and not (content_markdown or "").strip() and not placeholders and not title:
            raise ValueError(f"{fmt} from a template needs content_markdown, title or placeholders")
        import templates as TEMPLATES

        TEMPLATES.resolve(template)  # fail before a file name is reserved
    elif fmt in {"docx", "pdf"} and not (content_markdown or "").strip():
        raise ValueError(f"{fmt} needs content_markdown (# headings, - bullets, | tables |, **bold**)")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = output_name or title
    if name.lower().endswith("." + fmt):
        name = name[: -(len(fmt) + 1)]
    name = _safe_name(name, f"{template or 'document'}-{time.strftime('%Y%m%d-%H%M%S')}")
    target = _reserve(out_dir / f"{name}.{fmt}")
    started = time.monotonic()
    details: dict[str, Any] = {}
    try:
        if template:
            details.update(TEMPLATES.build_document(
                target, fmt, template, title=title, content_markdown=content_markdown,
                sheets=sheets if isinstance(sheets, list) else [],
                slides=slides if isinstance(slides, list) else [],
                placeholders=placeholders or {},
            ))
        elif fmt == "docx":
            build_docx(target, title, content_markdown)
        elif fmt == "pdf":
            details["engine"] = build_pdf(target, title, content_markdown)
        elif fmt == "xlsx":
            details.update(build_xlsx(target, title, sheets if isinstance(sheets, list) else []))
        else:
            details.update(build_pptx(target, title, slides if isinstance(slides, list) else []))
    except BaseException:
        # only our own empty/partial placeholder is removed
        try:
            target.unlink()
        except OSError:
            pass
        raise
    telegram = _telegram_copy(target)
    return {
        "status": "created",
        "format": fmt,
        "path": str(target),
        "bytes": target.stat().st_size,
        "seconds": round(time.monotonic() - started, 2),
        "telegram_attachment": str(telegram) if telegram else None,
        "delivery": "Tell the user the path. On Telegram, end the reply with a line of its own: MEDIA:<telegram_attachment>",
        **details,
    }
