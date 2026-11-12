"""Split an extraction into chunks for embedding and search.

A chunk is a run of whole blocks from one section, filled up to about
`documents.chunk_target_tokens` and never past `documents.chunk_max_tokens`.
Chunks never cross a section boundary. A single block larger than the maximum is
split: tables by rows (repeating the header), code and lists by lines, other text
by sentences. Token counts are estimated as characters / chars_per_token.

Each chunk's `embed_text` starts with the document title and section path, so a
chunk that says "remove the four bolts" carries which bolts. `text` is stored
without that prefix.
"""
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from .structure import iter_sections

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Chunk:
    id: str
    seq: int
    section_id: str
    heading_path: list[str]
    text: str
    embed_text: str
    page_start: int | None
    page_end: int | None
    block_types: list[str] = field(default_factory=list)
    figure_ids: list[str] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    return int(len(text) / settings.documents_chars_per_token) + 1


def chunk_extraction(extracted: dict[str, Any], *, title: str) -> list[Chunk]:
    target = settings.documents_chunk_target_tokens
    maximum = max(settings.documents_chunk_max_tokens, target)
    paths = _heading_paths(extracted["sections"])
    chunks: list[Chunk] = []
    for section in iter_sections(extracted["sections"]):
        path = paths[section["id"]]
        pending: list[dict[str, Any]] = []
        size = 0
        for block in section["blocks"]:
            for piece in _pieces(block, maximum):
                n = estimate_tokens(_render(piece))
                if pending and size + n > target:
                    chunks.append(_make(chunks, section, path, pending, title))
                    pending, size = [], 0
                pending.append(piece)
                size += n
        if pending:
            chunks.append(_make(chunks, section, path, pending, title))
    return chunks


def _heading_paths(sections: list[dict[str, Any]],
                   prefix: list[str] | None = None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for section in sections:
        path = (prefix or []) + ([section["heading"]] if section["heading"] else [])
        out[section["id"]] = path
        out.update(_heading_paths(section["children"], path))
    return out


def _render(block: dict[str, Any]) -> str:
    """The block as chunk text."""
    t = block["type"]
    text = block["text"]
    if t == "step":
        return f"{block.get('step_number', '')}. {text}".strip()
    if t in ("warning", "caution", "note"):
        return f"{t.upper()}: {text}"
    if t == "figure_ref":
        return f"[Figure: {text}]" if text else "[Figure]"
    if t == "table" and block.get("table"):
        table = block["table"]
        lines = [" | ".join(table["headers"])] if table.get("headers") else []
        return "\n".join(lines + [" | ".join(r) for r in table["rows"]])
    return text


def _pieces(block: dict[str, Any], maximum: int) -> list[dict[str, Any]]:
    """The block, or its parts when it is larger than `maximum` tokens."""
    if estimate_tokens(_render(block)) <= maximum:
        return [block]
    t = block["type"]
    if t == "table" and block.get("table"):
        return _split_table(block, maximum)
    if t in ("code", "list"):
        units = block["text"].split("\n")
        joiner = "\n"
    else:
        units = _SENTENCE.split(block["text"])
        joiner = " "
    parts: list[dict[str, Any]] = []
    current: list[str] = []
    for unit in units:
        for piece in _hard_split(unit, maximum):
            if current and estimate_tokens(joiner.join(current + [piece])) > maximum:
                parts.append({**block, "text": joiner.join(current)})
                current = []
            current.append(piece)
    if current:
        parts.append({**block, "text": joiner.join(current)})
    return parts


def _hard_split(text: str, maximum: int) -> list[str]:
    """Cut text with no usable boundary into pieces of at most `maximum` tokens."""
    limit = max(1, int(maximum * settings.documents_chars_per_token) - 1)
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]


def _split_table(block: dict[str, Any], maximum: int) -> list[dict[str, Any]]:
    headers = block["table"].get("headers") or []
    header_line = " | ".join(headers)
    parts: list[dict[str, Any]] = []
    rows: list[list[str]] = []
    for row in block["table"]["rows"]:
        candidate = rows + [row]
        text = "\n".join([header_line] + [" | ".join(r) for r in candidate])
        if rows and estimate_tokens(text) > maximum:
            parts.append(_table_part(block, headers, rows))
            rows = []
        rows.append(row)
    if rows:
        parts.append(_table_part(block, headers, rows))
    return parts


def _table_part(block: dict[str, Any], headers: list[str],
                rows: list[list[str]]) -> dict[str, Any]:
    lines = ([" | ".join(headers)] if headers else []) + [" | ".join(r) for r in rows]
    return {**block, "text": "\n".join(lines), "table": {"headers": headers, "rows": rows}}


def _make(chunks: list[Chunk], section: dict[str, Any], path: list[str],
          blocks: list[dict[str, Any]], title: str) -> Chunk:
    seq = len(chunks)
    text = "\n\n".join(_render(b) for b in blocks)
    prefix = " > ".join([title] + path) if title else " > ".join(path)
    pages = [b["page"] for b in blocks if b.get("page") is not None]
    if not pages:
        pages = [p for p in (section.get("page_start"), section.get("page_end"))
                 if p is not None]
    types: list[str] = []
    for b in blocks:
        if b["type"] not in types:
            types.append(b["type"])
    return Chunk(
        id=f"c{seq}", seq=seq, section_id=section["id"], heading_path=path, text=text,
        embed_text=f"{prefix}:\n{text}" if prefix else text,
        page_start=min(pages) if pages else None, page_end=max(pages) if pages else None,
        block_types=types,
        figure_ids=[b["figure_id"] for b in blocks if b["type"] == "figure_ref"],
    )
