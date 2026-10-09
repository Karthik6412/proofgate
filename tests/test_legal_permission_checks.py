"""Friday v1 beat 4: coarse permission checks and the restricted-document veto.

Permission is not authorization. The four coarse checks (user matter
access, agent may search, agent may package, recipient permitted) run on
the resolved effect against the trusted PermissionSnapshot and the seeded
AgentCapabilities, before the four Legal rules; any failure fails closed.
Restricted consequential documents veto ALLOW without pre-empting rule
evaluation. Nothing is re-fetched or re-resolved.
"""

import ast
import datetime
from pathlib import Path
from unittest import mock

import pytest
from pydantic import ValidationError

import proofgate.bindings.legal.authorize as authorize_module
import proofgate.bindings.legal.permission_checks as checks_module
from legal.models import AgentCapabilities, AuthorityGrant
from legal.permissions import (
    SEEDED_AGENT_ID,
    SEEDED_USER_ID,
    get_agent_capabilities,
    get_permission_snapshot,
)
from proofgate.bindings.legal.authorization import authorize_and_bind
from proofgate.bindings.legal.authorize import LegalAuthorizationStatus, authorize_legal_plan
from proofgate.bindings.legal.decision_record import authorize_and_record
from proofgate.bindings.legal.permission_checks import check_permissions
from proofgate.bindings.legal.plan import CANONICAL_PLAN, CLEAN_PLAN, LegalPlan, PlanStep
from proofgate.bindings.legal.resolver import resolve_plan
from proofgate.bindings.legal.rules import LEGAL_RULESET, RULE_MATTER_BOUNDARY, RULE_PRIVILEGE_BOUNDARY, RULE_RECIPIENT_BOUNDARY
from proofgate.policy import RULE_WORKFLOW_BUDGET

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
NOW = T0 + datetime.timedelta(hours=1)
CHECK_ORDER = ["user_matter_access", "agent_may_search", "agent_may_package", "recipient_permitted"]
CLEAN_DIGEST = "cd4ff5e2544c75e2ee62a0323384f28c83fc603158287654c5e596637b050b47"


def _plan(*steps) -> LegalPlan:
    return LegalPlan(steps=tuple(PlanStep(step_id=s, tool=t, arguments=a) for s, t, a in steps))


def _share_plan(query: str, recipient: str = "counsel@example.com", extra_reads=()) -> LegalPlan:
    return _plan(
        *extra_reads,
        ("step1", "search_documents", {"matter": "M-102", "query": query}),
        ("step2", "compile_package", {"documents": "$step1.result", "include_related": False}),
        ("step3", "share_package", {"package": "$step2.result", "recipient": recipient}),
    )


# The exact class of case the inspection found: one restricted, non-privileged
# M-102 document, inside the grant's matter, budget and recipient.
RESTRICTED_ONLY_PLAN = _share_plan("D-102-119")


def _grant(**overrides) -> AuthorityGrant:
    fields = dict(
        grant_id="grant_001",
        workflow_id="wf_123",
        matter_id="M-102",
        max_documents=100,
        privilege_policy="exclude",
        allowed_recipients=("counsel@example.com",),
        granted_by="partner-01",
        granted_at=T0,
        expires_at=T0 + datetime.timedelta(hours=8),
    )
    fields.update(overrides)
    return AuthorityGrant(**fields)


def _snapshot(**overrides):
    snapshot = get_permission_snapshot(SEEDED_USER_ID, captured_at=T0)
    return snapshot.model_copy(update=overrides) if overrides else snapshot


def _capabilities(tools=None):
    if tools is None:
        return get_agent_capabilities(SEEDED_AGENT_ID)
    return AgentCapabilities(agent_id=SEEDED_AGENT_ID, allowed_tools=frozenset(tools))


def _authorize(plan, snapshot="seeded", capabilities="seeded", grant=None):
    return authorize_legal_plan(
        grant or _grant(),
        plan,
        now=NOW,
        permission_snapshot=_snapshot() if snapshot == "seeded" else snapshot,
        agent_capabilities=_capabilities() if capabilities == "seeded" else capabilities,
    )


def _status(attempt) -> dict[str, str]:
    return {check.check_id: check.status for check in attempt.permissions.checks}


