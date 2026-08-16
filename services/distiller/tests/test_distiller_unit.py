"""Unit tests for the distiller job.

These exercise app.run() end-to-end against fakes for the KWIM client surface
(read_episodic / knowledge_propose / wisdom_propose / _post) and the LLM
(make_llm) - no real KWIM service or LLM involved. The `distiller_app` fixture
is provided by conftest.py.
"""
import json
from unittest.mock import AsyncMock

import pytest


class _FakeLLM:
    def __init__(self, content):
        self._content = content
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)

        class _R:
            content = self._content

        return _R()


_T1 = "2026-06-11T10:00:00+00:00"
_T2 = "2026-06-11T11:00:00+00:00"

_EVENTS = [
    {"id": "evt-1", "agent_id": "test", "session_id": "s1", "event_type": "research_complete",
     "event_data": {"summary": "trend A recurs"}, "occurred_at": _T1},
    {"id": "evt-2", "agent_id": "test", "session_id": "s1", "event_type": "research_complete",
     "event_data": {"summary": "trend A recurs again"}, "occurred_at": _T2},
]
_NEXT_CURSOR = {"ts": _T2, "id": "evt-2"}


def _no_watermark_window(window_events, next_cursor):
    """read_episodic fake: empty watermark, given window for the actual read."""
    async def _read_episodic(since_ts=None, since_id=None, limit=500, event_type=None, agent_id=None, order="asc", strict=False):
        if event_type == "distiller_watermark":
            return {"events": [], "next_cursor": None}
        return {"events": window_events, "next_cursor": next_cursor}
    return _read_episodic


class TestEmptyWindow:
    @pytest.mark.asyncio
    async def test_empty_window_exits_without_proposing(self, distiller_app, monkeypatch):
        monkeypatch.setattr(distiller_app, "read_episodic", _no_watermark_window([], None))

        knowledge_propose = AsyncMock()
        wisdom_propose = AsyncMock()
        post = AsyncMock()
        monkeypatch.setattr(distiller_app, "knowledge_propose", knowledge_propose)
        monkeypatch.setattr(distiller_app, "wisdom_propose", wisdom_propose)
        monkeypatch.setattr(distiller_app, "_post", post)

        await distiller_app.run()

        knowledge_propose.assert_not_called()
        wisdom_propose.assert_not_called()
        post.assert_not_called()


class TestWatermarkLoad:
    @pytest.mark.asyncio
    async def test_existing_watermark_used_as_cursor(self, distiller_app, monkeypatch):
        watermark_events = [
            {"id": "wm-1", "agent_id": "distiller", "session_id": "distiller",
             "event_type": "distiller_watermark", "event_data": {"last_ts": _T1, "last_id": "evt-1"},
             "occurred_at": _T1},
        ]
        calls = []

        async def fake_read_episodic(since_ts=None, since_id=None, limit=500, event_type=None, agent_id=None, order="asc", strict=False):
            calls.append({"since_ts": since_ts, "since_id": since_id, "event_type": event_type})
            if event_type == "distiller_watermark":
                return {"events": watermark_events, "next_cursor": {"ts": _T1, "id": "wm-1"}}
            return {"events": [], "next_cursor": None}

        monkeypatch.setattr(distiller_app, "read_episodic", fake_read_episodic)
        monkeypatch.setattr(distiller_app, "_post", AsyncMock())

        await distiller_app.run()

        window_call = calls[1]
        assert window_call["since_ts"] == _T1
        assert window_call["since_id"] == "evt-1"


