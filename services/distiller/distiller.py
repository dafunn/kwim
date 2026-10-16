"""Distiller - consolidates episodic traces into Knowledge facts and Wisdom rules.

Each run reads the team's episodic events past the watermark, asks the LLM for
durable cross-episode learnings, proposes them with their evidence, and then
advances the watermark. One CronJob run per team, identified by its mounted key.
See docs/DESIGN.md, "The client and the distiller".
"""
import asyncio
import json
import logging
import os

import otel

otel.configure()

from langchain_core.messages import HumanMessage, SystemMessage

from kwim import _post, knowledge_propose, read_episodic, require_available, wisdom_propose
from llm_router import make_llm

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

_VALID_DECAY_CLASSES = {"permanent", "slow", "fast"}

WATERMARK_EVENT_TYPE = "distiller_watermark"
DISTILLER_AGENT_ID = "distiller"
WINDOW_LIMIT = int(os.environ.get("DISTILLER_WINDOW_LIMIT", "500"))

_SYSTEM_PROMPT = """You are the KWIM distiller. You are given a window of episodic
events (agent traces) from a single team and must extract durable, cross-episode
learnings - not a summary of any one run.

Two kinds of output:

- "fact": a stable claim about the domain/entities that recurs across episodes.
  Fields: statement (string), fact_type (string), evidence (list of integer ref
  numbers - the "ref" field of the supporting input events),
  decay_class (string - required, one of "permanent", "slow", "fast"),
  about (list of strings - required, the entity/topic keys this fact concerns.
  Include the key subjects/entities the fact is about, lowercased, matching how a
  researcher would name them - this is how the fact gets found again later).
- "advisory": "in situation X, approach Y worked / failed", supported by the
  episodes that show it.
  Fields: situation (object - matchable fields like task_type/platform),
  approach (string), evidence (list of integer ref numbers).

decay_class for facts - you must set this explicitly based on how quickly the
claim goes stale:
- "fast": trends, current activity levels, what's popular/trending right now,
  anything tied to the current moment.
- "slow": typical durable observations - product details, general patterns
  that hold for weeks/months.
- "permanent": fixed entity attributes that essentially never change (e.g. a
  product's category, a platform's name).
When unsure between "slow" and "permanent", prefer "slow".

tool_observation events are raw tool results (e.g. database query results)
fetched by a research agent. A pattern that recurs across them (e.g. the same
trend/observation showing up in multiple tool_observation events) can be
distilled into a fact with the appropriate decay_class - current/trending
observations should be "fast". A single tool result is not a fact.

An agent turn that reports what its tools returned - a listing of tables or
files, log lines, an error, a refused or denied request, what the agent could or
could not access - is a record of that turn, not knowledge about the domain. Do
not propose it as a fact or an advisory.

Rules:
- Cross-episode, not per-episode. Propose something only when multiple episodes
  support it, or one episode supports it very strongly - and always cite the
  supporting events by their integer "ref" number in "evidence".
- Propose, don't decide. You are proposing candidates to a governed gate; a
  duplicate or contradiction will be screened or sent to a human. Prefer
  precision - a wrong durable rule is costly.
- Do not propose constraints (enforcement rules). If you believe one is
  warranted, omit it - constraints are a human authority decision.
- If nothing in this window meets the bar, return an empty list.
- Knowledge the agent recalled from KWIM is listed after the events, under
  "Already in KWIM". Do not propose it, or anything that restates it: an agent
  repeating what it recalled is not new evidence.
- Advisories come from decisions or outcomes that repeat across episodes, not
  from one episode's description of tool output such as log lines or errors.

Respond with only a JSON array (no prose, no markdown fences). Each element is an
object with a "kind" field of "fact" or "advisory" plus the fields listed above.
"""


def _failed_turn(event: dict) -> bool:
    """An agent turn recorded with an outcome other than "ok" (a cli_turn
    records "error" and "interrupted"). Events with no outcome are kept."""
    data = event.get("event_data")
    outcome = data.get("outcome") if isinstance(data, dict) else None
    return outcome is not None and outcome != "ok"


