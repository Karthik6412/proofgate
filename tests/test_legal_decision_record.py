"""Friday v1: the append-only Legal decision record.

One JSON record per governed action that reached policy evaluation, shaped
like the spec's sample, with every field copied from the object that
already holds it. ALLOW records carry the AuthorizationRecord's digest;
rule-triggered BLOCK records carry the digest of the same evaluated action
and no AuthorizationRecord exists. Pre-decision failures get no record.
"""

import ast
import datetime
import json
from pathlib import Path
from unittest import mock

import pytest
from pydantic import ValidationError

import proofgate.audit as audit_module
import proofgate.bindings.legal.decision_record as decision_module
from legal.models import AuthorityGrant
from legal.permissions import SEEDED_USER_ID, get_permission_snapshot
from proofgate.bindings.legal.authorization import LegalAction, action_digest, authorize_and_bind
from proofgate.bindings.legal.authorize import LegalAuthorizationStatus
from proofgate.bindings.legal.decision_record import (
    CHECK_KEYS,
    LegalDecisionRecord,
    append_decision_record,
    authorize_and_record,
    build_decision_record,
)
from proofgate.bindings.legal.plan import CANONICAL_PLAN, CLEAN_PLAN, LegalPlan, PlanStep
from proofgate.bindings.legal.rules import RULE_MATTER_BOUNDARY, RULE_PRIVILEGE_BOUNDARY, RULE_RECIPIENT_BOUNDARY
from proofgate.policy import RULE_WORKFLOW_BUDGET

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
NOW = T0 + datetime.timedelta(hours=1)
CLEAN_DIGEST = "cd4ff5e2544c75e2ee62a0323384f28c83fc603158287654c5e596637b050b47"
OUTSIDE_RECIPIENT_PLAN = LegalPlan(
    steps=CLEAN_PLAN.steps[:2]
    + (
        PlanStep(
            step_id="step3",
            tool="share_package",
            arguments={"package": "$step2.result", "recipient": "outside@example.com"},
        ),
    )
)


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


def _snapshot():
    return get_permission_snapshot(SEEDED_USER_ID, captured_at=T0)


@pytest.fixture(autouse=True)
def _isolate_decision_log(tmp_path, monkeypatch):
    monkeypatch.setattr(decision_module, "DEFAULT_DECISION_LOG_PATH", tmp_path / "legal_decisions.jsonl")


def _record(plan, path, grant=None):
    return authorize_and_record(
        grant or _grant(), plan, now=NOW, permission_snapshot=_snapshot(), decision_log_path=path
    )


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# ALLOW
# ---------------------------------------------------------------------------


def test_allow_record_matches_the_spec_shape(tmp_path):
    attempt, authorization, record = _record(CLEAN_PLAN, tmp_path / "log.jsonl")
    payload = json.loads(_lines(tmp_path / "log.jsonl")[0])

    assert attempt.verdict == "ALLOW"
    assert list(payload) == [
        "decision_id",
        "workflow_id",
        "grant_id",
        "authorized",
        "resolved",
        "checks",
        "check_details",
        "action_digest",
        "decision",
        "executed",
        "postcondition",
    ]
    assert payload["decision_id"] == record.decision_id
    assert (payload["workflow_id"], payload["grant_id"]) == ("wf_123", "grant_001")
    assert payload["authorized"] == {
        "matter": "M-102",
        "max_documents": 100,
        "privilege_policy": "exclude",
        "recipients": ["counsel@example.com"],
    }
    assert payload["resolved"] == {
        "document_count": 80,
        "matters_touched": {"M-102": 80},
        "privileged_count": 0,
        "restricted_count": 0,
        "external_recipients": ["counsel@example.com"],
    }
    assert payload["checks"] == dict.fromkeys(
        ["matter_boundary", "privilege_boundary", "workflow_budget", "recipient_boundary"], "PASS"
    )
    assert payload["decision"] == "ALLOW"
    assert payload["executed"] is False
    assert payload["postcondition"] is None


def test_allow_record_digest_is_the_authorization_record_digest(tmp_path):
    _, authorization, record = _record(CLEAN_PLAN, tmp_path / "log.jsonl")

    assert record.action_digest == authorization.action_digest == CLEAN_DIGEST


def test_allow_record_does_not_recompute_the_digest(tmp_path):
    """On ALLOW the digest is taken from the AuthorizationRecord."""
    with mock.patch.object(decision_module, "action_digest") as digest_spy:
        _record(CLEAN_PLAN, tmp_path / "log.jsonl")
    digest_spy.assert_not_called()


