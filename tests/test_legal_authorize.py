"""Friday v1 Slice L3: the Legal authorization entry point.

Order is the contract: validate the grant (precondition), resolve the
plan, evaluate the four rules. A missing or expired grant fails closed
before resolution; a resolution failure fails closed through the generic
unknown-impact decision; neither becomes a Legal rule.
"""

import ast
import datetime
import inspect
from pathlib import Path
from unittest import mock

import pytest

import proofgate.bindings.legal.authorize as authorize_module
from legal.models import AuthorityGrant
from legal.permissions import SEEDED_AGENT_ID, SEEDED_USER_ID, get_agent_capabilities, get_permission_snapshot
from proofgate.bindings.legal.authorize import (
    LegalAuthorizationAttempt,
    LegalAuthorizationStatus,
    authorize_legal_plan,
)
from proofgate.bindings.legal.plan import CANONICAL_PLAN, CLEAN_PLAN, LegalPlan, PlanStep
from proofgate.bindings.legal.rules import (
    LEGAL_RULESET,
    RULE_MATTER_BOUNDARY,
    RULE_PRIVILEGE_BOUNDARY,
    RULE_RECIPIENT_BOUNDARY,
)
from proofgate.policy import RULE_UNKNOWN_IMPACT, RULE_WORKFLOW_BUDGET

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
EXPIRES = T0 + datetime.timedelta(hours=8)
DURING = T0 + datetime.timedelta(hours=1)
SNAPSHOT = get_permission_snapshot(SEEDED_USER_ID, captured_at=T0)
CAPABILITIES = get_agent_capabilities(SEEDED_AGENT_ID)
UNRESOLVABLE_PLAN = LegalPlan(steps=(PlanStep(step_id="s", tool="send_email", arguments={"to": "x@example.com"}),))


def _authorize(grant, plan, **kwargs):
    """The seeded permission snapshot and agent capabilities, which permit
    every L3 scenario here, so these tests keep exercising L3 behavior."""
    return authorize_legal_plan(
        grant, plan, permission_snapshot=SNAPSHOT, agent_capabilities=CAPABILITIES, **kwargs
    )


def _grant() -> AuthorityGrant:
    return AuthorityGrant(
        grant_id="grant_001",
        workflow_id="wf_123",
        matter_id="M-102",
        max_documents=100,
        privilege_policy="exclude",
        allowed_recipients=("counsel@example.com",),
        granted_by="partner-01",
        granted_at=T0,
        expires_at=EXPIRES,
    )


@pytest.fixture
def spies():
    """Wrap the real resolver and rule evaluation so calls are observable."""
    with mock.patch.object(
        authorize_module, "resolve_plan", wraps=authorize_module.resolve_plan
    ) as resolve_spy, mock.patch.object(
        authorize_module, "evaluate_legal_rules", wraps=authorize_module.evaluate_legal_rules
    ) as rules_spy:
        yield resolve_spy, rules_spy


def _statuses(attempt: LegalAuthorizationAttempt) -> dict[str, str]:
    return {outcome.rule_id: "FAIL" if outcome.triggered else "PASS" for outcome in attempt.decision.outcomes}


# ---------------------------------------------------------------------------
# A/B. Valid grant: resolve, then the four rules
# ---------------------------------------------------------------------------


def test_valid_grant_canonical_plan_blocks_on_three_rules(spies):
    resolve_spy, rules_spy = spies
    attempt = _authorize(_grant(), CANONICAL_PLAN, now=DURING)

    assert attempt.status is LegalAuthorizationStatus.EVALUATED
    assert attempt.verdict == "BLOCK"
    assert _statuses(attempt) == {
        RULE_MATTER_BOUNDARY: "FAIL",
        RULE_PRIVILEGE_BOUNDARY: "FAIL",
        RULE_WORKFLOW_BUDGET: "FAIL",
        RULE_RECIPIENT_BOUNDARY: "PASS",
    }
    assert attempt.effect.total_affected == 143
    assert attempt.reason is None
    resolve_spy.assert_called_once()
    rules_spy.assert_called_once()


def test_valid_grant_clean_plan_allows(spies):
    resolve_spy, rules_spy = spies
    attempt = _authorize(_grant(), CLEAN_PLAN, now=DURING)

    assert attempt.status is LegalAuthorizationStatus.EVALUATED
    assert attempt.verdict == "ALLOW"
    assert set(_statuses(attempt).values()) == {"PASS"}
    assert attempt.effect.total_affected == 80
    resolve_spy.assert_called_once()
    rules_spy.assert_called_once()