@pytest.fixture
def rules_spy():
    with mock.patch.object(
        authorize_module, "evaluate_legal_rules", wraps=authorize_module.evaluate_legal_rules
    ) as spy:
        yield spy


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_canonical_flow_has_four_passing_checks_in_spec_order():
    attempt = _authorize(CANONICAL_PLAN)

    assert [check.check_id for check in attempt.permissions.checks] == CHECK_ORDER
    assert _status(attempt) == dict.fromkeys(CHECK_ORDER, "PASS")
    assert attempt.status is LegalAuthorizationStatus.EVALUATED
    assert attempt.verdict == "BLOCK"  # the rules still decide the canonical BLOCK


def test_canonical_check_evidence():
    checks = {check.check_id: check for check in _authorize(CANONICAL_PLAN).permissions.checks}

    assert checks["user_matter_access"].required == ("M-102", "M-118")
    assert checks["user_matter_access"].permitted == ("M-102", "M-118")
    assert checks["agent_may_search"].required == ("search_documents",)
    assert checks["agent_may_package"].required == ("compile_package",)
    assert checks["recipient_permitted"].required == ("counsel@example.com",)
    assert all(check.denied == () for check in checks.values())


def test_clean_flow_passes_every_check_and_allows():
    attempt, record = authorize_and_bind(
        _grant(), CLEAN_PLAN, now=NOW, permission_snapshot=_snapshot(), agent_capabilities=_capabilities()
    )

    assert _status(attempt) == dict.fromkeys(CHECK_ORDER, "PASS")
    assert attempt.verdict == "ALLOW"
    assert record.action_digest == CLEAN_DIGEST  # L4 digest unchanged


# ---------------------------------------------------------------------------
# Each coarse check fails closed before the rules
# ---------------------------------------------------------------------------


def test_resolved_matter_outside_permitted_matters_fails(rules_spy):
    attempt = _authorize(CANONICAL_PLAN, snapshot=_snapshot(permitted_matters=frozenset({"M-102"})))

    assert attempt.status is LegalAuthorizationStatus.PERMISSION_DENIED
    assert attempt.reason == "permission_check_failed"
    assert _status(attempt)["user_matter_access"] == "FAIL"
    assert attempt.permissions.checks[0].denied == ("M-118",)
    assert attempt.decision is None and attempt.verdict == "BLOCK"
    rules_spy.assert_not_called()


def test_matter_access_covers_documents_that_were_only_read():
    plan = _share_plan(
        "diligence", extra_reads=[("lookup", "search_documents", {"matter": "M-118", "query": "agreement"})]
    )
    attempt = _authorize(plan, snapshot=_snapshot(permitted_matters=frozenset({"M-102"})))

    assert attempt.effect.consequential_resource_ids[0].startswith("D-102-")
    assert _status(attempt)["user_matter_access"] == "FAIL"
    assert attempt.status is LegalAuthorizationStatus.PERMISSION_DENIED


def test_missing_search_capability_fails(rules_spy):
    attempt = _authorize(CLEAN_PLAN, capabilities=_capabilities({"compile_package", "share_package"}))

    assert _status(attempt) == {
        "user_matter_access": "PASS",
        "agent_may_search": "FAIL",
        "agent_may_package": "PASS",
        "recipient_permitted": "PASS",
    }
    assert attempt.permissions.checks[1].denied == ("search_documents",)
    assert attempt.status is LegalAuthorizationStatus.PERMISSION_DENIED
    rules_spy.assert_not_called()


def test_missing_package_capability_fails(rules_spy):
    attempt = _authorize(CLEAN_PLAN, capabilities=_capabilities({"search_documents", "share_package"}))

    assert _status(attempt)["agent_may_package"] == "FAIL"
    assert attempt.permissions.checks[2].denied == ("compile_package",)
    assert attempt.status is LegalAuthorizationStatus.PERMISSION_DENIED
    rules_spy.assert_not_called()


def test_capability_checks_follow_the_plans_actual_tools():
    search_only = _plan(("step1", "search_documents", {"matter": "M-102", "query": "diligence"}))
    checks = {
        check.check_id: check
        for check in _authorize(search_only, capabilities=_capabilities({"search_documents"})).permissions.checks
    }

    assert checks["agent_may_search"].required == ("search_documents",)
    assert checks["agent_may_package"].required == ()
    assert checks["agent_may_package"].status == "PASS"


