"""What an extractor returns."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Extraction:
    extracted: dict[str, Any]
    extractor: str
    extractor_version: str
    page_count: int | None = None
    warnings: list[str] = field(default_factory=list)
