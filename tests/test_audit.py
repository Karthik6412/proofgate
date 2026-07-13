"""Slice 10 tests: append-only JSONL audit persistence.

The project-wide autouse fixture in tests/conftest.py already redirects
proofgate.audit.DEFAULT_AUDIT_PATH to a per-test tmp_path, so
guarded_delete_users's automatic audit writes never touch the real
artifacts/audit.jsonl. Tests that exercise append_audit_event/
reset_audit_log directly pass an explicit tmp_path anyway.
"""

import hashlib
import json
import uuid
from pathlib import Path
from unittest import mock

import pytest

import operations.actions as operations_actions
import proofgate.audit as audit_module
from operations.actions import count_rows
from operations.database import PRISTINE_DB_PATH, WORKING_DB_PATH, reset_working_db
from operations.snapshots import create_snapshot
from proofgate.audit import append_audit_event, current_timestamp, new_event_id, reset_audit_log
from proofgate.budgets import get_workflow_budget, reset_workflow_state
from proofgate.core import guarded_delete_users
from proofgate.models import (
    ActionContext,
    AuditEvent,
    IntentConstraints,
    WorkflowBudget,
)
from proofgate.policy import (
    RULE_INTENT_BOUNDARY,
    RULE_RECOVERY_PROOF,
    RULE_UNKNOWN_IMPACT,
    RULE_WORKFLOW_BUDGET,
)

BROAD_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


@pytest.fixture(autouse=True)
def _reset_real_working_db():
    reset_working_db()
    yield
    reset_working_db()


def _fresh_workflow_id() -> str:
    return f"wf-{uuid.uuid4().hex[:12]}"


def _action_context(workflow_id: str) -> ActionContext:
    return ActionContext(
        workflow_id=workflow_id,
        requesting_user="demo-user",
        agent_id="demo-agent",
        original_instruction=BROAD_INSTRUCTION,
    )


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    return [json.loads(line) for line in lines if line]


def _current_audit_path() -> Path:
    return audit_module.DEFAULT_AUDIT_PATH


def _minimal_intent() -> IntentConstraints:
    return IntentConstraints(
        action_type="delete_users",
        target_resource="users",
        environment="test",
        inactivity_days=90,
        confidence=0.98,
    )


def _minimal_event(event_id: str = "evt-test") -> AuditEvent:
    budget = WorkflowBudget(workflow_id="wf-x")
    return AuditEvent(
        schema_version="v1",
        event_id=event_id,
        workflow_id="wf-x",
        timestamp=current_timestamp(),
        original_instruction=BROAD_INSTRUCTION,
        requesting_user="u",
        agent_id="a",
        tool_name="delete_users",
        tool_arguments={"inactive_days": 90, "environment": "test"},
        intent_constraints=_minimal_intent(),
        impact_envelope=None,
        risk_features=None,
        risk_score=0.0,
        risk_factors=[],
        triggered_rules=[],
        policy_version="v1",
        extraction_mode="deterministic_fallback",
        nebius_model=None,
        craft_evidence=None,
        verdict="BLOCK",
        missing_requirements=[],
        suggested_repairs=[],
        proof_status="MISSING",
        proof_checks=None,
        execution_status="NOT_EXECUTED",
        mutation_result=None,
        postcondition_result=None,
        workflow_budget_before=budget,
        workflow_budget_after=budget,
    )


# ---------------------------------------------------------------------------
# AuditEvent contract + server-side event id generation
# ---------------------------------------------------------------------------


def test_audit_event_contains_required_contract_fields():
    expected_fields = {
        "schema_version", "event_id", "workflow_id", "timestamp",
        "original_instruction", "requesting_user", "agent_id", "tool_name",
        "tool_arguments", "intent_constraints", "impact_envelope",
        "risk_features", "risk_score", "risk_factors", "triggered_rules",
        "policy_version", "extraction_mode", "nebius_model", "craft_evidence",
        "verdict", "missing_requirements", "suggested_repairs", "proof_status",
        "proof_checks", "execution_status", "mutation_result",
        "postcondition_result", "workflow_budget_before", "workflow_budget_after",
    }
    assert set(AuditEvent.model_fields) == expected_fields