class TestDistillAndAdvance:
    @pytest.mark.asyncio
    async def test_well_formed_candidates_proposed_and_watermark_advanced(self, distiller_app, monkeypatch):
        monkeypatch.setattr(distiller_app, "read_episodic", _no_watermark_window(_EVENTS, _NEXT_CURSOR))

        candidates = json.dumps([
            {"kind": "fact", "statement": "Trend A recurs", "fact_type": "observation",
             "evidence": [1, 2], "about": ["demoproject", "trend a"]},
            {"kind": "advisory", "situation": {"project": "demoproject"}, "approach": "Lead with trend A",
             "evidence": [1, 2]},
        ])
        monkeypatch.setattr(distiller_app, "make_llm", lambda *a, **kw: _FakeLLM(candidates))

        knowledge_propose = AsyncMock(return_value={"proposal_id": "p1"})
        wisdom_propose = AsyncMock(return_value={"proposal_id": "p2"})
        post = AsyncMock(return_value={"event_id": "wm1"})
        monkeypatch.setattr(distiller_app, "knowledge_propose", knowledge_propose)
        monkeypatch.setattr(distiller_app, "wisdom_propose", wisdom_propose)
        monkeypatch.setattr(distiller_app, "_post", post)

        await distiller_app.run()

        knowledge_propose.assert_awaited_once_with(
            statement="Trend A recurs", fact_type="observation", evidence=["evt-1", "evt-2"],
            decay_class=None, about=["demoproject", "trend a"], source_kind="distiller",
        )
        wisdom_propose.assert_awaited_once_with(
            "advisory", situation={"project": "demoproject"}, approach="Lead with trend A",
            evidence=["evt-1", "evt-2"], source_kind="distiller",
        )

        post.assert_awaited_once()
        path, body = post.await_args.args
        assert path == "/v1/memory/episodic"
        assert body["event_type"] == "distiller_watermark"
        assert body["event_data"] == {"last_ts": _T2, "last_id": "evt-2"}

    @pytest.mark.asyncio
    async def test_one_malformed_candidate_dropped_others_proposed(self, distiller_app, monkeypatch):
        monkeypatch.setattr(distiller_app, "read_episodic", _no_watermark_window(_EVENTS, _NEXT_CURSOR))

        candidates = json.dumps([
            {"kind": "fact", "statement": "ok fact", "fact_type": "observation", "evidence": [1]},
            {"kind": "fact", "statement": "missing fact_type", "evidence": [1]},
            {"kind": "advisory", "situation": "not-a-dict", "approach": "x", "evidence": [1]},
        ])
        monkeypatch.setattr(distiller_app, "make_llm", lambda *a, **kw: _FakeLLM(candidates))

        knowledge_propose = AsyncMock(return_value={"proposal_id": "p1"})
        wisdom_propose = AsyncMock()
        monkeypatch.setattr(distiller_app, "knowledge_propose", knowledge_propose)
        monkeypatch.setattr(distiller_app, "wisdom_propose", wisdom_propose)
        monkeypatch.setattr(distiller_app, "_post", AsyncMock(return_value={"event_id": "wm1"}))

        await distiller_app.run()

        knowledge_propose.assert_awaited_once_with(
            statement="ok fact", fact_type="observation", evidence=["evt-1"],
            decay_class=None, about=[], source_kind="distiller",
        )
        wisdom_propose.assert_not_called()


class TestSelfEventExclusion:
    @pytest.mark.asyncio
    async def test_watermark_events_excluded_from_distill_window(self, distiller_app, monkeypatch):
        events_with_watermark = _EVENTS + [
            {"id": "evt-wm", "agent_id": "distiller", "session_id": "distiller",
             "event_type": "distiller_watermark", "event_data": {"last_ts": _T1, "last_id": "evt-1"},
             "occurred_at": _T1},
        ]
        monkeypatch.setattr(
            distiller_app, "read_episodic", _no_watermark_window(events_with_watermark, _NEXT_CURSOR)
        )

        fake_llm = _FakeLLM("[]")
        monkeypatch.setattr(distiller_app, "make_llm", lambda *a, **kw: fake_llm)
        monkeypatch.setattr(distiller_app, "knowledge_propose", AsyncMock())
        monkeypatch.setattr(distiller_app, "wisdom_propose", AsyncMock())
        monkeypatch.setattr(distiller_app, "_post", AsyncMock(return_value={"event_id": "wm1"}))

        await distiller_app.run()

        assert len(fake_llm.calls) == 1
        human_content = fake_llm.calls[0][-1].content
        assert "distiller_watermark" not in human_content   # watermark event excluded
        # Events are presented by `ref` index now (not raw id); the real events
        # content is present, the watermark's is not.
        assert "trend A recurs" in human_content
        assert '"ref": 1' in human_content and '"ref": 2' in human_content