def test_committed_ids_reach_the_budget_rule():
    committed = tuple(f"D-102-{number:03d}" for number in range(501, 522))  # 21 disjoint + 80 = 101
    attempt = _authorize(_grant(), CLEAN_PLAN, now=DURING, committed_resource_ids=committed)

    assert attempt.verdict == "BLOCK"
    assert [rule.rule_id for rule in attempt.decision.triggered_rules] == [RULE_WORKFLOW_BUDGET]


# ---------------------------------------------------------------------------
# C/D/E. Grant precondition
# ---------------------------------------------------------------------------


def test_missing_grant_fails_closed_before_resolution(spies):
    resolve_spy, rules_spy = spies
    attempt = _authorize(None, CLEAN_PLAN, now=DURING)

    assert attempt.status is LegalAuthorizationStatus.GRANT_MISSING
    assert attempt.reason == "grant_missing"
    assert attempt.verdict == "BLOCK"
    assert attempt.decision is None and attempt.effect is None
    resolve_spy.assert_not_called()
    rules_spy.assert_not_called()


def test_expired_grant_fails_closed_before_resolution(spies):
    resolve_spy, rules_spy = spies
    attempt = _authorize(_grant(), CLEAN_PLAN, now=EXPIRES + datetime.timedelta(minutes=1))

    assert attempt.status is LegalAuthorizationStatus.GRANT_EXPIRED
    assert attempt.reason == "grant_expired"
    assert attempt.verdict == "BLOCK"
    assert attempt.decision is None and attempt.effect is None
    resolve_spy.assert_not_called()
    rules_spy.assert_not_called()


def test_future_dated_grant_fails_closed_before_resolution(spies):
    resolve_spy, rules_spy = spies
    attempt = _authorize(_grant(), CLEAN_PLAN, now=T0 - datetime.timedelta(minutes=1))

    assert attempt.status is LegalAuthorizationStatus.GRANT_NOT_YET_VALID
    assert attempt.reason == "grant_not_yet_valid"
    assert attempt.verdict == "BLOCK"
    assert attempt.decision is None and attempt.effect is None
    resolve_spy.assert_not_called()
    rules_spy.assert_not_called()


@pytest.mark.parametrize(
    "now,status",
    [
        (T0 - datetime.timedelta(microseconds=1), LegalAuthorizationStatus.GRANT_NOT_YET_VALID),
        (T0, LegalAuthorizationStatus.EVALUATED),
        (DURING, LegalAuthorizationStatus.EVALUATED),
        (EXPIRES - datetime.timedelta(microseconds=1), LegalAuthorizationStatus.EVALUATED),
        (EXPIRES, LegalAuthorizationStatus.GRANT_EXPIRED),
    ],
    ids=["before_granted_at", "at_granted_at", "inside", "just_before_expiry", "at_expires_at"],
)
def test_authority_is_in_effect_exactly_during_granted_at_to_expires_at(now, status):
    attempt = _authorize(_grant(), CLEAN_PLAN, now=now)

    assert attempt.status is status
    assert attempt.verdict == ("ALLOW" if status is LegalAuthorizationStatus.EVALUATED else "BLOCK")


def test_expired_grant_blocks_even_a_plan_whose_rules_would_all_pass():
    assert _authorize(_grant(), CLEAN_PLAN, now=DURING).verdict == "ALLOW"
    assert _authorize(_grant(), CLEAN_PLAN, now=EXPIRES).verdict == "BLOCK"


def test_expiry_boundary():
    assert _authorize(_grant(), CLEAN_PLAN, now=EXPIRES).status is LegalAuthorizationStatus.GRANT_EXPIRED
    just_before = EXPIRES - datetime.timedelta(microseconds=1)
    assert _authorize(_grant(), CLEAN_PLAN, now=just_before).status is LegalAuthorizationStatus.EVALUATED


def test_grant_failures_never_use_unknown_impact():
    for attempt in (
        _authorize(None, CLEAN_PLAN, now=DURING),
        _authorize(_grant(), CLEAN_PLAN, now=T0 - datetime.timedelta(seconds=1)),
        _authorize(_grant(), CLEAN_PLAN, now=EXPIRES),
    ):
        assert attempt.decision is None  # no PolicyDecision, so no RULE_UNKNOWN_IMPACT


# ---------------------------------------------------------------------------
# F. No hidden clock
# ---------------------------------------------------------------------------


