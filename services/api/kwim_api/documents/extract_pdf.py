"""PDF extractor for born-digital PDFs (a text layer is required; scans need OCR).

Per page, text lines come from pdfplumber with their font size and weight, and
tables from its table finder. Then:

  - Running headers and footers (the same text at the same height near the top or
    bottom of most pages) and bare page numbers are dropped.
  - Headings are lines set larger than the body text, or wholly bold short lines.
    The level comes from section numbering ("3.2 Removal" is level 2) when
    present, else from the font size rank.
  - Numbered lines become steps, bulleted lines lists, and lines opening with
    WARNING, CAUTION or NOTE those block types. Other lines join into paragraphs.
  - Figures are embedded images, and regions above a "Figure ..." caption (which
    covers vector drawings). Each is rendered with pdfium and saved as PNG.
"""
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import pdfplumber
import pypdfium2 as pdfium

from .ingest_types import Extraction
from .structure import StructureBuilder

EXTRACTOR = "pdfplumber"
VERSION = "1"

# A document with fewer extracted characters than this per page has no usable
# text layer.
_MIN_CHARS_PER_PAGE = 20
_HEADING_SIZE_RATIO = 1.15
_HEADING_MAX_CHARS = 120
_EDGE_FRACTION = 0.08
_REPEAT_FRACTION = 0.5
_FIGURE_MIN_PT = 40
_CAPTION_GAP_PT = 72
_RENDER_SCALE = 2.0
# Wrapped step and list text is indented at least this far past the marker.
_INDENT_PT = 2.0
# A gap wider than this many ems inside a line separates table columns.
_COLUMN_GAP_EM = 2.0
_MONO_FONTS = ("mono", "courier", "consol", "menlo", "inconsolata")

_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")
_STEP = re.compile(r"^\(?(\d{1,3})[.)]\s+(\S.*)$")
_BULLET = re.compile(r"^(?:[\u2022\u25cf\u25aa\u25e6\u2013\uf0b7*-]|\(cid:\d+\))\s*(\S.*)$")
# A glyph pdfminer could not map to Unicode.
_CID = re.compile(r"\(cid:\d+\)")
_ADMONITION = re.compile(r"^(WARNING|DANGER|CAUTION|NOTE|IMPORTANT)\b\s*[:.\-]?\s*",
                         re.IGNORECASE)
_ADMONITION_TYPE = {"warning": "warning", "danger": "warning", "caution": "caution",
                    "note": "note", "important": "note"}
_CAPTION = re.compile(r"^(?:Figure|Fig\.)\s*[\w.\-]+", re.IGNORECASE)
_PAGE_NUMBER = re.compile(r"^(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?$", re.IGNORECASE)


@dataclass
class _Line:
    page: int
    text: str
    size: float
    bold: bool
    x0: float
    top: float
    bottom: float
    mono: bool = False
    columns: bool = False


@dataclass
class _Page:
    number: int
    width: float
    height: float
    lines: list[_Line] = field(default_factory=list)
    tables: list[tuple[tuple[float, float, float, float], list[list[str]]]] = \
        field(default_factory=list)
    images: list[tuple[float, float, float, float]] = field(default_factory=list)


def extract_pdf(path: str, figures_dir: str) -> Extraction:
    pages, meta_title = _read_pages(path)
    page_count = len(pages)
    chars = sum(len(ln.text) for p in pages for ln in p.lines)
    if page_count and chars < _MIN_CHARS_PER_PAGE * page_count:
        raise ValueError("the PDF has no usable text layer (a scanned document?); "
                         "OCR is not supported yet")

    _drop_running_lines(pages)
    body = _body_size(pages)
    levels = _heading_levels(pages, body)

    builder = StructureBuilder()
    figures = _Figures(path, figures_dir, builder)
    warnings: list[str] = []
    for page in pages:
        _emit_page(page, body, levels, builder, figures)
    figures.close()
    if figures.failed:
        warnings.append(f"{figures.failed} figure(s) could not be rendered")

    extracted = builder.build(title=meta_title)
    return Extraction(extracted=extracted, extractor=EXTRACTOR, extractor_version=VERSION,
                      page_count=page_count, warnings=warnings)


# --- Reading -----------------------------------------------------------------