class TestMalformedDistillResponse:
    @pytest.mark.asyncio
    async def test_non_json_response_does_not_advance_watermark(self, distiller_app, monkeypatch):
        # A non-JSON (failed) distill must retry the window, not silently skip it.
        monkeypatch.setattr(distiller_app, "read_episodic", _no_watermark_window(_EVENTS, _NEXT_CURSOR))
        monkeypatch.setattr(distiller_app, "make_llm", lambda *a, **kw: _FakeLLM("not json at all"))

        knowledge_propose = AsyncMock()
        wisdom_propose = AsyncMock()
        post = AsyncMock(return_value={"event_id": "wm1"})
        monkeypatch.setattr(distiller_app, "knowledge_propose", knowledge_propose)
        monkeypatch.setattr(distiller_app, "wisdom_propose", wisdom_propose)
        monkeypatch.setattr(distiller_app, "_post", post)

        await distiller_app.run()

        knowledge_propose.assert_not_called()
        wisdom_propose.assert_not_called()
        # Distill failed -> watermark must not advance (window retried next run).
        post.assert_not_called()

    @pytest.mark.asyncio
    async def test_markdown_fenced_json_is_parsed(self, distiller_app, monkeypatch):
        # Smaller models often wrap JSON in a ```json fence; the extractor must
        # strip it so the candidate is proposed (not lost to a char-0 parse error).
        monkeypatch.setattr(distiller_app, "read_episodic", _no_watermark_window(_EVENTS, _NEXT_CURSOR))
        fenced = '```json\n[{"kind": "fact", "statement": "Trend A recurs", ' \
                 '"fact_type": "observation", "evidence": [1, 2]}]\n```'
        monkeypatch.setattr(distiller_app, "make_llm", lambda *a, **kw: _FakeLLM(fenced))

        knowledge_propose = AsyncMock(return_value={"proposal_id": "p1"})
        post = AsyncMock(return_value={"event_id": "wm1"})
        monkeypatch.setattr(distiller_app, "knowledge_propose", knowledge_propose)
        monkeypatch.setattr(distiller_app, "wisdom_propose", AsyncMock())
        monkeypatch.setattr(distiller_app, "_post", post)

        await distiller_app.run()

        knowledge_propose.assert_awaited_once()       # fenced JSON parsed -> proposed
        post.assert_awaited_once()                    # success -> watermark advances


class TestFailSoft:
    @pytest.mark.asyncio
    async def test_propose_failure_does_not_advance_watermark(self, distiller_app, monkeypatch):
        monkeypatch.setattr(distiller_app, "read_episodic", _no_watermark_window(_EVENTS, _NEXT_CURSOR))

        candidates = json.dumps([
            {"kind": "fact", "statement": "x", "fact_type": "observation", "evidence": [1]},
        ])
        monkeypatch.setattr(distiller_app, "make_llm", lambda *a, **kw: _FakeLLM(candidates))
        monkeypatch.setattr(distiller_app, "knowledge_propose", AsyncMock(return_value=None))
        monkeypatch.setattr(distiller_app, "wisdom_propose", AsyncMock())
        post = AsyncMock()
        monkeypatch.setattr(distiller_app, "_post", post)

        await distiller_app.run()

        post.assert_not_called()


