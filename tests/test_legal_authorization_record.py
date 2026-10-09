"""Friday v1 Slice L4: action digest, AuthorizationRecord, tamper rejection.

An ALLOW mints a record bound to the exact action authorized; verification
recomputes the digest from the presented action and rejects any change.
Representation-only differences (argument or recipient order, equivalent
timezones, snapshot capture time, reads, issue time) do not change it.
"""

import ast
import datetime
from pathlib import Path
from unittest import mock

import pytest
from pydantic import ValidationError

import proofgate.bindings.legal.authorization as authorization_module
from legal.models import AuthorityGrant
from legal.permissions import SEEDED_USER_ID, get_permission_snapshot
from proofgate.bindings.legal.authorization import (
    AuthorizationDigestMismatch,
    AuthorizationRecord,
    LegalAction,
    action_digest,
    authorize_and_bind,
    verify_authorization,
)
from proofgate.bindings.legal.authorize import LegalAuthorizationStatus
from proofgate.bindings.legal.plan import CANONICAL_PLAN, CLEAN_PLAN, LegalPlan, PlanStep
from proofgate.bindings.legal.resolver import ResourceFacts

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
NOW = T0 + datetime.timedelta(hours=1)
PINNED_CLEAN_DIGEST = "cd4ff5e2544c75e2ee62a0323384f28c83fc603158287654c5e596637b050b47"


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


def _snapshot(captured_at=T0):
    return get_permission_snapshot(SEEDED_USER_ID, captured_at=captured_at)


def _bind(plan=CLEAN_PLAN, grant=None):
    return authorize_and_bind(grant or _grant(), plan, now=NOW, permission_snapshot=_snapshot())


def _authorized_action() -> tuple[AuthorizationRecord, LegalAction]:
    attempt, record = _bind()
    return record, LegalAction(grant=_grant(), plan=CLEAN_PLAN, effect=attempt.effect, permission_snapshot=_snapshot())


def _replace_step(plan: LegalPlan, index: int, **changes) -> LegalPlan:
    steps = list(plan.steps)
    step = steps[index]
    steps[index] = PlanStep(
        step_id=changes.get("step_id", step.step_id),
        tool=changes.get("tool", step.tool),
        arguments=changes.get("arguments", step.argument_map()),
    )
    return LegalPlan(steps=tuple(steps))


def _with_plan(action: LegalAction, plan: LegalPlan) -> LegalAction:
    return LegalAction(grant=action.grant, plan=plan, effect=action.effect, permission_snapshot=action.permission_snapshot)


def _with_resources(action: LegalAction, facts: tuple[ResourceFacts, ...]) -> LegalAction:
    effect = action.effect.model_copy(
        update={
            "consequential_resources": facts,
            "consequential_resource_ids": tuple(fact.doc_id for fact in facts),
            "total_affected": len(facts),
        }
    )
    return LegalAction(grant=action.grant, plan=action.plan, effect=effect, permission_snapshot=action.permission_snapshot)


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def test_digest_is_stable_and_pinned():
    _, action = _authorized_action()

    assert action_digest(action) == PINNED_CLEAN_DIGEST
    assert {action_digest(_authorized_action()[1]) for _ in range(5)} == {PINNED_CLEAN_DIGEST}


def test_argument_insertion_order_does_not_change_the_digest():
    _, action = _authorized_action()
    reordered = LegalPlan(
        steps=tuple(
            PlanStep(step_id=step.step_id, tool=step.tool, arguments=dict(reversed(list(step.arguments))))
            for step in CLEAN_PLAN.steps
        )
    )

    assert action_digest(_with_plan(action, reordered)) == action_digest(action)


def test_grant_recipient_order_and_equivalent_timezones_do_not_change_the_digest():
    _, action = _authorized_action()
    plus_two = datetime.timezone(datetime.timedelta(hours=2))
    equivalent = _grant(
        allowed_recipients=("counsel@example.com", "counsel@example.com"),
        granted_at=T0.astimezone(plus_two),
        expires_at=(T0 + datetime.timedelta(hours=8)).astimezone(plus_two),
    )

    assert action_digest(action.model_copy(update={"grant": equivalent})) == action_digest(action)


def test_snapshot_capture_time_does_not_change_the_digest():
    _, action = _authorized_action()
    recaptured = action.model_copy(update={"permission_snapshot": _snapshot(captured_at=T0 + datetime.timedelta(days=1))})

    assert action_digest(recaptured) == action_digest(action)


