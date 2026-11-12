"""Tests for the Markdown and PDF extractors and the structure builder."""
import io
import os
import struct
import zlib

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from kwim_api.documents.extract_markdown import extract_markdown
from kwim_api.documents.extract_pdf import extract_pdf
from kwim_api.documents.structure import StructureBuilder, iter_sections


def _sections(extracted):
    return list(iter_sections(extracted["sections"]))


def _blocks(extracted, type_=None):
    return [b for s in _sections(extracted) for b in s["blocks"]
            if type_ is None or b["type"] == type_]


def _by_heading(extracted, heading):
    return next(s for s in _sections(extracted) if s["heading"] == heading)


# ---------------------------------------------------------------------------
# Structure builder
# ---------------------------------------------------------------------------

def test_builder_nests_sections_and_tracks_pages():
    b = StructureBuilder()
    b.block("paragraph", "preamble", 1)
    b.heading("Chapter", 1, 1)
    b.heading("Part A", 2, 2)
    b.block("step", "do it", 3, step_number=1)
    b.heading("Part B", 2, 4)
    b.heading("Next chapter", 1, 5)
    x = b.build()
    assert [s["heading"] for s in x["sections"]] == ["", "Chapter", "Next chapter"]
    preamble = x["sections"][0]
    assert preamble["children"] == []
    assert (preamble["page_start"], preamble["page_end"]) == (1, 1)
    assert [s["heading"] for s in iter_sections(x["sections"])] == [
        "", "Chapter", "Part A", "Part B", "Next chapter"]
    chapter = x["sections"][1]
    assert [c["heading"] for c in chapter["children"]] == ["Part A", "Part B"]
    assert (chapter["page_start"], chapter["page_end"]) == (1, 4)
    assert x["title"] == "Chapter"


def test_builder_rejects_unknown_block_types_and_drops_empty_text():
    b = StructureBuilder()
    with pytest.raises(ValueError):
        b.block("banner", "x")
    b.block("paragraph", "   ")
    assert b.build()["sections"] == []


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

MARKDOWN = """# Pump Manual

Intro text.

## Removal

> **Warning:** Relieve fuel pressure first.

1. Disconnect the battery.
2. Remove the **four** bolts.
   - keep them
3. Lift the pump out.

- torque wrench
- 10 mm socket

| Bolt | Torque |
|------|--------|
| M6   | 10 Nm  |

```
systemctl stop pump
```

![Exploded view](pump.png "Figure 3 - exploded view")

NOTE: Replace the gasket.

### Inspection

Check the impeller.
"""


@pytest.fixture
def md_path(tmp_path):
    p = tmp_path / "manual.md"
    p.write_text(MARKDOWN)
    return str(p)


def test_markdown_sections_nest_by_heading_level(md_path, tmp_path):
    x = extract_markdown(md_path, str(tmp_path)).extracted
    assert x["title"] == "Pump Manual"
    top = x["sections"][0]
    assert top["heading"] == "Pump Manual" and top["level"] == 1
    removal = top["children"][0]
    assert removal["heading"] == "Removal"
    assert removal["children"][0]["heading"] == "Inspection"


def test_markdown_block_types(md_path, tmp_path):
    x = extract_markdown(md_path, str(tmp_path)).extracted
    removal = _by_heading(x, "Removal")
    types = [b["type"] for b in removal["blocks"]]
    assert types == ["warning", "step", "step", "step", "list", "table", "code",
                     "figure_ref", "note"]
    steps = [b for b in removal["blocks"] if b["type"] == "step"]
    assert [s["step_number"] for s in steps] == [1, 2, 3]
    assert steps[1]["text"] == "Remove the four bolts. keep them"
    assert removal["blocks"][0]["text"] == "Relieve fuel pressure first."
    table = next(b for b in removal["blocks"] if b["type"] == "table")
    assert table["table"] == {"headers": ["Bolt", "Torque"], "rows": [["M6", "10 Nm"]]}
    assert next(b for b in removal["blocks"] if b["type"] == "code")["text"] == \
        "systemctl stop pump"