# ---------------------------------------------------------------------------
# BLOCK
# ---------------------------------------------------------------------------


def test_canonical_block_is_recorded_without_an_authorization_record(tmp_path):
    attempt, authorization, record = _record(CANONICAL_PLAN, tmp_path / "log.jsonl")
    payload = json.loads(_lines(tmp_path / "log.jsonl")[0])

    assert attempt.verdict == "BLOCK"
    assert authorization is None
    assert payload["checks"] == {
        "matter_boundary": "FAIL",
        "privilege_boundary": "FAIL",
        "workflow_budget": "FAIL",
        "recipient_boundary": "PASS",
    }
    assert payload["resolved"] == {
        "document_count": 143,
        "matters_touched": {"M-102": 126, "M-118": 17},
        "privileged_count": 9,
        "restricted_count": 4,
        "external_recipients": ["counsel@example.com"],
    }
    assert payload["decision"] == "BLOCK"
    assert payload["executed"] is False
    assert payload["postcondition"] is None


def test_block_record_digest_names_the_exact_evaluated_action(tmp_path):
    attempt, _, record = _record(CANONICAL_PLAN, tmp_path / "log.jsonl")
    expected = action_digest(
        LegalAction(grant=_grant(), plan=CANONICAL_PLAN, effect=attempt.effect, permission_snapshot=_snapshot())
    )

    assert record.action_digest == expected
    assert record.action_digest != CLEAN_DIGEST


def test_single_rule_block_marks_only_that_rule_failed(tmp_path):
    attempt, authorization, record = _record(OUTSIDE_RECIPIENT_PLAN, tmp_path / "log.jsonl")
    payload = json.loads(_lines(tmp_path / "log.jsonl")[0])

    assert authorization is None
    assert payload["checks"] == {
        "matter_boundary": "PASS",
        "privilege_boundary": "PASS",
        "workflow_budget": "PASS",
        "recipient_boundary": "FAIL",
    }
    detail = payload["check_details"]["recipient_boundary"]
    assert detail["reason"] == "recipient_outside_grant"
    assert detail["unauthorized_recipients"] == ["outside@example.com"]
    assert payload["decision"] == "BLOCK"


# ---------------------------------------------------------------------------
# Rule mapping and details
# ---------------------------------------------------------------------------


def test_rule_ids_map_exactly_to_the_spec_check_keys():
    assert CHECK_KEYS == {
        RULE_MATTER_BOUNDARY: "matter_boundary",
        RULE_PRIVILEGE_BOUNDARY: "privilege_boundary",
        RULE_WORKFLOW_BUDGET: "workflow_budget",
        RULE_RECIPIENT_BOUNDARY: "recipient_boundary",
    }


def test_check_details_carry_each_outcomes_own_details(tmp_path):
    attempt, _, _ = _record(CANONICAL_PLAN, tmp_path / "log.jsonl")
    payload = json.loads(_lines(tmp_path / "log.jsonl")[0])

    for outcome in attempt.decision.outcomes:
        detail = payload["check_details"][CHECK_KEYS[outcome.rule_id]]
        assert detail["rule_id"] == outcome.rule_id
        assert detail["explanation"] == (outcome.triggered_rule.explanation if outcome.triggered else None)
        for key, value in outcome.details.items():
            assert detail[key] == (list(value) if isinstance(value, tuple) else value)


def test_build_uses_existing_objects_without_resolving_or_evaluating():
    attempt, authorization = authorize_and_bind(_grant(), CLEAN_PLAN, now=NOW, permission_snapshot=_snapshot())
    with mock.patch("proofgate.bindings.legal.resolver.resolve_plan") as resolve_spy, mock.patch(
        "proofgate.bindings.legal.rules.evaluate_legal_rules"
    ) as rules_spy:
        record = build_decision_record(_grant(), attempt, authorization.action_digest, decision_id="dec-fixed")
    resolve_spy.assert_not_called()
    rules_spy.assert_not_called()
    assert record.decision_id == "dec-fixed"


def test_decision_id_is_not_derived_from_the_digest(tmp_path):
    _, _, first = _record(CLEAN_PLAN, tmp_path / "log.jsonl")
    _, _, second = _record(CLEAN_PLAN, tmp_path / "log.jsonl")

    assert first.action_digest == second.action_digest
    assert first.decision_id != second.decision_id
    assert first.decision_id.startswith("dec-") and first.action_digest not in first.decision_id


