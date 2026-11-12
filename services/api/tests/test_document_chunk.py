"""Tests for the document chunker."""
import dataclasses

import pytest

from kwim_api.config import settings
from kwim_api.documents import chunk as chunk_mod
from kwim_api.documents.chunk import chunk_extraction, estimate_tokens
from kwim_api.documents.structure import StructureBuilder


@pytest.fixture
def sizes(monkeypatch):
    """Small chunk sizes: 4 characters per token, target 20, max 30 tokens."""
    def _set(target=20, maximum=30):
        monkeypatch.setattr(chunk_mod, "settings", dataclasses.replace(
            settings, documents_chunk_target_tokens=target,
            documents_chunk_max_tokens=maximum, documents_chars_per_token=4.0))
    _set()
    return _set


def _doc():
    b = StructureBuilder()
    b.heading("Pump", 1, 1)
    b.heading("Removal", 2, 1)
    b.block("warning", "Relieve the pressure.", 1)
    for n in range(1, 6):
        b.block("step", f"Do step number {n} carefully now.", n, step_number=n)
    b.heading("Inspection", 2, 6)
    b.block("paragraph", "Check the impeller.", 6)
    return b.build()


def test_chunks_stay_inside_sections_and_carry_heading_paths(sizes):
    chunks = chunk_extraction(_doc(), title="Manual")
    assert [c.seq for c in chunks] == list(range(len(chunks)))
    removal = [c for c in chunks if c.heading_path == ["Pump", "Removal"]]
    inspection = [c for c in chunks if c.heading_path == ["Pump", "Inspection"]]
    assert len(removal) > 1 and len(inspection) == 1
    assert inspection[0].text == "Check the impeller."
    assert inspection[0].embed_text == "Manual > Pump > Inspection:\nCheck the impeller."


def test_blocks_are_never_split_below_the_maximum_and_render_their_type(sizes):
    chunks = chunk_extraction(_doc(), title="Manual")
    removal = [c for c in chunks if c.heading_path == ["Pump", "Removal"]]
    text = "\n\n".join(c.text for c in removal)
    assert text.startswith("WARNING: Relieve the pressure.")
    for n in range(1, 6):
        assert text.count(f"{n}. Do step number {n} carefully now.") == 1
    assert all(estimate_tokens(c.text) <= 30 + 2 for c in removal)


def test_pages_and_block_types(sizes):
    chunks = chunk_extraction(_doc(), title="Manual")
    first = chunks[0]
    assert first.page_start == 1 and first.block_types[0] == "warning"
    last_removal = [c for c in chunks if c.heading_path[-1] == "Removal"][-1]
    assert last_removal.page_end == 5


def test_an_oversized_table_splits_by_rows_and_repeats_the_header(sizes):
    b = StructureBuilder()
    b.heading("Specs", 1)
    rows = [[f"M{n}", f"{n * 2} Nm"] for n in range(1, 30)]
    b.block("table", "x", table={"headers": ["Bolt", "Torque"], "rows": rows})
    chunks = chunk_extraction(b.build(), title="")
    parts = [c.text for c in chunks]
    assert len(parts) > 1
    assert all(p.startswith("Bolt | Torque\n") for p in parts)
    body_rows = [line for p in parts for line in p.split("\n")[1:]]
    assert body_rows == [f"M{n} | {n * 2} Nm" for n in range(1, 30)]


def test_an_oversized_paragraph_splits_on_sentences(sizes):
    b = StructureBuilder()
    b.heading("Notes", 1)
    sentences = [f"Sentence number {n} is here." for n in range(12)]
    b.block("paragraph", " ".join(sentences))
    chunks = chunk_extraction(b.build(), title="")
    joined = " ".join(c.text.replace("\n\n", " ") for c in chunks)
    assert joined == " ".join(sentences)
    assert all(estimate_tokens(c.text) <= 32 for c in chunks)


def test_text_with_no_boundaries_is_cut_to_the_maximum(sizes):
    b = StructureBuilder()
    b.heading("Blob", 1)
    b.block("code", "x" * 500)
    chunks = chunk_extraction(b.build(), title="")
    assert "".join(c.text.replace("\n\n", "") for c in chunks) == "x" * 500
    assert all(estimate_tokens(c.text) <= 32 for c in chunks)


def test_figure_refs_are_recorded_on_their_chunk(sizes):
    b = StructureBuilder()
    b.heading("Parts", 1)
    b.figure("f1", page=2, caption="Figure 1 Pump", file=None)
    b.block("figure_ref", "Figure 1 Pump", 2, figure_id="f1")
    chunks = chunk_extraction(b.build(), title="")
    assert chunks[0].figure_ids == ["f1"] and chunks[0].text == "[Figure: Figure 1 Pump]"