def test_event_ids_are_generated_server_side_and_differ():
    id_a = new_event_id()
    id_b = new_event_id()
    assert id_a and id_b
    assert id_a != id_b


# ---------------------------------------------------------------------------
# JSONL writer (unit level, explicit tmp paths)
# ---------------------------------------------------------------------------


def test_writes_append_rather_than_overwrite(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    append_audit_event(_minimal_event("evt-1"), audit_path=audit_path)
    append_audit_event(_minimal_event("evt-2"), audit_path=audit_path)

    events = _read_events(audit_path)
    assert len(events) == 2
    assert events[0]["event_id"] == "evt-1"
    assert events[1]["event_id"] == "evt-2"


def test_every_written_line_parses_as_valid_json(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    for i in range(3):
        append_audit_event(_minimal_event(f"evt-{i}"), audit_path=audit_path)

    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        json.loads(line)


def test_append_creates_parent_directory_if_missing(tmp_path):
    audit_path = tmp_path / "nested" / "dir" / "audit.jsonl"
    append_audit_event(_minimal_event(), audit_path=audit_path)
    assert audit_path.exists()


def test_reset_audit_log_clears_only_the_audit_file(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    append_audit_event(_minimal_event(), audit_path=audit_path)
    assert audit_path.exists()

    reset_audit_log(audit_path=audit_path)

    assert not audit_path.exists()


def test_reset_audit_log_does_not_reset_database_or_workflow_state(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    workflow_id = _fresh_workflow_id()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )
    rows_before = count_rows(WORKING_DB_PATH)
    budget_before = get_workflow_budget(workflow_id).rows_mutated

    append_audit_event(_minimal_event("evt-extra"), audit_path=audit_path)
    reset_audit_log(audit_path=audit_path)

    assert count_rows(WORKING_DB_PATH) == rows_before
    assert get_workflow_budget(workflow_id).rows_mutated == budget_before


# ---------------------------------------------------------------------------
# guarded_delete_users automatic audit integration
# ---------------------------------------------------------------------------


def test_broad_block_creates_exactly_one_event_with_expected_fields():
    workflow_id = _fresh_workflow_id()

    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    events = _read_events(_current_audit_path())
    assert len(events) == 1
    event = events[0]

    assert event["event_id"] == result.audit_event_id
    assert event["verdict"] == "BLOCK"

    triggered_ids = {r["rule_id"] for r in event["triggered_rules"]}
    assert triggered_ids == {RULE_INTENT_BOUNDARY, RULE_RECOVERY_PROOF, RULE_WORKFLOW_BUDGET}
    assert RULE_UNKNOWN_IMPACT not in triggered_ids

    expected_risk_factors = {
        "mass scope",
        "production impact",
        "intent mismatch",
        "irreversible action",
        "missing proof",
    }
    assert expected_risk_factors.issubset(set(event["risk_factors"]))

    assert event["proof_status"] == "MISSING"
    assert event["proof_checks"] is None
    assert event["execution_status"] == "NOT_EXECUTED"
    assert event["mutation_result"] is None
    assert event["postcondition_result"] is None
    assert event["workflow_budget_before"]["rows_mutated"] == 0
    assert event["workflow_budget_after"]["rows_mutated"] == 0


def test_corrected_allow_creates_exactly_one_event_with_expected_fields():
    workflow_id = _fresh_workflow_id()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )

    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )

    events = _read_events(_current_audit_path())
    assert len(events) == 1
    event = events[0]

    assert event["event_id"] == result.audit_event_id
    assert event["verdict"] == "ALLOW"
    assert event["triggered_rules"] == []
    assert event["proof_status"] == "VALID"
    assert event["proof_checks"] == {
        "snapshot_exists": True,
        "resource_matches": True,
        "selector_hash_matches": True,
        "count_within_approved_maximum": True,
    }
    assert event["execution_status"] == "EXECUTED"
    assert event["mutation_result"] == {
        "affected_count": 92,
        "production_affected": 0,
        "test_affected": 92,
    }
    assert event["postcondition_result"] == {
        "predicted_count": 92,
        "actual_count": 92,
        "production_affected": 0,
        "status": "VERIFIED",
    }
    assert event["workflow_budget_before"]["rows_mutated"] == 0
    assert event["workflow_budget_after"]["rows_mutated"] == 92


def test_enforcement_result_audit_event_id_matches_persisted_event():
    workflow_id = _fresh_workflow_id()
    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )
    events = _read_events(_current_audit_path())
    assert events[0]["event_id"] == result.audit_event_id