def _read_pages(path: str) -> tuple[list[_Page], str | None]:
    pages: list[_Page] = []
    with pdfplumber.open(path) as pdf:
        title = (pdf.metadata or {}).get("Title")
        title = title.strip() if isinstance(title, str) and title.strip() else None
        for index, page in enumerate(pdf.pages, start=1):
            try:
                pages.append(_read_page(page, index))
            finally:
                page.close()
    return pages, title


def _read_page(page: Any, number: int) -> _Page:
    out = _Page(number=number, width=float(page.width), height=float(page.height))
    tables = page.find_tables()
    for table in tables:
        rows = [[(cell or "").strip() for cell in row] for row in table.extract()]
        rows = [r for r in rows if any(r)]
        if rows:
            out.tables.append((tuple(table.bbox), rows))
    boxes = [t[0] for t in out.tables]

    for line in page.extract_text_lines(return_chars=True, strip=True):
        text = re.sub(r"\s+", " ", line["text"]).strip()
        if not _BULLET.match(text):
            text = _CID.sub("", text).strip()
        if not text:
            continue
        if any(_inside(line, box) for box in boxes):
            continue
        chars = [c for c in line["chars"] if c.get("text", "").strip()]
        if not chars:
            continue
        sizes = Counter(round(float(c["size"]), 1) for c in chars)
        size = sizes.most_common(1)[0][0]
        fonts = [(c.get("fontname") or "").lower() for c in chars]
        out.lines.append(_Line(
            page=number, text=text, size=size,
            bold=all("bold" in f for f in fonts),
            x0=float(line["x0"]), top=float(line["top"]), bottom=float(line["bottom"]),
            mono=all(any(m in f for m in _MONO_FONTS) for f in fonts),
            columns=_has_column_gap(line["chars"], size)))

    for image in page.images:
        x0, top, x1, bottom = (float(image[k]) for k in ("x0", "top", "x1", "bottom"))
        if x1 - x0 >= _FIGURE_MIN_PT and bottom - top >= _FIGURE_MIN_PT:
            out.images.append((x0, top, x1, bottom))
    return out


def _has_column_gap(chars: list[dict], size: float) -> bool:
    """Whether a line has a horizontal gap wider than a few spaces, as between
    the columns of a table without ruling lines."""
    ordered = sorted(chars, key=lambda c: float(c["x0"]))
    for left, right in zip(ordered, ordered[1:], strict=False):
        if float(right["x0"]) - float(left["x1"]) > size * _COLUMN_GAP_EM:
            return True
    return False


def _inside(line: dict, box: tuple[float, float, float, float]) -> bool:
    x0, top, x1, bottom = box
    cx = (line["x0"] + line["x1"]) / 2
    cy = (line["top"] + line["bottom"]) / 2
    return x0 <= cx <= x1 and top <= cy <= bottom


# --- Cleanup and classification ----------------------------------------------


def _running_key(line: _Line) -> str:
    return re.sub(r"\d+", "#", line.text.lower())


def _drop_running_lines(pages: list[_Page]) -> None:
    """Remove page numbers, and text repeated near the top or bottom edge of most pages."""
    if not pages:
        return
    counts: Counter[str] = Counter()
    for page in pages:
        seen = set()
        for line in page.lines:
            if _near_edge(line, page):
                seen.add(_running_key(line))
        counts.update(seen)
    threshold = max(3, int(len(pages) * _REPEAT_FRACTION))
    repeated = {k for k, n in counts.items() if n >= threshold}
    for page in pages:
        page.lines = [ln for ln in page.lines
                      if not (_near_edge(ln, page)
                              and (_running_key(ln) in repeated or _PAGE_NUMBER.match(ln.text)))]


def _near_edge(line: _Line, page: _Page) -> bool:
    edge = page.height * _EDGE_FRACTION
    return line.top < edge or line.bottom > page.height - edge


def _body_size(pages: list[_Page]) -> float:
    weights: Counter[float] = Counter()
    for page in pages:
        for line in page.lines:
            weights[line.size] += len(line.text)
    return weights.most_common(1)[0][0] if weights else 10.0