class TestDecayClass:
    @pytest.mark.asyncio
    async def test_tool_observation_trend_proposed_with_explicit_decay_class(self, distiller_app, monkeypatch):
        """A tool_observation-derived trend candidate carries decay_class=fast through to knowledge_propose."""
        tool_obs_events = [
            {"id": "evt-1", "agent_id": "test", "session_id": "s1", "event_type": "tool_observation",
             "event_data": {"tool": "pg_query", "result": "[{\"phrase\": \"chaos energy\"}]",
                             "subject": "demoproject", "project": "demoproject"}, "occurred_at": _T1},
            {"id": "evt-2", "agent_id": "test", "session_id": "s1", "event_type": "tool_observation",
             "event_data": {"tool": "pg_query", "result": "[{\"phrase\": \"chaos energy\"}]",
                             "subject": "demoproject", "project": "demoproject"}, "occurred_at": _T2},
        ]
        monkeypatch.setattr(distiller_app, "read_episodic", _no_watermark_window(tool_obs_events, _NEXT_CURSOR))

        candidates = json.dumps([
            {"kind": "fact", "statement": "'chaos energy' is a recurring trending phrase for demoproject",
             "fact_type": "trend", "decay_class": "fast", "evidence": [1, 2],
             "about": ["demoproject", "chaos energy"]},
        ])
        monkeypatch.setattr(distiller_app, "make_llm", lambda *a, **kw: _FakeLLM(candidates))

        knowledge_propose = AsyncMock(return_value={"proposal_id": "p1"})
        monkeypatch.setattr(distiller_app, "knowledge_propose", knowledge_propose)
        monkeypatch.setattr(distiller_app, "wisdom_propose", AsyncMock())
        monkeypatch.setattr(distiller_app, "_post", AsyncMock(return_value={"event_id": "wm1"}))

        await distiller_app.run()

        knowledge_propose.assert_awaited_once_with(
            statement="'chaos energy' is a recurring trending phrase for demoproject",
            fact_type="trend", evidence=["evt-1", "evt-2"], decay_class="fast",
            about=["demoproject", "chaos energy"], source_kind="distiller",
        )

    @pytest.mark.asyncio
    async def test_invalid_decay_class_dropped_to_none(self, distiller_app, monkeypatch):
        """An out-of-range decay_class from the LLM is dropped (gate falls back to its map)."""
        monkeypatch.setattr(distiller_app, "read_episodic", _no_watermark_window(_EVENTS, _NEXT_CURSOR))

        candidates = json.dumps([
            {"kind": "fact", "statement": "x", "fact_type": "observation",
             "decay_class": "bogus", "evidence": [1]},
        ])
        monkeypatch.setattr(distiller_app, "make_llm", lambda *a, **kw: _FakeLLM(candidates))

        knowledge_propose = AsyncMock(return_value={"proposal_id": "p1"})
        monkeypatch.setattr(distiller_app, "knowledge_propose", knowledge_propose)
        monkeypatch.setattr(distiller_app, "wisdom_propose", AsyncMock())
        monkeypatch.setattr(distiller_app, "_post", AsyncMock(return_value={"event_id": "wm1"}))

        await distiller_app.run()

        knowledge_propose.assert_awaited_once_with(
            statement="x", fact_type="observation", evidence=["evt-1"], decay_class=None,
            about=[], source_kind="distiller",
        )


class TestAbout:
    @pytest.mark.asyncio
    async def test_about_passed_through_to_knowledge_propose(self, distiller_app, monkeypatch):
        monkeypatch.setattr(distiller_app, "read_episodic", _no_watermark_window(_EVENTS, _NEXT_CURSOR))

        candidates = json.dumps([
            {"kind": "fact", "statement": "x", "fact_type": "observation",
             "evidence": [1], "about": ["demoproject", "friday"]},
        ])
        monkeypatch.setattr(distiller_app, "make_llm", lambda *a, **kw: _FakeLLM(candidates))

        knowledge_propose = AsyncMock(return_value={"proposal_id": "p1"})
        monkeypatch.setattr(distiller_app, "knowledge_propose", knowledge_propose)
        monkeypatch.setattr(distiller_app, "wisdom_propose", AsyncMock())
        monkeypatch.setattr(distiller_app, "_post", AsyncMock(return_value={"event_id": "wm1"}))

        await distiller_app.run()

        knowledge_propose.assert_awaited_once_with(
            statement="x", fact_type="observation", evidence=["evt-1"], decay_class=None,
            about=["demoproject", "friday"], source_kind="distiller",
        )

    @pytest.mark.asyncio
    async def test_non_string_about_entries_dropped(self, distiller_app, monkeypatch):
        monkeypatch.setattr(distiller_app, "read_episodic", _no_watermark_window(_EVENTS, _NEXT_CURSOR))

        candidates = json.dumps([
            {"kind": "fact", "statement": "x", "fact_type": "observation",
             "evidence": [1], "about": ["demoproject", 42, "", None]},
        ])
        monkeypatch.setattr(distiller_app, "make_llm", lambda *a, **kw: _FakeLLM(candidates))

        knowledge_propose = AsyncMock(return_value={"proposal_id": "p1"})
        monkeypatch.setattr(distiller_app, "knowledge_propose", knowledge_propose)
        monkeypatch.setattr(distiller_app, "wisdom_propose", AsyncMock())
        monkeypatch.setattr(distiller_app, "_post", AsyncMock(return_value={"event_id": "wm1"}))

        await distiller_app.run()

        knowledge_propose.assert_awaited_once_with(
            statement="x", fact_type="observation", evidence=["evt-1"], decay_class=None,
            about=["demoproject"], source_kind="distiller",
        )