def test_now_is_a_required_keyword_with_no_default():
    parameter = inspect.signature(authorize_legal_plan).parameters["now"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        authorize_legal_plan(_grant(), CLEAN_PLAN)


def test_authorization_module_never_reads_a_clock():
    tree = ast.parse(Path(authorize_module.__file__).read_text())
    clock_calls = {"now", "utcnow", "today", "time", "monotonic", "perf_counter"}
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    imported = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }

    assert not called & clock_calls
    assert "time" not in imported


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        _authorize(_grant(), CLEAN_PLAN, now=datetime.datetime(2026, 10, 9, 10, 0))


# ---------------------------------------------------------------------------
# G. Resolution failure
# ---------------------------------------------------------------------------


def test_resolution_failure_fails_closed_with_unknown_impact(spies):
    resolve_spy, rules_spy = spies
    attempt = _authorize(_grant(), UNRESOLVABLE_PLAN, now=DURING)

    assert attempt.status is LegalAuthorizationStatus.RESOLUTION_FAILED
    assert attempt.verdict == "BLOCK"
    assert attempt.effect is None
    assert attempt.reason == "Step 's': unknown tool 'send_email'."
    assert [(rule.rule_id, rule.explanation) for rule in attempt.decision.triggered_rules] == [
        (RULE_UNKNOWN_IMPACT, "The plan could not be resolved to a known effect: Step 's': unknown tool 'send_email'.")
    ]
    assert attempt.decision.missing_requirements == ["a plan that resolves to a known effect"]
    resolve_spy.assert_called_once()
    rules_spy.assert_not_called()


def test_neither_precondition_is_a_legal_rule():
    rule_ids = [rule.rule_id for rule in LEGAL_RULESET.rules]

    assert rule_ids == [RULE_MATTER_BOUNDARY, RULE_PRIVILEGE_BOUNDARY, RULE_WORKFLOW_BUDGET, RULE_RECIPIENT_BOUNDARY]
    assert RULE_UNKNOWN_IMPACT not in rule_ids


# ---------------------------------------------------------------------------
# H. The outcomes stay distinguishable
# ---------------------------------------------------------------------------


def test_the_six_outcomes_are_observably_different():
    attempts = {
        "missing": _authorize(None, CANONICAL_PLAN, now=DURING),
        "not_yet_valid": _authorize(_grant(), CANONICAL_PLAN, now=T0 - datetime.timedelta(seconds=1)),
        "expired": _authorize(_grant(), CANONICAL_PLAN, now=EXPIRES),
        "unresolvable": _authorize(_grant(), UNRESOLVABLE_PLAN, now=DURING),
        "known_block": _authorize(_grant(), CANONICAL_PLAN, now=DURING),
        "allow": _authorize(_grant(), CLEAN_PLAN, now=DURING),
    }

    def signature(attempt):
        triggered = tuple(rule.rule_id for rule in attempt.decision.triggered_rules) if attempt.decision else None
        return (attempt.status, attempt.verdict, triggered)

    signatures = {name: signature(attempt) for name, attempt in attempts.items()}
    assert signatures == {
        "missing": (LegalAuthorizationStatus.GRANT_MISSING, "BLOCK", None),
        "not_yet_valid": (LegalAuthorizationStatus.GRANT_NOT_YET_VALID, "BLOCK", None),
        "expired": (LegalAuthorizationStatus.GRANT_EXPIRED, "BLOCK", None),
        "unresolvable": (LegalAuthorizationStatus.RESOLUTION_FAILED, "BLOCK", (RULE_UNKNOWN_IMPACT,)),
        "known_block": (
            LegalAuthorizationStatus.EVALUATED,
            "BLOCK",
            (RULE_MATTER_BOUNDARY, RULE_PRIVILEGE_BOUNDARY, RULE_WORKFLOW_BUDGET),
        ),
        "allow": (LegalAuthorizationStatus.EVALUATED, "ALLOW", ()),
    }
    assert len(set(signatures.values())) == 6


# ---------------------------------------------------------------------------
# Determinism and immutability
# ---------------------------------------------------------------------------


def test_attempts_are_deterministic():
    for plan in (CANONICAL_PLAN, CLEAN_PLAN, UNRESOLVABLE_PLAN):
        assert _authorize(_grant(), plan, now=DURING) == _authorize(_grant(), plan, now=DURING)


def test_attempt_is_immutable():
    attempt = _authorize(_grant(), CANONICAL_PLAN, now=DURING)

    with pytest.raises(AttributeError):
        attempt.status = LegalAuthorizationStatus.EVALUATED
    with pytest.raises(AttributeError):
        attempt.decision = None
