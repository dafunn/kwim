"""Opaque keyset cursors for /v1/admin list endpoints: each resource's keyset,
base64-encoded. See docs/DESIGN.md, "The admin API".
"""
import base64
import json
from typing import Any

from fastapi import HTTPException, status


def encode_cursor(values: dict[str, Any]) -> str:
    """Base64 a keyset dict into an opaque cursor."""
    raw = json.dumps(values, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> dict[str, Any]:
    """Reverse encode_cursor. Raises HTTPException(422) on malformed input."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        values = json.loads(raw)
    except Exception:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="malformed cursor") from None
    if not isinstance(values, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="malformed cursor")
    return values


def next_cursor(rows: list, limit: int, keyset) -> str | None:
    """The cursor after a full page of `rows`, or None after a short one.
    `keyset` maps the last row to its cursor dict."""
    if not rows or len(rows) < limit:
        return None
    return encode_cursor(keyset(rows[-1]))
