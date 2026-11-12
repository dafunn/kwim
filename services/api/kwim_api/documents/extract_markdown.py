"""Markdown extractor: headings become sections; paragraphs, numbered steps,
lists, tables, code and admonitions become blocks; images become figure
references. Markdown has no pages, so every page is None.
"""
import re

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .ingest_types import Extraction
from .structure import StructureBuilder

EXTRACTOR = "markdown"
VERSION = "1"

# "WARNING:", "**Caution**", "> [!NOTE]" and similar openings.
_ADMONITION = re.compile(
    r"^\s*(?:\[!)?\**\s*(warning|danger|caution|note|important|tip)\s*\**\s*(?:\])?\s*[:.\-]?\s*",
    re.IGNORECASE)
_ADMONITION_TYPE = {"warning": "warning", "danger": "warning", "caution": "caution",
                    "important": "note", "note": "note", "tip": "note"}


def extract_markdown(path: str, figures_dir: str) -> Extraction:
    with open(path, encoding="utf-8", errors="replace") as fh:
        source = fh.read()
    tokens = MarkdownIt("commonmark").enable("table").parse(source)
    builder = StructureBuilder()
    _Walker(builder).walk(tokens)
    extracted = builder.build()
    return Extraction(extracted=extracted, extractor=EXTRACTOR, extractor_version=VERSION)


def inline_text(token: Token | None) -> str:
    """Plain text of an inline token: link and emphasis markup removed."""
    if token is None or not token.children:
        return token.content if token is not None else ""
    parts: list[str] = []
    for child in token.children:
        if child.type in ("text", "code_inline", "html_inline"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
        elif child.type == "image":
            parts.append(child.content or child.attrGet("alt") or "")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _images(token: Token | None) -> list[Token]:
    if token is None or not token.children:
        return []
    return [c for c in token.children if c.type == "image"]


class _Walker:
    def __init__(self, builder: StructureBuilder) -> None:
        self.b = builder
        self.figure_count = 0

    def walk(self, tokens: list[Token]) -> None:
        i = 0
        while i < len(tokens):
            i = self._one(tokens, i)

    def _one(self, tokens: list[Token], i: int) -> int:
        tok = tokens[i]
        if tok.type == "heading_open":
            self.b.heading(inline_text(tokens[i + 1]), int(tok.tag[1:]))
            return i + 3
        if tok.type == "paragraph_open":
            self._paragraph(tokens[i + 1], default="paragraph")
            return i + 3
        if tok.type in ("fence", "code_block"):
            self.b.block("code", tok.content)
            return i + 1
        if tok.type == "ordered_list_open":
            return self._list(tokens, i, ordered=True)
        if tok.type == "bullet_list_open":
            return self._list(tokens, i, ordered=False)
        if tok.type == "table_open":
            return self._table(tokens, i)
        if tok.type == "blockquote_open":
            return self._blockquote(tokens, i)
        return i + 1

    def _paragraph(self, inline: Token, default: str) -> None:
        text = inline_text(inline)
        for image in _images(inline):
            self._figure(image)
        if not _non_image_text(inline):
            return
        match = _ADMONITION.match(text)
        if match:
            self.b.block(_ADMONITION_TYPE[match.group(1).lower()], text[match.end():] or text)
        else:
            self.b.block(default, text)

    def _figure(self, image: Token) -> None:
        self.figure_count += 1
        fid = f"f{self.figure_count}"
        caption = (image.attrGet("title") or image.content or "").strip() or None
        self.b.figure(fid, page=None, caption=caption, file=None)
        self.b.block("figure_ref", caption or "", figure_id=fid,
                     source=image.attrGet("src"))

    def _end(self, tokens: list[Token], i: int, open_type: str, close_type: str) -> int:
        depth = 0
        for j in range(i, len(tokens)):
            if tokens[j].type == open_type:
                depth += 1
            elif tokens[j].type == close_type:
                depth -= 1
                if depth == 0:
                    return j
        return len(tokens) - 1

    def _list(self, tokens: list[Token], i: int, *, ordered: bool) -> int:
        open_type = "ordered_list_open" if ordered else "bullet_list_open"
        close_type = "ordered_list_close" if ordered else "bullet_list_close"
        end = self._end(tokens, i, open_type, close_type)
        start = int(tokens[i].attrGet("start") or 1)
        items = self._items(tokens, i + 1, end)
        if ordered:
            for n, text in enumerate(items, start=start):
                self.b.block("step", text, step_number=n)
        else:
            self.b.block("list", "\n".join(f"- {t}" for t in items))
        return end + 1

    def _items(self, tokens: list[Token], i: int, end: int) -> list[str]:
        """Text of each top-level list item; nested lists are folded into it."""
        items: list[str] = []
        depth = 0
        current: list[str] = []
        for tok in tokens[i:end]:
            if tok.type == "list_item_open":
                depth += 1
                if depth == 1:
                    current = []
            elif tok.type == "list_item_close":
                depth -= 1
                if depth == 0:
                    items.append(" ".join(p for p in current if p))
            elif tok.type == "inline":
                for image in _images(tok):
                    self._figure(image)
                current.append(inline_text(tok))
            elif tok.type in ("fence", "code_block"):
                current.append(tok.content.strip())
        return [t for t in items if t]

    def _table(self, tokens: list[Token], i: int) -> int:
        end = self._end(tokens, i, "table_open", "table_close")
        headers: list[str] = []
        rows: list[list[str]] = []
        row: list[str] = []
        in_head = False
        for tok in tokens[i:end]:
            if tok.type == "thead_open":
                in_head = True
            elif tok.type == "thead_close":
                in_head = False
            elif tok.type == "tr_open":
                row = []
            elif tok.type == "tr_close":
                (headers.extend(row) if in_head else rows.append(row))
            elif tok.type == "inline":
                row.append(inline_text(tok))
        self.b.block("table", _table_text(headers, rows),
                     table={"headers": headers, "rows": rows})
        return end + 1

    def _blockquote(self, tokens: list[Token], i: int) -> int:
        end = self._end(tokens, i, "blockquote_open", "blockquote_close")
        texts = [inline_text(t) for t in tokens[i:end] if t.type == "inline"]
        text = " ".join(t for t in texts if t)
        match = _ADMONITION.match(text)
        if match:
            self.b.block(_ADMONITION_TYPE[match.group(1).lower()], text[match.end():] or text)
        else:
            self.b.block("paragraph", text)
        return end + 1


def _non_image_text(inline: Token) -> str:
    """The inline text other than image alt text."""
    return "".join(c.content for c in inline.children or []
                   if c.type in ("text", "code_inline")).strip()


def _table_text(headers: list[str], rows: list[list[str]]) -> str:
    lines = []
    if headers:
        lines.append(" | ".join(headers))
    lines.extend(" | ".join(r) for r in rows)
    return "\n".join(lines)