def test_documents_only_read_do_not_change_the_digest():
    _, action = _authorized_action()
    effect = action.effect.model_copy(
        update={"all_resolved_resource_ids": action.effect.all_resolved_resource_ids + ("D-118-020",)}
    )

    assert action_digest(action.model_copy(update={"effect": effect})) == action_digest(action)


@pytest.mark.parametrize(
    "index,changes",
    [
        (2, {"tool": "export_package"}),
        (0, {"arguments": {"matter": "M-118", "query": "diligence"}}),
        (0, {"arguments": {"matter": "M-102", "query": "privileged"}}),
        (1, {"arguments": {"documents": "$step1.result", "include_related": True}}),
        (2, {"arguments": {"package": "$step2.result", "recipient": "outside@example.com"}}),
    ],
    ids=["operation", "target_matter", "query_argument", "nested_include_related", "nested_recipient"],
)
def test_execution_relevant_plan_changes_change_the_digest(index, changes):
    _, action = _authorized_action()

    assert action_digest(_with_plan(action, _replace_step(CLEAN_PLAN, index, **changes))) != action_digest(action)


@pytest.mark.parametrize(
    "grant_change",
    [{"max_documents": 1000}, {"matter_id": "M-118"}, {"privilege_policy": "include"}, {"granted_by": "someone"}],
    ids=["max_documents", "matter", "privilege_policy", "grantor"],
)
def test_changed_authority_under_the_same_grant_id_changes_the_digest(grant_change):
    _, action = _authorized_action()

    assert action_digest(action.model_copy(update={"grant": _grant(**grant_change)})) != action_digest(action)


def test_changed_resource_labels_or_content_change_the_digest():
    _, action = _authorized_action()
    first, *rest = action.effect.consequential_resources

    for change in ({"privileged": True}, {"restricted": True}, {"matter_id": "M-118"}, {"content_hash": "edited"}):
        tampered = (first.model_copy(update=change), *rest)
        assert action_digest(_with_resources(action, tampered)) != action_digest(action), change


def test_changed_permission_state_changes_the_digest():
    _, action = _authorized_action()
    snapshot = action.permission_snapshot.model_copy(update={"source_version": "seed-v2"})

    assert action_digest(action.model_copy(update={"permission_snapshot": snapshot})) != action_digest(action)


# ---------------------------------------------------------------------------
# AuthorizationRecord
# ---------------------------------------------------------------------------


def test_allow_mints_a_record_bound_to_the_authorized_action():
    record, action = _authorized_action()

    assert record.action_digest == action_digest(action) == PINNED_CLEAN_DIGEST
    assert record.verdict == "ALLOW"
    assert (record.workflow_id, record.grant_id) == ("wf_123", "grant_001")
    assert record.permission_snapshot_digest == _snapshot().snapshot_digest
    assert record.evaluated_rule_ids == (
        "RULE_MATTER_BOUNDARY",
        "RULE_PRIVILEGE_BOUNDARY",
        "RULE_WORKFLOW_BUDGET",
        "RULE_RECIPIENT_BOUNDARY",
    )
    assert record.issued_at == NOW


def test_issue_time_is_not_part_of_the_digest():
    _, early = authorize_and_bind(_grant(), CLEAN_PLAN, now=NOW, permission_snapshot=_snapshot())
    _, late = authorize_and_bind(
        _grant(), CLEAN_PLAN, now=NOW + datetime.timedelta(hours=2), permission_snapshot=_snapshot()
    )

    assert early.action_digest == late.action_digest
    assert early.issued_at != late.issued_at


@pytest.mark.parametrize(
    "grant,plan,status",
    [
        ("valid", CANONICAL_PLAN, LegalAuthorizationStatus.EVALUATED),
        (None, CLEAN_PLAN, LegalAuthorizationStatus.GRANT_MISSING),
        ("expired", CLEAN_PLAN, LegalAuthorizationStatus.GRANT_EXPIRED),
        (
            "valid",
            LegalPlan(steps=(PlanStep(step_id="s", tool="send_email", arguments={"to": "x@example.com"}),)),
            LegalAuthorizationStatus.RESOLUTION_FAILED,
        ),
    ],
    ids=["canonical_block", "missing_grant", "expired_grant", "resolution_failed"],
)
def test_anything_but_allow_mints_no_record(grant, plan, status):
    grants = {"valid": _grant(), "expired": _grant(expires_at=T0 + datetime.timedelta(minutes=30)), None: None}
    attempt, record = authorize_and_bind(grants[grant], plan, now=NOW, permission_snapshot=_snapshot())

    assert attempt.status is status
    assert attempt.verdict == "BLOCK"
    assert record is None


