"""The intermediate format every extractor produces, and a builder for it.

    {
      "format": 1,
      "title": str | None,
      "sections": [Section],
      "figures": [{"id", "page", "caption", "file", "referenced_by": [section ids]}],
    }
    Section: {"id", "heading", "level", "page_start", "page_end",
              "blocks": [Block], "children": [Section]}
    Block:   {"type", "text", "page", ...}
      type is one of paragraph, step, list, table, warning, caution, note,
      code, figure_ref. A step carries "step_number", a table carries
      "table": {"headers", "rows"}, a figure_ref carries "figure_id".

Content before the first heading goes into a level-0 section with an empty
heading. `file` on a figure is a path in the extractor's output directory; the
ingest job stores it and replaces it with the stored object key.
"""
from typing import Any

FORMAT_VERSION = 1

BLOCK_TYPES = frozenset({"paragraph", "step", "list", "table", "warning", "caution",
                         "note", "code", "figure_ref"})


class StructureBuilder:
    def __init__(self) -> None:
        self._count = 0
        self._root = self._new_section("", 0, None)
        self._stack: list[dict[str, Any]] = [self._root]
        self.figures: list[dict[str, Any]] = []
        self.title: str | None = None

    def _new_section(self, heading: str, level: int, page: int | None) -> dict[str, Any]:
        sid = f"s{self._count}"
        self._count += 1
        return {"id": sid, "heading": heading, "level": level, "page_start": page,
                "page_end": page, "blocks": [], "children": []}

    @property
    def current_section_id(self) -> str:
        return self._stack[-1]["id"]

    def heading(self, text: str, level: int, page: int | None = None) -> None:
        """Open a section at `level` (1 is the top), closing any at the same or a
        deeper level."""
        level = max(1, level)
        while len(self._stack) > 1 and self._stack[-1]["level"] >= level:
            self._stack.pop()
        section = self._new_section(text.strip(), level, page)
        self._stack[-1]["children"].append(section)
        self._stack.append(section)
        self._touch(page)
        if self.title is None and level == 1:
            self.title = section["heading"]

    def block(self, type_: str, text: str, page: int | None = None, **extra: Any) -> None:
        if type_ not in BLOCK_TYPES:
            raise ValueError(f"unknown block type {type_!r}")
        if not text.strip() and type_ not in ("table", "figure_ref"):
            return
        block = {"type": type_, "text": text.strip(), "page": page, **extra}
        self._stack[-1]["blocks"].append(block)
        self._touch(page)

    def figure(self, figure_id: str, *, page: int | None, caption: str | None,
               file: str | None) -> None:
        self.figures.append({"id": figure_id, "page": page, "caption": caption,
                             "file": file, "referenced_by": [self.current_section_id]})

    def _touch(self, page: int | None) -> None:
        if page is None:
            return
        for section in self._stack:
            if section["page_start"] is None or page < section["page_start"]:
                section["page_start"] = page
            if section["page_end"] is None or page > section["page_end"]:
                section["page_end"] = page

    def build(self, title: str | None = None) -> dict[str, Any]:
        sections = list(self._root["children"])
        if self._root["blocks"]:
            # The headed sections are siblings of the preamble, not its children,
            # and its pages are those of its own blocks.
            pages = [b["page"] for b in self._root["blocks"] if b["page"] is not None]
            sections.insert(0, {**self._root, "children": [],
                                "page_start": min(pages) if pages else None,
                                "page_end": max(pages) if pages else None})
        return {"format": FORMAT_VERSION, "title": title or self.title,
                "sections": sections, "figures": self.figures}


def iter_sections(sections: list[dict[str, Any]]):
    """Every section in document order, depth first."""
    for section in sections:
        yield section
        yield from iter_sections(section["children"])


def text_length(extracted: dict[str, Any]) -> int:
    """Characters of block text in an extraction."""
    return sum(len(b["text"]) for s in iter_sections(extracted["sections"])
               for b in s["blocks"])