def _is_heading_candidate(line: _Line, body: float) -> bool:
    text = line.text
    if len(text) > _HEADING_MAX_CHARS or not re.search(r"[A-Za-z]", text):
        return False
    if line.columns or line.mono:
        return False
    if _CAPTION.match(text) or _STEP.match(text) and not _NUMBERED_HEADING.match(text):
        return False
    if line.size >= body * _HEADING_SIZE_RATIO:
        return True
    return line.bold and line.size >= body - 0.5 and not text.endswith((".", ",", ";"))


def _heading_levels(pages: list[_Page], body: float) -> dict[float, int]:
    """Level by font size: the largest heading size is level 1."""
    sizes = sorted({line.size for p in pages for line in p.lines
                    if _is_heading_candidate(line, body) and line.size >= body * _HEADING_SIZE_RATIO},
                   reverse=True)
    return {size: i + 1 for i, size in enumerate(sizes)}


def _heading_level(line: _Line, levels: dict[float, int]) -> int:
    numbered = _NUMBERED_HEADING.match(line.text)
    if numbered:
        return numbered.group(1).count(".") + 1
    return levels.get(line.size, len(levels) + 1)


# --- Emitting ----------------------------------------------------------------


def _emit_page(page: _Page, body: float, levels: dict[float, int],
               b: StructureBuilder, figures: "_Figures") -> None:
    """Emit the page's lines, tables and figures in reading order (top to bottom)."""
    items: list[tuple[float, str, Any]] = [(ln.top, "line", ln) for ln in page.lines]
    items += [(box[1], "table", (box, rows)) for box, rows in page.tables]
    captions = [ln for ln in page.lines if _CAPTION.match(ln.text)]
    for fig in figures.plan(page, captions):
        items.append((fig["top"], "figure", fig))
    items.sort(key=lambda it: (it[0], {"figure": 0, "table": 1, "line": 2}[it[1]]))

    para = _Paragraph(b, page.number)
    for _, kind, item in items:
        if kind == "table":
            para.flush()
            _, rows = item
            headers, data = rows[0], rows[1:]
            text = "\n".join(" | ".join(r) for r in rows)
            b.block("table", text, page.number, table={"headers": headers, "rows": data})
            continue
        if kind == "figure":
            para.flush()
            figures.emit(page, item)
            continue
        line: _Line = item
        if _CAPTION.match(line.text):
            para.flush()
            continue
        if _is_heading_candidate(line, body) and not para.continues(line):
            para.flush()
            b.heading(line.text, _heading_level(line, levels), page.number)
            continue
        para.add(line)
    para.flush()


class _Paragraph:
    """Joins consecutive body lines into blocks, starting a new block at a step,
    a bullet, an admonition or a vertical gap."""

    def __init__(self, b: StructureBuilder, page: int) -> None:
        self.b = b
        self.page = page
        self.kind = "paragraph"
        self.extra: dict[str, Any] = {}
        self.parts: list[str] = []
        self.last: _Line | None = None
        self.marker_x0 = 0.0

    def continues(self, line: _Line) -> bool:
        """A short bold line right under body text reads as text, not a heading."""
        return (self.last is not None and self.kind == "paragraph" and line.bold
                and line.size < self.last.size * _HEADING_SIZE_RATIO
                and line.top - self.last.bottom < (self.last.bottom - self.last.top) * 0.6)

    def add(self, line: _Line) -> None:
        text = line.text
        if line.mono:
            if self.kind != "code":
                self.flush()
                self.kind, self.extra = "code", {}
            self.parts.append(text)
            self.last = line
            return
        if self.kind == "code":
            self.flush()
        step = _STEP.match(text)
        bullet = _BULLET.match(text)
        admonition = _ADMONITION.match(text)
        gap = (self.last is not None
               and line.top - self.last.bottom > (self.last.bottom - self.last.top) * 1.2)
        hanging = line.x0 > self.marker_x0 + _INDENT_PT
        if step:
            self.flush()
            self.kind, self.extra = "step", {"step_number": int(step.group(1))}
            self.parts = [step.group(2)]
            self.marker_x0 = line.x0
        elif bullet:
            if self.kind != "list" or gap:
                self.flush()
                self.kind, self.extra = "list", {}
            self.parts.append(f"- {_CID.sub('', bullet.group(1)).strip()}")
            self.marker_x0 = line.x0
        elif admonition:
            self.flush()
            self.kind = _ADMONITION_TYPE[admonition.group(1).lower()]
            self.extra = {}
            self.parts = [text[admonition.end():] or text]
        elif (gap and self.kind not in ("warning", "caution", "note")) or (
                self.kind in ("step", "list") and not hanging):
            self.flush()
            self.parts = [text]
        elif self.kind == "list" and self.parts:
            self.parts[-1] = _join(self.parts[-1], text)
        elif self.parts:
            self.parts[-1] = _join(self.parts[-1], text)
        else:
            self.parts = [text]
        self.last = line

    def flush(self) -> None:
        if self.parts:
            joined = "\n" if self.kind in ("list", "code") else " "
            text = joined.join(self.parts)
            self.b.block(self.kind, text, self.page, **self.extra)
        self.kind, self.extra, self.parts = "paragraph", {}, []