def test_record_is_immutable():
    record, _ = _authorized_action()

    with pytest.raises(ValidationError):
        record.action_digest = "0" * 64
    with pytest.raises(ValidationError):
        AuthorizationRecord(**{**record.model_dump(), "verdict": "BLOCK"})


def test_binding_leaves_l3_authorization_unchanged():
    attempt, _ = _bind(CANONICAL_PLAN)
    statuses = {outcome.rule_id: outcome.triggered for outcome in attempt.decision.outcomes}

    assert statuses == {
        "RULE_MATTER_BOUNDARY": True,
        "RULE_PRIVILEGE_BOUNDARY": True,
        "RULE_WORKFLOW_BUDGET": True,
        "RULE_RECIPIENT_BOUNDARY": False,
    }


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def test_untouched_action_verifies():
    record, action = _authorized_action()

    assert verify_authorization(record, action) is None


def test_equivalent_reordered_action_verifies():
    record, action = _authorized_action()
    reordered = LegalPlan(
        steps=tuple(
            PlanStep(step_id=step.step_id, tool=step.tool, arguments=dict(reversed(list(step.arguments))))
            for step in CLEAN_PLAN.steps
        )
    )

    verify_authorization(record, _with_plan(action, reordered))


def _tampered(action: LegalAction, case: str) -> LegalAction:
    if case == "changed_operation":
        return _with_plan(action, _replace_step(CLEAN_PLAN, 2, tool="export_package"))
    if case == "changed_target":
        return _with_plan(action, _replace_step(CLEAN_PLAN, 0, arguments={"matter": "M-118", "query": "diligence"}))
    if case == "changed_argument":
        return _with_plan(action, _replace_step(CLEAN_PLAN, 0, arguments={"matter": "M-102", "query": "memo"}))
    if case == "added_argument":
        return _with_plan(
            action,
            _replace_step(CLEAN_PLAN, 0, arguments={"matter": "M-102", "query": "diligence", "limit": 500}),
        )
    if case == "removed_argument":
        return _with_plan(action, _replace_step(CLEAN_PLAN, 0, arguments={"matter": "M-102"}))
    if case == "nested_mutation":
        return _with_plan(
            action,
            _replace_step(CLEAN_PLAN, 2, arguments={"package": "$step2.result", "recipient": "outside@example.com"}),
        )
    if case == "added_resource":
        extra = ResourceFacts(doc_id="D-118-001", matter_id="M-118", privileged=False, restricted=False, content_hash="h")
        return _with_resources(action, action.effect.consequential_resources + (extra,))
    if case == "removed_resource":
        return _with_resources(action, action.effect.consequential_resources[:-1])
    if case == "changed_grant":
        return action.model_copy(update={"grant": _grant(max_documents=10_000)})
    if case == "changed_permissions":
        snapshot = action.permission_snapshot.model_copy(update={"permitted_matters": frozenset({"M-102", "M-131"})})
        return action.model_copy(update={"permission_snapshot": snapshot})
    raise AssertionError(case)


TAMPER_CASES = [
    "changed_operation",
    "changed_target",
    "changed_argument",
    "added_argument",
    "removed_argument",
    "nested_mutation",
    "added_resource",
    "removed_resource",
    "changed_grant",
    "changed_permissions",
]


@pytest.mark.parametrize("case", TAMPER_CASES)
def test_tampered_action_is_rejected(case):
    record, action = _authorized_action()
    tampered = _tampered(action, case)

    with pytest.raises(AuthorizationDigestMismatch) as excinfo:
        verify_authorization(record, tampered)
    assert excinfo.value.expected == record.action_digest
    assert excinfo.value.actual == action_digest(tampered) != record.action_digest


def test_verification_never_reauthorizes_or_resolves():
    record, action = _authorized_action()
    tampered = _tampered(action, "added_resource")

    with mock.patch.object(authorization_module, "authorize_legal_plan") as authorize_spy:
        verify_authorization(record, action)
        with pytest.raises(AuthorizationDigestMismatch):
            verify_authorization(record, tampered)
    authorize_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Layer boundary
# ---------------------------------------------------------------------------


def test_authorization_module_has_no_l5_concepts():
    tree = ast.parse(Path(authorization_module.__file__).read_text())
    identifiers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            identifiers.add(node.name)
    lowered = {identifier.lower() for identifier in identifiers}

    for concept in ("approval", "outbox", "dispatch", "execute", "postcondition", "rollback", "require_approval"):
        assert not any(concept in identifier for identifier in lowered), concept