def test_record_is_immutable_and_cannot_claim_execution(tmp_path):
    _, _, record = _record(CLEAN_PLAN, tmp_path / "log.jsonl")

    with pytest.raises(ValidationError):
        record.decision = "BLOCK"
    with pytest.raises(ValidationError):
        LegalDecisionRecord(**{**record.model_dump(), "executed": True})
    with pytest.raises(ValidationError):
        LegalDecisionRecord(**{**record.model_dump(), "postcondition": "VERIFIED"})


# ---------------------------------------------------------------------------
# Pre-decision failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grant,plan,status",
    [
        (None, CLEAN_PLAN, LegalAuthorizationStatus.GRANT_MISSING),
        ("future", CLEAN_PLAN, LegalAuthorizationStatus.GRANT_NOT_YET_VALID),
        ("expired", CLEAN_PLAN, LegalAuthorizationStatus.GRANT_EXPIRED),
        (
            "valid",
            LegalPlan(steps=(PlanStep(step_id="s", tool="send_email", arguments={"to": "x@example.com"}),)),
            LegalAuthorizationStatus.RESOLUTION_FAILED,
        ),
    ],
    ids=["missing", "not_yet_valid", "expired", "resolution_failed"],
)
def test_pre_decision_failures_write_no_record(tmp_path, grant, plan, status):
    grants = {
        None: None,
        "valid": _grant(),
        "future": _grant(granted_at=NOW + datetime.timedelta(hours=1), expires_at=NOW + datetime.timedelta(hours=9)),
        "expired": _grant(expires_at=T0 + datetime.timedelta(minutes=30)),
    }
    log = tmp_path / "log.jsonl"
    attempt, authorization, record = authorize_and_record(
        grants[grant], plan, now=NOW, permission_snapshot=_snapshot(), decision_log_path=log
    )

    assert attempt.status is status
    assert authorization is None and record is None
    assert not log.exists()


def test_build_refuses_an_attempt_that_was_not_evaluated():
    attempt, _ = authorize_and_bind(None, CLEAN_PLAN, now=NOW, permission_snapshot=_snapshot())

    with pytest.raises(ValueError, match="not evaluated"):
        build_decision_record(_grant(), attempt, "0" * 64)


# ---------------------------------------------------------------------------
# Append-only persistence
# ---------------------------------------------------------------------------


def test_second_write_appends_and_preserves_the_first_line_exactly(tmp_path):
    log = tmp_path / "log.jsonl"
    _record(CANONICAL_PLAN, log)
    first_bytes = log.read_bytes()
    assert len(_lines(log)) == 1

    _record(CLEAN_PLAN, log)

    assert log.read_bytes().startswith(first_bytes)
    lines = _lines(log)
    assert len(lines) == 2
    assert [json.loads(line)["decision"] for line in lines] == ["BLOCK", "ALLOW"]


def test_default_log_is_separate_from_the_operations_audit_log():
    _, _, record = authorize_and_record(_grant(), CLEAN_PLAN, now=NOW, permission_snapshot=_snapshot())

    default_log = decision_module.DEFAULT_DECISION_LOG_PATH
    assert json.loads(_lines(default_log)[0])["decision_id"] == record.decision_id
    assert default_log != audit_module.DEFAULT_AUDIT_PATH
    assert not audit_module.DEFAULT_AUDIT_PATH.exists()


def test_append_never_truncates_existing_content(tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text('{"pre-existing": true}\n', encoding="utf-8")
    attempt, authorization = authorize_and_bind(_grant(), CLEAN_PLAN, now=NOW, permission_snapshot=_snapshot())

    append_decision_record(build_decision_record(_grant(), attempt, authorization.action_digest), log)

    assert _lines(log)[0] == '{"pre-existing": true}'
    assert len(_lines(log)) == 2


# ---------------------------------------------------------------------------
# Layer boundary
# ---------------------------------------------------------------------------


def test_decision_record_code_has_no_continuation_scope_logic():
    """executed / postcondition exist only as the spec's fixed fields."""
    tree = ast.parse(Path(decision_module.__file__).read_text())
    identifiers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            identifiers.add(node.target.id)
    identifiers -= {"executed", "postcondition"}
    lowered = " ".join(sorted(identifiers)).lower()

    for concept in (
        "approv", "outbox", "dispatch", "execut", "postcondition", "retry", "rollback",
        "canary", "inject", "stale", "refetch", "re_resolve",
    ):
        assert concept not in lowered, concept