def test_markdown_images_become_figures(md_path, tmp_path):
    x = extract_markdown(md_path, str(tmp_path)).extracted
    assert x["figures"] == [{"id": "f1", "page": None,
                             "caption": "Figure 3 - exploded view", "file": None,
                             "referenced_by": [_by_heading(x, "Removal")["id"]]}]


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _png_bytes(width=200, height=120, rgb=(200, 30, 30)):
    """A solid-colour RGB PNG, written directly."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    row = b"\x00" + bytes(rgb) * width
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(row * height))
           + chunk(b"IEND", b""))
    return io.BytesIO(png)


def _write_pdf(path):
    from reportlab.lib.utils import ImageReader
    c = canvas.Canvas(path, pagesize=letter)
    w, h = letter

    def header_footer(n):
        c.setFont("Helvetica", 8)
        c.drawString(72, h - 30, "ACME PUMP SERVICE MANUAL")
        c.drawString(w / 2, 30, str(n))

    def body(y, text, font="Helvetica", size=10):
        c.setFont(font, size)
        c.drawString(72, y, text)
        return y - size * 1.4

    # Page 1
    header_footer(1)
    y = body(h - 80, "Pump Service Manual", "Helvetica-Bold", 22)
    y = body(y - 10, "1 Removal", "Helvetica-Bold", 16)
    y = body(y - 4, "Read this whole chapter before starting the job on the")
    y = body(y, "pump. It covers removal of the fuel pump assembly.")
    y = body(y - 8, "WARNING: Relieve fuel pressure before loosening any line.")
    y = body(y - 8, "1. Disconnect the negative battery cable.")
    y = body(y, "2. Remove the four mounting bolts.")
    y = body(y, "3. Lift the pump out of the tank.")
    y = body(y - 8, "\u2022 torque wrench")
    y = body(y, "\u2022 10 mm socket")
    y = body(y - 8, "1.1 Tools and torque", "Helvetica-Bold", 13)
    # Ruled table
    top = y - 4
    rows = [("Bolt", "Torque"), ("M6", "10 Nm"), ("M8", "22 Nm")]
    for i, (a, b) in enumerate(rows):
        ry = top - i * 18
        c.setFont("Helvetica", 10)
        c.drawString(78, ry - 13, a)
        c.drawString(178, ry - 13, b)
    c.setLineWidth(0.5)
    for i in range(len(rows) + 1):
        c.line(72, top - i * 18, 272, top - i * 18)
    for x in (72, 172, 272):
        c.line(x, top, x, top - len(rows) * 18)
    y = top - len(rows) * 18 - 20
    y = body(y, "systemctl stop pump.service", "Courier", 10)
    y = body(y, "journalctl -u pump", "Courier", 10)
    c.showPage()

    # Page 2
    header_footer(2)
    y = body(h - 80, "2 Inspection", "Helvetica-Bold", 16)
    y = body(y - 4, "Inspect the impeller for wear and replace it if scored.")
    c.drawImage(ImageReader(_png_bytes()), 72, y - 140, width=200, height=120)
    y = body(y - 160, "Figure 2-1 Impeller location")
    # A vector drawing above a caption
    c.setLineWidth(2)
    c.rect(72, y - 130, 200, 110)
    c.line(72, y - 130, 272, y - 20)
    y = body(y - 145, "Figure 2-2 Wiring diagram")
    y = body(y - 10, "Reinstall in reverse order.")
    c.showPage()

    # Page 3, so the running header repeats on enough pages
    header_footer(3)
    body(h - 80, "3 Specifications", "Helvetica-Bold", 16)
    body(h - 110, "Flow rate is 120 litres per hour at 3 bar.")
    c.showPage()
    c.save()


@pytest.fixture(scope="module")
def pdf_result(tmp_path_factory):
    d = tmp_path_factory.mktemp("pdf")
    path = str(d / "manual.pdf")
    _write_pdf(path)
    figures = d / "figures"
    figures.mkdir()
    return extract_pdf(path, str(figures)), figures


def test_pdf_headings_use_numbering_and_size(pdf_result):
    result, _ = pdf_result
    x = result.extracted
    assert result.page_count == 3
    headings = [(s["heading"], s["level"]) for s in _sections(x)]
    assert ("1 Removal", 1) in headings
    assert ("1.1 Tools and torque", 2) in headings
    assert ("2 Inspection", 1) in headings
    assert ("3 Specifications", 1) in headings
    assert _by_heading(x, "1.1 Tools and torque") in _by_heading(x, "1 Removal")["children"]


def test_pdf_drops_running_header_and_page_numbers(pdf_result):
    x = pdf_result[0].extracted
    texts = [b["text"] for b in _blocks(x)] + [s["heading"] for s in _sections(x)]
    assert not any("ACME PUMP SERVICE MANUAL" in t for t in texts)
    assert not any(t.strip() in ("1", "2", "3") for t in texts)


def test_pdf_paragraphs_steps_lists_and_warnings(pdf_result):
    x = pdf_result[0].extracted
    removal = _by_heading(x, "1 Removal")
    types = [b["type"] for b in removal["blocks"]]
    assert types == ["paragraph", "warning", "step", "step", "step", "list"]
    assert removal["blocks"][0]["text"].startswith("Read this whole chapter before starting")
    assert removal["blocks"][0]["text"].endswith("fuel pump assembly.")
    assert removal["blocks"][1]["text"] == "Relieve fuel pressure before loosening any line."
    assert [b["step_number"] for b in removal["blocks"] if b["type"] == "step"] == [1, 2, 3]
    assert removal["blocks"][-1]["text"] == "- torque wrench\n- 10 mm socket"


def test_pdf_ruled_table_and_code(pdf_result):
    x = pdf_result[0].extracted
    tools = _by_heading(x, "1.1 Tools and torque")
    table = next(b for b in tools["blocks"] if b["type"] == "table")
    assert table["table"] == {"headers": ["Bolt", "Torque"],
                              "rows": [["M6", "10 Nm"], ["M8", "22 Nm"]]}
    code = next(b for b in tools["blocks"] if b["type"] == "code")
    assert code["text"] == "systemctl stop pump.service\njournalctl -u pump"


def test_pdf_figures_from_images_and_captioned_drawings(pdf_result):
    result, figures_dir = pdf_result
    x = result.extracted
    figs = {f["caption"]: f for f in x["figures"]}
    assert set(figs) == {"Figure 2-1 Impeller location", "Figure 2-2 Wiring diagram"}
    inspection = _by_heading(x, "2 Inspection")
    for fig in figs.values():
        assert fig["page"] == 2
        assert fig["referenced_by"] == [inspection["id"]]
        assert fig["file"] and os.path.getsize(fig["file"]) > 0
        assert os.path.dirname(fig["file"]) == str(figures_dir)
    refs = [b for b in inspection["blocks"] if b["type"] == "figure_ref"]
    assert {r["figure_id"] for r in refs} == {f["id"] for f in figs.values()}


def test_pdf_without_a_text_layer_is_rejected(tmp_path):
    path = str(tmp_path / "scan.pdf")
    from reportlab.lib.utils import ImageReader
    c = canvas.Canvas(path, pagesize=letter)
    c.drawImage(ImageReader(_png_bytes()), 72, 300, width=400, height=300)
    c.showPage()
    c.save()
    with pytest.raises(ValueError, match="no usable text layer"):
        extract_pdf(path, str(tmp_path))