def _join(left: str, right: str) -> str:
    if left.endswith("-") and right[:1].islower():
        return left[:-1] + right
    return f"{left} {right}"


# --- Figures -----------------------------------------------------------------


class _Figures:
    """Plans figure regions on a page and renders them to PNG files."""

    def __init__(self, path: str, out_dir: str, b: StructureBuilder) -> None:
        self.path = path
        self.out_dir = out_dir
        self.b = b
        self.count = 0
        self.failed = 0
        self._pdf: pdfium.PdfDocument | None = None

    def plan(self, page: _Page, captions: list[_Line]) -> list[dict[str, Any]]:
        """Each image becomes a figure, captioned by a caption line just below or
        above it. A caption with no image near it marks the region above it, up to
        the previous text, as a figure (a vector drawing)."""
        planned: list[dict[str, Any]] = []
        used: set[int] = set()
        for box in page.images:
            caption = _nearest_caption(box, captions, used)
            planned.append({"box": box, "top": box[1], "caption": caption})
        for i, cap in enumerate(captions):
            if i in used:
                continue
            above = [ln for ln in page.lines if ln.bottom <= cap.top and not _CAPTION.match(ln.text)]
            region_top = max((ln.bottom for ln in above), default=page.height * _EDGE_FRACTION)
            if cap.top - region_top >= _FIGURE_MIN_PT:
                planned.append({"box": (0.0, region_top, page.width, cap.top),
                                "top": region_top, "caption": cap.text})
            else:
                planned.append({"box": None, "top": cap.top, "caption": cap.text})
        return planned

    def emit(self, page: _Page, fig: dict[str, Any]) -> None:
        self.count += 1
        fid = f"f{self.count}"
        file = self._render(page, fig["box"], fid) if fig["box"] else None
        self.b.figure(fid, page=page.number, caption=fig["caption"], file=file)
        self.b.block("figure_ref", fig["caption"] or "", page.number, figure_id=fid)

    def _render(self, page: _Page, box: tuple[float, float, float, float],
                fid: str) -> str | None:
        try:
            if self._pdf is None:
                self._pdf = pdfium.PdfDocument(self.path)
            pdf_page = self._pdf[page.number - 1]
            try:
                x0, top, x1, bottom = box
                crop = (max(0.0, x0), max(0.0, page.height - bottom),
                        max(0.0, page.width - x1), max(0.0, top))
                bitmap = pdf_page.render(scale=_RENDER_SCALE, crop=crop)
                file = os.path.join(self.out_dir, f"{fid}.png")
                bitmap.to_pil().save(file, format="PNG", optimize=True)
                return file
            finally:
                pdf_page.close()
        except Exception:
            self.failed += 1
            return None

    def close(self) -> None:
        if self._pdf is not None:
            self._pdf.close()


def _nearest_caption(box: tuple[float, float, float, float], captions: list[_Line],
                     used: set[int]) -> str | None:
    best: tuple[float, int] | None = None
    for i, cap in enumerate(captions):
        if i in used:
            continue
        gap = min(abs(cap.top - box[3]), abs(box[1] - cap.bottom))
        if gap <= _CAPTION_GAP_PT and (best is None or gap < best[0]):
            best = (gap, i)
    if best is None:
        return None
    used.add(best[1])
    return captions[best[1]].text
