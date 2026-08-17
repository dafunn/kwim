"""LLM factory for the KWIM Intelligence inference gateway (LiteLLM).

Routes all agent inference through LiteLLM - ChatOpenAI-compatible client,
base URL and key driven by env.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from secret_reader import read_secret

if TYPE_CHECKING:
    # Type-checking only: the runtime import stays inside make_llm so the base
    # client remains httpx-only and langchain is an extra, not a dependency.
    from langchain_openai import ChatOpenAI

# Off-cluster fallback only; in-cluster deployments override this via the
# LITELLM_BASE_URL env (cluster DNS), and the eval harness points it at the
# gateway. No environment-specific address is baked in - set LITELLM_BASE_URL.
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1")


def _env_tags() -> dict[str, str]:
    """Deployment-declared spend tags from ``LITELLM_TAGS``.

    Format is a comma-separated ``key:value`` list, e.g.
    ``LITELLM_TAGS=host:host1,cluster:us-east``. Lets a deployment attribute
    spend by whatever groupings it cares about (or none) without a code change;
    KWIM attaches the tags but ascribes no meaning to them.
    """
    out: dict[str, str] = {}
    for part in os.environ.get("LITELLM_TAGS", "").split(","):
        key, sep, val = part.partition(":")
        if sep and key.strip() and val.strip():
            out[key.strip()] = val.strip()
    return out


def _litellm_tags(agent: str | None, tags: Mapping[str, str] | None) -> str | None:
    """Build the comma-separated ``x-litellm-tags`` value.

    ``agent`` is the calling service - a grouping-neutral infra dimension.
    ``tags`` is an open set of caller-defined ``key:value`` groupings; the
    factory attaches whatever it is given and ascribes no meaning to them. A
    caller wanting per-cluster accounting passes ``{"cluster": ...}``.
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

    Explicit ``model`` wins; otherwise the deployment's ``DEFAULT_LLM_MODEL``.
    Raises ``RuntimeError`` if neither is set - the model must be configured,
    never silently defaulted. Callers pass ``os.environ.get("<SVC>_MODEL")``
    (which may be None) and let this resolve it.
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

    Key resolution: when ``LLM_API_KEY_SECRET`` is set, that secret is used
    directly (cluster path - the manifest sets it). Otherwise falls back to
    ``litellm-key`` (a team virtual key). Each consuming deployment sets
    ``LLM_API_KEY_SECRET`` to its own team's key secret.

    ``agent`` (arg or ``KWIM_AGENT``) plus any caller-supplied ``tags``, merged
    with deployment-declared ``LITELLM_TAGS``, are emitted as the
    ``x-litellm-tags`` header (``agent:<agent>[,<key>:<val>...]``), which LiteLLM
    records in LiteLLM_SpendLogs / LiteLLM_DailyTagSpend for per-agent and
    per-grouping token + cost tracking and audit attribution. Call-site ``tags``
    win over ``LITELLM_TAGS`` on key collision. KWIM ascribes no meaning to the
    grouping keys - a team defines its own (location, cluster, ...) or none.

    The httpx client used internally by langchain-openai is instrumented by
    otel.py at startup, so outbound requests will carry a traceparent header
    if the OTel SDK is initialised before this is called.
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