def _format_events(events: list[dict]) -> tuple[str, dict[int, str]]:
    """Present events keyed by a 1-based `ref` index instead of their ids.
    Returns (json_for_prompt, ref->real_event_id)."""
    ref_to_id: dict[int, str] = {}
    compact = []
    for i, e in enumerate(events, start=1):
        ref_to_id[i] = str(e.get("id"))
        data = e.get("event_data")
        if isinstance(data, dict) and "recalled" in data:
            # Recalled knowledge is listed once, apart from the events (_recalled_block).
            data = {k: v for k, v in data.items() if k != "recalled"}
        compact.append({
            "ref": i,
            "agent_id": e.get("agent_id"),
            "event_type": e.get("event_type"),
            "event_data": data,
            "occurred_at": e.get("occurred_at"),
        })
    return json.dumps(compact, default=str), ref_to_id


# Bounds on the "Already in KWIM" block, so a long window cannot crowd out the events.
_RECALLED_MAX_ITEMS = 50
_RECALLED_MAX_CHARS = 300


def _recalled_block(events: list[dict]) -> str:
    """The KWIM items agents recalled during the window's turns, from each
    event's `event_data.recalled` ({kind, id, text}), deduplicated by id. Empty
    when no event records any."""
    seen: set[str] = set()
    lines: list[str] = []
    for e in events:
        data = e.get("event_data")
        items = data.get("recalled") if isinstance(data, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or item.get("text") or "")
            text = str(item.get("text") or "").strip()
            if not key or not text or key in seen:
                continue
            seen.add(key)
            lines.append(f"- [{item.get('kind', 'item')}] {text[:_RECALLED_MAX_CHARS]}")
            if len(lines) >= _RECALLED_MAX_ITEMS:
                break
        if len(lines) >= _RECALLED_MAX_ITEMS:
            break
    if not lines:
        return ""
    return ("Already in KWIM - do not propose these or restatements of them:\n"
            + "\n".join(lines))


def _validate_candidate(item: object, ref_to_id: dict[int, str]) -> dict | None:
    if not isinstance(item, dict):
        log.warning("distiller: dropping non-dict candidate: %r", item)
        return None

    # Map each `ref` back to its event id; drop the candidate if none resolve.
    raw_refs = item.get("evidence")
    if not isinstance(raw_refs, list):
        log.warning("distiller: dropping candidate with non-list evidence: %r", item)
        return None
    evidence: list[str] = []
    for r in raw_refs:
        try:
            rid = ref_to_id.get(int(r))
        except (ValueError, TypeError):
            rid = None
        if rid is not None and rid not in evidence:
            evidence.append(rid)
    if not evidence:
        log.warning("distiller: dropping candidate with no resolvable evidence refs: %r", item)
        return None

    kind = item.get("kind")
    if kind == "fact":
        statement, fact_type = item.get("statement"), item.get("fact_type")
        if not isinstance(statement, str) or not isinstance(fact_type, str):
            log.warning("distiller: dropping malformed fact candidate: %r", item)
            return None
        decay_class = item.get("decay_class")
        if decay_class not in _VALID_DECAY_CLASSES:
            decay_class = None
        raw_about = item.get("about")
        about = [a for a in raw_about if isinstance(a, str) and a] if isinstance(raw_about, list) else []
        return {"kind": "fact", "statement": statement, "fact_type": fact_type,
                "evidence": evidence, "decay_class": decay_class, "about": about}

    if kind == "advisory":
        situation, approach = item.get("situation"), item.get("approach")
        if not isinstance(situation, dict) or not isinstance(approach, str):
            log.warning("distiller: dropping malformed advisory candidate: %r", item)
            return None
        return {"kind": "advisory", "situation": situation, "approach": approach, "evidence": evidence}

    if kind == "constraint":
        # Constraints are never auto-distilled to enforcement - a model
        # suggestion here is dropped, not proposed.
        log.warning("distiller: dropping constraint candidate (human authority decision): %r", item)
        return None

    log.warning("distiller: dropping candidate with unknown kind: %r", item)
    return None


def _extract_json(content: str) -> str:
    """Pull the JSON array out of an LLM response: strip a markdown fence, else
    take the outermost [...]."""
    s = content.strip()
    if s.startswith("```"):
        s = s[3:]
        if s[:4].lower() == "json":
            s = s[4:]
        if "```" in s:
            s = s[: s.rindex("```")]
        s = s.strip()
    if not s.startswith("["):
        start, end = s.find("["), s.rfind("]")
        if start != -1 and end > start:
            s = s[start : end + 1]
    return s.strip()