def test_unpermitted_recipient_fails(rules_spy):
    attempt = _authorize(_share_plan("diligence", recipient="outside@example.com"))

    assert _status(attempt)["recipient_permitted"] == "FAIL"
    assert attempt.permissions.checks[3].denied == ("outside@example.com",)
    assert attempt.status is LegalAuthorizationStatus.PERMISSION_DENIED
    rules_spy.assert_not_called()


def test_permission_recipient_failure_is_distinct_from_the_grant_recipient_rule():
    permitted_but_not_granted = _authorize(
        _share_plan("diligence", recipient="outside@example.com"),
        snapshot=_snapshot(permitted_recipients=frozenset({"counsel@example.com", "outside@example.com"})),
    )
    granted_but_not_permitted = _authorize(
        _share_plan("diligence", recipient="outside@example.com"),
        grant=_grant(allowed_recipients=("counsel@example.com", "outside@example.com")),
    )

    assert permitted_but_not_granted.status is LegalAuthorizationStatus.EVALUATED
    assert _status(permitted_but_not_granted)["recipient_permitted"] == "PASS"
    assert [rule.rule_id for rule in permitted_but_not_granted.decision.triggered_rules] == [RULE_RECIPIENT_BOUNDARY]

    assert granted_but_not_permitted.status is LegalAuthorizationStatus.PERMISSION_DENIED
    assert _status(granted_but_not_permitted)["recipient_permitted"] == "FAIL"
    assert granted_but_not_permitted.decision is None


# ---------------------------------------------------------------------------
# Restricted-document veto
# ---------------------------------------------------------------------------


def test_restricted_non_privileged_in_scope_document_can_no_longer_be_allowed():
    attempt, record = authorize_and_bind(
        _grant(), RESTRICTED_ONLY_PLAN, now=NOW, permission_snapshot=_snapshot(), agent_capabilities=_capabilities()
    )
    (resource,) = attempt.effect.consequential_resources

    assert (resource.doc_id, resource.matter_id, resource.privileged, resource.restricted) == (
        "D-102-119",
        "M-102",
        False,
        True,
    )
    assert _status(attempt) == dict.fromkeys(CHECK_ORDER, "PASS")
    assert attempt.decision.verdict == "ALLOW"  # every grant boundary passes...
    assert attempt.permissions.restricted_resource_ids == ("D-102-119",)
    assert attempt.verdict == "BLOCK"  # ...but the restriction vetoes ALLOW
    assert record is None


def test_restricted_veto_does_not_preempt_the_canonical_rule_verdict():
    attempt = _authorize(CANONICAL_PLAN)

    assert attempt.status is LegalAuthorizationStatus.EVALUATED
    assert attempt.permissions.restricted_resource_ids == ("D-102-119", "D-102-120", "D-118-013", "D-118-014")
    assert [rule.rule_id for rule in attempt.decision.triggered_rules] == [
        RULE_MATTER_BOUNDARY,
        RULE_PRIVILEGE_BOUNDARY,
        RULE_WORKFLOW_BUDGET,
    ]


def test_restricted_veto_comes_from_the_permission_snapshot():
    attempt = _authorize(RESTRICTED_ONLY_PLAN, snapshot=_snapshot(restricted_document_ids=frozenset()))

    assert attempt.permissions.restricted_resource_ids == ()
    assert attempt.verdict == "ALLOW"


def test_reading_restricted_metadata_is_not_acting_on_it():
    plan = _share_plan(
        "diligence", extra_reads=[("peek", "search_documents", {"matter": "M-102", "query": "D-102-119"})]
    )
    attempt = _authorize(plan)

    assert "D-102-119" in attempt.effect.all_resolved_resource_ids
    assert attempt.permissions.restricted_resource_ids == ()
    assert attempt.verdict == "ALLOW"


def test_restricted_veto_is_recorded_as_a_block_with_its_restricted_count(tmp_path):
    attempt, authorization, record = authorize_and_record(
        _grant(),
        RESTRICTED_ONLY_PLAN,
        now=NOW,
        permission_snapshot=_snapshot(),
        agent_capabilities=_capabilities(),
        decision_log_path=tmp_path / "log.jsonl",
    )

    assert authorization is None
    assert record.decision == "BLOCK"
    assert record.resolved.restricted_count == 1
    assert record.checks.model_dump() == dict.fromkeys(
        ["matter_boundary", "privilege_boundary", "workflow_budget", "recipient_boundary"], "PASS"
    )