def test_separate_invocations_receive_different_event_ids():
    workflow_id = _fresh_workflow_id()
    result_1 = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )
    result_2 = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )
    assert result_1.audit_event_id != result_2.audit_event_id

    events = _read_events(_current_audit_path())
    assert len(events) == 2
    assert events[0]["event_id"] != events[1]["event_id"]


def test_complete_block_repair_allow_flow_creates_both_events():
    workflow_id = _fresh_workflow_id()

    blocked = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    allowed = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )

    events = _read_events(_current_audit_path())
    assert len(events) == 2
    assert events[0]["event_id"] == blocked.audit_event_id
    assert events[0]["verdict"] == "BLOCK"
    assert events[1]["event_id"] == allowed.audit_event_id
    assert events[1]["verdict"] == "ALLOW"


# ---------------------------------------------------------------------------
# No secrets or file paths leaked
# ---------------------------------------------------------------------------


def test_snapshot_file_paths_absent_from_serialized_audit_output():
    workflow_id = _fresh_workflow_id()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )

    raw_text = _current_audit_path().read_text()
    assert "snapshot_path" not in raw_text
    assert "operations/snapshots" not in raw_text
    assert ".db" not in raw_text


def test_likely_secret_strings_absent_from_serialized_audit_output():
    workflow_id = _fresh_workflow_id()
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    raw_text = _current_audit_path().read_text().lower()
    for suspicious in ("api_key", "apikey", "secret", "token", "bearer ", "sk-"):
        assert suspicious not in raw_text


# ---------------------------------------------------------------------------
# Reset separation, execution boundary, pristine safety
# ---------------------------------------------------------------------------


def test_reset_working_db_does_not_clear_audit_events():
    workflow_id = _fresh_workflow_id()
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )
    assert len(_read_events(_current_audit_path())) == 1

    reset_working_db()

    assert len(_read_events(_current_audit_path())) == 1


def test_reset_workflow_state_does_not_clear_audit_events():
    workflow_id = _fresh_workflow_id()
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )
    assert len(_read_events(_current_audit_path())) == 1

    reset_workflow_state(workflow_id)

    assert len(_read_events(_current_audit_path())) == 1


def test_operations_delete_users_called_exactly_once_and_only_after_allow():
    workflow_id = _fresh_workflow_id()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )

    with mock.patch(
        "operations.actions.delete_users", wraps=operations_actions.delete_users
    ) as spy:
        blocked = guarded_delete_users(
            action_context=_action_context(workflow_id),
            inactive_days=90,
            environment="test",
            rollback_proof=None,
        )
        assert blocked.verdict == "BLOCK"
        spy.assert_not_called()

        allowed = guarded_delete_users(
            action_context=_action_context(workflow_id),
            inactive_days=90,
            environment="test",
            rollback_proof=proof,
        )
        assert allowed.verdict == "ALLOW"
        spy.assert_called_once_with(inactive_days=90, environment="test")


def test_pristine_db_unchanged_through_full_audit_flow():
    before_hash = hashlib.sha256(PRISTINE_DB_PATH.read_bytes()).hexdigest()

    workflow_id = _fresh_workflow_id()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )

    after_hash = hashlib.sha256(PRISTINE_DB_PATH.read_bytes()).hexdigest()
    assert before_hash == after_hash