class TestConstraintCandidatesNotAutoEmitted:
    @pytest.mark.asyncio
    async def test_constraint_candidate_not_proposed(self, distiller_app, monkeypatch):
        monkeypatch.setattr(distiller_app, "read_episodic", _no_watermark_window(_EVENTS, _NEXT_CURSOR))

        candidates = json.dumps([
            {"kind": "constraint", "action_pattern": "post:.*", "verdict": "deny",
             "authority": "project_lead", "severity": "high", "check_tier": "deterministic",
             "evidence": [1]},
        ])
        monkeypatch.setattr(distiller_app, "make_llm", lambda *a, **kw: _FakeLLM(candidates))

        knowledge_propose = AsyncMock()
        wisdom_propose = AsyncMock()
        monkeypatch.setattr(distiller_app, "knowledge_propose", knowledge_propose)
        monkeypatch.setattr(distiller_app, "wisdom_propose", wisdom_propose)
        monkeypatch.setattr(distiller_app, "_post", AsyncMock(return_value={"event_id": "wm1"}))

        await distiller_app.run()

        knowledge_propose.assert_not_called()
        wisdom_propose.assert_not_called()


class TestPreflight:
    """Regression: a misconfigured distiller must crash, not report success.
    """

    @pytest.mark.asyncio
    async def test_run_raises_when_preflight_fails(self, distiller_app, monkeypatch):
        from kwim import KwimUnavailable

        def _boom():
            raise KwimUnavailable("no key")

        monkeypatch.setattr(distiller_app, "require_available", _boom)
        # If run() ever swallows this, the job exits 0 and the outage is invisible
        with pytest.raises(KwimUnavailable):
            await distiller_app.run()

    @pytest.mark.asyncio
    async def test_preflight_runs_before_any_read(self, distiller_app, monkeypatch):
        """The preflight must gate the reads, not trail them."""
        from kwim import KwimUnavailable

        read_episodic = AsyncMock()
        monkeypatch.setattr(distiller_app, "read_episodic", read_episodic)

        def _boom():
            raise KwimUnavailable("no key")

        monkeypatch.setattr(distiller_app, "require_available", _boom)

        with pytest.raises(KwimUnavailable):
            await distiller_app.run()

        read_episodic.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_window_read_does_not_look_like_an_empty_window(
        self, distiller_app, monkeypatch
    ):
        """A strict read that raises must propagate, not advance the watermark."""
        from kwim import KwimUnavailable

        async def _failing_read(**kwargs):
            raise KwimUnavailable("read failed")

        monkeypatch.setattr(distiller_app, "read_episodic", _failing_read)
        post = AsyncMock()
        monkeypatch.setattr(distiller_app, "_post", post)

        with pytest.raises(KwimUnavailable):
            await distiller_app.run()

        post.assert_not_called()

    @pytest.mark.asyncio
    async def test_distiller_requests_strict_reads(self, distiller_app, monkeypatch):
        """Both reads must opt into strict, or the silent path stays open."""
        seen = []

        async def _recording_read(since_ts=None, since_id=None, limit=500,
                                  event_type=None, agent_id=None, order="asc",
                                  strict=False):
            seen.append(strict)
            return {"events": [], "next_cursor": None}

        monkeypatch.setattr(distiller_app, "read_episodic", _recording_read)

        await distiller_app.run()

        assert seen, "run() performed no reads"
        assert all(seen), f"non-strict read(s) in the distiller: {seen}"
