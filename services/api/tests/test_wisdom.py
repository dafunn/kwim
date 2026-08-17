"""Pure-logic tests for the Wisdom gate decision paths and wisdom.check matcher.

These tests cover only in-process logic (no external services - no FalkorDB,
Postgres, or RabbitMQ).

The _decide / _check logic below is copied from gate.py and main.py; any drift
between the copy and the real code is with the test, not a contract violation.
(The end-to-end gate paths are exercised in test_gate.py.)
"""
import re
from typing import Any

import pytest

_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

GATE_THRESHOLD = 3  # mirrors settings.gate_auto_commit_threshold default


def _decide(ptype: str, body: dict[str, Any]) -> tuple[str, str]:
    """Mirrors Gate._decide (gate.py)."""
    if ptype == "fact":
        return "commit", "auto_committed"
    if body.get("rule_type") == "constraint":
        return "review", "human_approved"
    # advisory
    if len(body.get("evidence", [])) >= GATE_THRESHOLD:
        return "commit", "auto_committed"
    return "review", "human_approved"


def _check(constraints: list[dict], action: dict[str, Any]) -> dict[str, Any]:
    """Mirrors the wisdom.check deterministic matcher logic (main.py)."""
    target = action.get("content", "")
    matches = []
    for r in constraints:
        if r.get("check_tier") != "deterministic" or not r.get("action_pattern"):
            continue
        try:
            if re.search(r["action_pattern"], target):
                matches.append(r)
        except re.error:
            continue
    if not matches:
        return {"verdict": "allow", "matched_rule": None}
    best = max(matches, key=lambda r: _SEVERITY_ORDER.get(r.get("severity", ""), 0))
    if _SEVERITY_ORDER.get(best.get("severity", ""), 0) >= _SEVERITY_ORDER["critical"]:
        verdict = "escalate"
    else:
        verdict = best.get("verdict", "allow")
    return {"verdict": verdict, "matched_rule": best["id"], "reason": best.get("authority")}


def _should_reinforce(ptype: str, body: dict) -> bool:
    return ptype == "rule" and bool(body.get("reinforces"))


# ---------------------------------------------------------------------------
# Gate._decide - regression table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ptype,body,expected", [
    ("fact", {}, ("commit", "auto_committed")),
    ("rule", {"rule_type": "constraint"}, ("review", "human_approved")),
    ("rule", {"rule_type": "advisory", "evidence": ["a", "b"]}, ("review", "human_approved")),
    ("rule", {"rule_type": "advisory", "evidence": ["a", "b", "c"]}, ("commit", "auto_committed")),
    ("rule", {"rule_type": "advisory", "evidence": ["a", "b", "c", "d"]}, ("commit", "auto_committed")),
    ("rule", {"rule_type": "advisory", "evidence": []}, ("review", "human_approved")),
])
def test_decide_table(ptype, body, expected):
    assert _decide(ptype, body) == expected


# ---------------------------------------------------------------------------
# reinforce short-circuit detection (gate.handle branch condition)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ptype,body,expected", [
    ("rule", {"rule_type": "advisory", "reinforces": "rule-123"}, True),
    ("rule", {"rule_type": "advisory"}, False),
    ("rule", {"rule_type": "advisory", "reinforces": None}, False),
    ("fact", {"reinforces": "rule-123"}, False),  # fact path, not rule
])
def test_should_reinforce(ptype, body, expected):
    assert _should_reinforce(ptype, body) == expected


# ---------------------------------------------------------------------------
# wisdom.check - deterministic matcher
# ---------------------------------------------------------------------------

_C_DENY = {"id": "c1", "rule_type": "constraint", "action_pattern": r"badword",
           "check_tier": "deterministic", "severity": "medium", "verdict": "deny",
           "authority": "project-policy"}
_C_ESCALATE = {"id": "c2", "rule_type": "constraint", "action_pattern": r"dangerous",
               "check_tier": "deterministic", "severity": "high", "verdict": "escalate",
               "authority": "legal"}
_C_CRITICAL = {"id": "c3", "rule_type": "constraint", "action_pattern": r"nuclear",
               "check_tier": "deterministic", "severity": "critical", "verdict": "deny",
               "authority": "compliance"}


def test_check_no_constraints_allows():
    assert _check([], {"content": "anything"})["verdict"] == "allow"


def test_check_no_match_allows():
    assert _check([_C_DENY], {"content": "hello world"})["verdict"] == "allow"


def test_check_single_match_uses_stored_verdict():
    assert _check([_C_DENY], {"content": "contains badword here"})["verdict"] == "deny"
    assert _check([_C_DENY], {"content": "badword"})["matched_rule"] == "c1"


def test_check_critical_escalates_regardless_of_verdict():
    assert _check([_C_CRITICAL], {"content": "nuclear option"})["verdict"] == "escalate"


def test_check_highest_severity_wins():
    assert _check([_C_DENY, _C_ESCALATE], {"content": "badword and dangerous"})["verdict"] == "escalate"
    assert _check([_C_ESCALATE, _C_CRITICAL], {"content": "dangerous nuclear"})["verdict"] == "escalate"


def test_check_empty_or_missing_content_no_match():
    assert _check([_C_DENY], {"content": ""})["verdict"] == "allow"
    assert _check([_C_DENY], {})["verdict"] == "allow"


def test_check_classifier_tier_rule_skipped():
    rule = {"id": "c4", "rule_type": "constraint", "action_pattern": "badword",
            "check_tier": "classifier", "severity": "critical", "verdict": "deny"}
    assert _check([rule], {"content": "badword"})["verdict"] == "allow"


def test_check_malformed_regex_tolerated():
    rule = {"id": "c5", "rule_type": "constraint", "action_pattern": "[invalid",
            "check_tier": "deterministic", "severity": "high", "verdict": "deny"}
    assert _check([rule], {"content": "anything"})["verdict"] == "allow"


# ---------------------------------------------------------------------------
# Model field smoke-tests (import the real models)
# ---------------------------------------------------------------------------

def test_model_fields():
    from kwim_api.models import AdvisoryProposal, CheckResult, Rule

    ap = AdvisoryProposal(situation={"project": "demoproject"}, approach="do x",
                          evidence=["ev1", "ev2", "ev3"], reinforces="rule-abc")
    assert ap.reinforces == "rule-abc"

    ap_none = AdvisoryProposal(situation={}, approach="y", evidence=[])
    assert ap_none.reinforces is None

    rule = Rule(id="r1", rule_type="advisory", status="approved")
    assert rule.scope == "team"

    rule_u = Rule(id="r2", rule_type="constraint", status="approved", scope="universe")
    assert rule_u.scope == "universe"

    cr = CheckResult(verdict="deny", matched_rule="c1", reason="policy",
                     check_tier="deterministic")
    assert cr.verdict == "deny"