async def _distill(events: list[dict]) -> list[dict] | None:
    """LLM policy step.

    Returns the candidates (possibly empty), or None if the LLM call or parse
    failed, in which case the watermark must not advance."""
    llm = make_llm(model=os.environ.get("DISTILLER_MODEL"), agent="distiller")
    formatted, ref_to_id = _format_events(events)
    recalled = _recalled_block(events)
    human = f"{formatted}\n\n{recalled}" if recalled else formatted
    try:
        response = await llm.ainvoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=human),
        ])
        content = response.content if isinstance(response.content, str) else str(response.content)
    except Exception as exc:
        log.warning("distiller: LLM call failed (window will be retried): %s", exc)
        return None

    try:
        raw = json.loads(_extract_json(content))
    except Exception as exc:
        # Log the raw content (truncated) so an empty response vs. a malformed
        # one is diagnosable.
        log.warning("distiller: LLM response not parseable as JSON (window will be retried): "
                    "%s; raw=%r", exc, content[:500])
        return None

    if not isinstance(raw, list):
        # Valid JSON but not a list: logged, and the window is not retried.
        log.warning("distiller: LLM response was valid JSON but not a list, dropping: %r", raw)
        return []

    return [c for item in raw if (c := _validate_candidate(item, ref_to_id)) is not None]


async def _propose(candidate: dict) -> dict | None:
    if candidate["kind"] == "fact":
        return await knowledge_propose(
            statement=candidate["statement"],
            fact_type=candidate["fact_type"],
            evidence=candidate["evidence"],
            decay_class=candidate.get("decay_class"),
            about=candidate.get("about", []),
            source_kind="distiller",
        )
    return await wisdom_propose(
        "advisory",
        situation=candidate["situation"],
        approach=candidate["approach"],
        evidence=candidate["evidence"],
        source_kind="distiller",
    )


async def _load_watermark() -> tuple[str | None, str | None]:
    # The latest watermark event: newest first, limit 1.
    result = await read_episodic(
        event_type=WATERMARK_EVENT_TYPE, limit=1, order="desc", strict=True
    )
    events = result.get("events") or []
    if not events:
        return None, None
    data = events[0].get("event_data") or {}
    return data.get("last_ts"), data.get("last_id")


async def _advance_watermark(cursor: dict) -> None:
    # Awaited, not fire-and-forget: the job exits right after run().
    await _post("/v1/memory/episodic", {
        "agent_id": DISTILLER_AGENT_ID,
        "session_id": "distiller",
        "event_type": WATERMARK_EVENT_TYPE,
        "event_data": {"last_ts": cursor["ts"], "last_id": cursor["id"]},
    })


async def run() -> None:
    # Fail if KWIM is not configured, instead of reading an empty window.
    require_available()

    last_ts, last_id = await _load_watermark()
    window = await read_episodic(
        since_ts=last_ts, since_id=last_id, limit=WINDOW_LIMIT, strict=True
    )
    raw_events = window.get("events") or []
    if not raw_events:
        log.info("distiller: empty window, nothing to distill")
        return

    # Self-event exclusion - never distill the distiller's own bookkeeping.
    events = [e for e in raw_events if e.get("event_type") != WATERMARK_EVENT_TYPE]
    # A turn that errored or was interrupted is evidence of nothing durable.
    failed = [e for e in events if _failed_turn(e)]
    if failed:
        log.info("distiller: skipping %d failed or interrupted turn(s)", len(failed))
        events = [e for e in events if not _failed_turn(e)]

    proposals_ok = True
    distill_failed = False
    if events:
        candidates = await _distill(events)
        if candidates is None:
            # Leave the watermark so the window is retried.
            distill_failed = True
        else:
            log.info("distiller: %d candidate(s) from %d event(s)", len(candidates), len(events))
            for candidate in candidates:
                result = await _propose(candidate)
                if result is None:
                    proposals_ok = False
                    log.warning("distiller: propose failed for kind=%s - window will be retried", candidate["kind"])
    else:
        log.info("distiller: window contained only bookkeeping events and failed turns")

    next_cursor = window.get("next_cursor")
    if distill_failed:
        log.warning("distiller: not advancing watermark - distill failed, window will be retried")
    elif proposals_ok and next_cursor:
        await _advance_watermark(next_cursor)
    elif not proposals_ok:
        log.warning("distiller: not advancing watermark past failed window")


if __name__ == "__main__":
    asyncio.run(run())
