"""LLM factory for the KWIM Intelligence inference gateway (LiteLLM).

A ChatOpenAI client with base URL and key from the environment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from secret_reader import read_secret

if TYPE_CHECKING:
    # Imported at run time inside make_llm; langchain is an optional extra.
    from langchain_openai import ChatOpenAI

# Default when LITELLM_BASE_URL is unset.
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1")


def _env_tags() -> dict[str, str]:
    """Deployment-declared spend tags from ``LITELLM_TAGS``.

    A comma-separated ``key:value`` list, e.g.
    ``LITELLM_TAGS=host:host1,cluster:us-east``.
    """
    out: dict[str, str] = {}
    for part in os.environ.get("LITELLM_TAGS", "").split(","):
        key, sep, val = part.partition(":")
        if sep and key.strip() and val.strip():
            out[key.strip()] = val.strip()
    return out


def _litellm_tags(agent: str | None, tags: Mapping[str, str] | None) -> str | None:
    """Build the comma-separated ``x-litellm-tags`` value.

    ``agent`` is the calling service; ``tags`` are caller-defined ``key:value``
    pairs, passed through unchanged.
    """
    out: list[str] = []
    if agent:
        out.append(f"agent:{agent}")
    for key, val in (tags or {}).items():
        if val:
            out.append(f"{key}:{val}")
    return ",".join(out) if out else None


def resolve_model(model: str | None = None) -> str:
    """Resolve the effective model name from config - never hardcoded.

    ``model`` if given, else ``DEFAULT_LLM_MODEL``; raises ``RuntimeError`` if
    neither is set.
    """
    resolved = model or os.environ.get("DEFAULT_LLM_MODEL")
    if not resolved:
        raise RuntimeError(
            "No LLM model configured: set DEFAULT_LLM_MODEL (or the service's "
            "<SVC>_MODEL) env var, or pass model=... explicitly."
        )
    return resolved


def make_llm(
    model: str | None = None,
    temperature: float = 0.7,
    agent: str | None = None,
    tags: Mapping[str, str] | None = None,
    **kwargs,
) -> ChatOpenAI:
    """Return a ChatOpenAI pointed at the LiteLLM gateway.

    The key is the secret named by ``LLM_API_KEY_SECRET``, else ``litellm-key``.
    ``agent`` (or ``KWIM_AGENT``) and ``tags``, merged over ``LITELLM_TAGS``, are
    sent as the ``x-litellm-tags`` header (``agent:<agent>[,<key>:<val>...]``) for
    spend attribution.
    """
    model = resolve_model(model)
    if agent is None:
        agent = os.environ.get("KWIM_AGENT")

    key_secret = os.environ.get("LLM_API_KEY_SECRET") or "litellm-key"

    # Per-agent + per-grouping attribution - LiteLLM reads x-litellm-tags.
    merged_tags = {**_env_tags(), **(tags or {})}  # call-site wins on collision
    tag_str = _litellm_tags(agent, merged_tags)
    default_headers = {"x-litellm-tags": tag_str} if tag_str else None

    from langchain_openai import ChatOpenAI  # lazy - keeps the base client light
    return ChatOpenAI(
        base_url=LITELLM_BASE_URL,
        api_key=read_secret(key_secret),
        model=model,
        temperature=temperature,
        default_headers=default_headers,
        **kwargs,
    )