# ---------------------------------------------------------------------------
# Missing permission state fails intentionally
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snapshot,capabilities,reason",
    [
        (None, "seeded", "permission_snapshot_missing"),
        ("unknown_user", "seeded", "permission_snapshot_missing"),
        ("seeded", None, "agent_capabilities_missing"),
        ("seeded", "unknown_agent", "agent_capabilities_missing"),
    ],
    ids=["snapshot_none", "unknown_user", "capabilities_none", "unknown_agent"],
)
def test_missing_permission_state_fails_closed_without_an_exception(tmp_path, snapshot, capabilities, reason):
    snapshot = get_permission_snapshot("nobody", captured_at=T0) if snapshot == "unknown_user" else snapshot
    capabilities = get_agent_capabilities("nobody") if capabilities == "unknown_agent" else capabilities
    with mock.patch.object(authorize_module, "resolve_plan", wraps=authorize_module.resolve_plan) as resolve_spy:
        attempt, authorization, record = authorize_and_record(
            _grant(),
            CLEAN_PLAN,
            now=NOW,
            permission_snapshot=_snapshot() if snapshot == "seeded" else snapshot,
            agent_capabilities=_capabilities() if capabilities == "seeded" else capabilities,
            decision_log_path=tmp_path / "log.jsonl",
        )

    assert attempt.status is LegalAuthorizationStatus.PERMISSION_DENIED
    assert attempt.reason == reason
    assert attempt.verdict == "BLOCK"
    assert attempt.decision is None and attempt.effect is None and attempt.permissions is None
    assert authorization is None and record is None
    assert not (tmp_path / "log.jsonl").exists()
    resolve_spy.assert_not_called()


def test_grant_precondition_still_comes_first():
    attempt = authorize_legal_plan(
        None, CLEAN_PLAN, now=NOW, permission_snapshot=None, agent_capabilities=None
    )

    assert attempt.status is LegalAuthorizationStatus.GRANT_MISSING


def test_unknown_tool_still_fails_through_resolution():
    attempt = _authorize(_plan(("s", "send_email", {"to": "x@example.com"})))

    assert attempt.status is LegalAuthorizationStatus.RESOLUTION_FAILED
    assert attempt.permissions is None


# ---------------------------------------------------------------------------
# Separation and purity
# ---------------------------------------------------------------------------


def test_the_four_legal_rules_remain_four():
    assert [rule.rule_id for rule in LEGAL_RULESET.rules] == [
        RULE_MATTER_BOUNDARY,
        RULE_PRIVILEGE_BOUNDARY,
        RULE_WORKFLOW_BUDGET,
        RULE_RECIPIENT_BOUNDARY,
    ]


def test_permissions_are_never_refetched_during_authorization():
    with mock.patch("legal.permissions.get_permission_snapshot") as snapshot_spy, mock.patch(
        "legal.permissions.get_agent_capabilities"
    ) as capability_spy:
        authorize_and_bind(
            _grant(), CLEAN_PLAN, now=NOW, permission_snapshot=_snapshot(), agent_capabilities=_capabilities()
        )
    snapshot_spy.assert_not_called()
    capability_spy.assert_not_called()

    for module in (authorize_module, checks_module):
        tree = ast.parse(Path(module.__file__).read_text())
        imported_from = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert "legal.permissions" not in imported_from, module.__name__


def test_check_permissions_is_pure_and_deterministic():
    effect = resolve_plan(CANONICAL_PLAN)
    first = check_permissions(_snapshot(), _capabilities(), CANONICAL_PLAN, effect)

    assert first == check_permissions(_snapshot(), _capabilities(), CANONICAL_PLAN, effect)
    with pytest.raises(ValidationError):
        first.checks[0].status = "FAIL"


def test_permission_snapshot_canonical_state_is_unchanged():
    assert set(_snapshot().canonical_state()) == {
        "user_id",
        "permitted_matters",
        "restricted_document_ids",
        "permitted_recipients",
        "source_version",
    }
