from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


SUPPORTED_EXTENSIONS = {
    ".csv", ".docx", ".html", ".htm", ".json", ".md", ".pdf", ".pptx",
    ".rtf", ".txt", ".xls", ".xlsx", ".xml", ".png", ".jpg", ".jpeg",
    ".bmp", ".tif", ".tiff", ".webp",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
TEXT_EXTENSIONS = {".html", ".htm", ".json", ".md", ".rtf", ".txt", ".xml"}


def _tesseract() -> Path:
    configured = os.environ.get("QWEN_TESSERACT", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ]
    discovered = shutil.which("tesseract")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise RuntimeError("Tesseract OCR is not installed.")


def _tessdata() -> Path | None:
    configured = os.environ.get("QWEN_TESSDATA", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parents[1] / "ocr" / "tessdata",
        Path(r"E:\localllm\ocr\tessdata"),
        Path(r"C:\Program Files\Tesseract-OCR\tessdata"),
    ]
    for candidate in candidates:
        if candidate and (candidate / "eng.traineddata").is_file():
            return candidate
    return None


def _ocr(input_value: Path | bytes, languages: str = "tha+eng") -> str:
    executable = _tesseract()
    command = [str(executable), "stdin" if isinstance(input_value, bytes) else str(input_value), "stdout"]
    tessdata = _tessdata()
    if tessdata:
        command.extend(["--tessdata-dir", str(tessdata)])
    command.extend(["-l", languages, "--psm", "6", "quiet"])
    completed = subprocess.run(
        command,
        input=input_value if isinstance(input_value, bytes) else None,
        capture_output=True,
        timeout=180,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if completed.returncode:
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"OCR failed: {error or completed.returncode}")
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _markdown_table(rows: list[list[Any]], max_rows: int = 100, max_cols: int = 30) -> str:
    cleaned: list[list[str]] = []
    for row in rows[:max_rows]:
        values = []
        for value in list(row)[:max_cols]:
            text = "" if value is None else str(value)
            values.append(text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip())
        cleaned.append(values)
    if not cleaned:
        return ""
    width = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    header = cleaned[0]
    body = cleaned[1:]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _pdf(path: Path, use_ocr: bool, max_pages: int, languages: str) -> list[dict[str, Any]]:
    import pymupdf

    sections: list[dict[str, Any]] = []
    document = pymupdf.open(path)
    try:
        for page_index in range(min(len(document), max_pages)):
            page = document[page_index]
            text = page.get_text("text", sort=True).strip()
            method = "embedded_text"
            if use_ocr and len(text) < 40:
                pixmap = page.get_pixmap(dpi=200, alpha=False)
                text = _ocr(pixmap.tobytes("png"), languages=languages)
                method = "ocr"
            tables: list[str] = []
            try:
                found = page.find_tables()
                for table in found.tables[:10]:
                    markdown = _markdown_table(table.extract())
                    if markdown:
                        tables.append(markdown)
            except Exception:
                pass
            content = text
            if tables:
                content += "\n\n" + "\n\n".join(
                    f"Table {index + 1}\n\n{table}" for index, table in enumerate(tables)
                )
            sections.append(
                {
                    "citation": f"{path.name} · page {page_index + 1}",
                    "page": page_index + 1,
                    "method": method,
                    "text": content.strip(),
                }
            )
    finally:
        document.close()
    return sections


def _excel(path: Path, max_pages: int) -> list[dict[str, Any]]:
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sections: list[dict[str, Any]] = []
    try:
        for sheet in workbook.worksheets[:max_pages]:
            rows = [list(row) for row in sheet.iter_rows(values_only=True, max_row=200, max_col=40)]
            sections.append(
                {
                    "citation": f"{path.name} · sheet {sheet.title}",
                    "sheet": sheet.title,
                    "method": "spreadsheet",
                    "text": _markdown_table(rows, max_rows=200, max_cols=40),
                }
            )
    finally:
        workbook.close()
    return sections


def _docx(path: Path) -> list[dict[str, Any]]:
    from docx import Document

    document = Document(path)
    chunks = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    for index, table in enumerate(document.tables, start=1):
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        rendered = _markdown_table(rows)
        if rendered:
            chunks.append(f"Table {index}\n\n{rendered}")
    return [{"citation": f"{path.name} · document", "method": "docx", "text": "\n\n".join(chunks)}]


def _pptx(path: Path, max_pages: int) -> list[dict[str, Any]]:
    from pptx import Presentation

    presentation = Presentation(path)
    sections: list[dict[str, Any]] = []
    for index, slide in enumerate(presentation.slides[:max_pages], start=1):
        parts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                value = shape.text.strip()
                if value:
                    parts.append(value)
            if getattr(shape, "has_table", False):
                rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
                rendered = _markdown_table(rows)
                if rendered:
                    parts.append(rendered)
        sections.append(
            {"citation": f"{path.name} · slide {index}", "slide": index, "method": "pptx", "text": "\n\n".join(parts)}
        )
    return sections


def _csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        rows = [row for _, row in zip(range(200), csv.reader(handle))]
    return [{"citation": f"{path.name} · rows 1-{len(rows)}", "method": "csv", "text": _markdown_table(rows, 200, 40)}]


def extract_document(
    path: Path,
    *,
    use_ocr: bool = True,
    max_pages: int = 40,
    max_chars: int = 60_000,
    languages: str = "tha+eng",
) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    extension = path.suffix.casefold()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported document type: {extension}")
    max_pages = max(1, min(int(max_pages), 100))
    max_chars = max(2_000, min(int(max_chars), 200_000))
    if extension == ".pdf":
        sections = _pdf(path, use_ocr, max_pages, languages)
    elif extension in {".xlsx", ".xls"}:
        if extension == ".xls":
            raise ValueError("Legacy .xls should be converted to .xlsx before structured extraction.")
        sections = _excel(path, max_pages)
    elif extension == ".docx":
        sections = _docx(path)
    elif extension == ".pptx":
        sections = _pptx(path, max_pages)
    elif extension == ".csv":
        sections = _csv(path)
    elif extension in IMAGE_EXTENSIONS:
        sections = [{"citation": f"{path.name} · image", "method": "ocr", "text": _ocr(path, languages)}]
    else:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if extension == ".json":
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                pass
        sections = [{"citation": f"{path.name} · document", "method": "text", "text": text}]

    rendered: list[str] = []
    citations: list[dict[str, Any]] = []
    used = 0
    for section in sections:
        heading = f"## [{section['citation']}]\n\n"
        body = str(section.get("text") or "").strip()
        if not body:
            body = "(no readable text found)"
        remaining = max_chars - used - len(heading)
        if remaining <= 0:
            break
        piece = heading + body[:remaining]
        rendered.append(piece)
        used += len(piece) + 2
        citations.append({key: value for key, value in section.items() if key != "text"})
    return {
        "file": str(path),
        "filename": path.name,
        "type": extension,
        "sections": len(citations),
        "citations": citations,
        "content": "\n\n".join(rendered),
        "truncated": used >= max_chars,
        "ocr_languages": languages if use_ocr or extension in IMAGE_EXTENSIONS else None,
    }
