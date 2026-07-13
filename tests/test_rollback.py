"""Slice 23 tests: proofgate/rollback.py's resolve_final_postcondition, and
full integration through guarded_delete_users with the real MCP-free guarded
pipeline. Follows the same real-working-db convention as
tests/test_snapshot_and_allow.py / tests/test_postcondition_and_budget.py.
"""

import os
from pathlib import Path
from unittest import mock

import pytest

import proofgate.audit as audit_module
from operations.actions import count_rows, delete_users
from operations.database import PRISTINE_DB_PATH, WORKING_DB_PATH, reset_working_db
from operations.restoration import compute_users_digest, restore_snapshot
from operations.snapshots import create_snapshot, get_snapshot_metadata
from proofgate.audit import reset_audit_log
from proofgate.budgets import get_last_postcondition_result, get_workflow_budget, reset_workflow_state
from proofgate.core import guarded_delete_users
from proofgate.models import ActionContext, PostconditionResult, RollbackProof
from proofgate.registry import get_tool_spec
from proofgate.rollback import resolve_final_postcondition

BROAD_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


@pytest.fixture(autouse=True)
def _reset_real_working_db():
    reset_working_db()
    yield
    reset_working_db()


@pytest.fixture(autouse=True)
def _reset_mismatch_env(monkeypatch):
    monkeypatch.delenv("DEMO_SIMULATE_POSTCONDITION_MISMATCH", raising=False)
    yield


def _action_context(workflow_id: str) -> ActionContext:
    return ActionContext(
        workflow_id=workflow_id,
        requesting_user="demo-user",
        agent_id="demo-agent",
        original_instruction=BROAD_INSTRUCTION,
    )


def _mismatch(predicted=92, actual=93, production=0):
    return PostconditionResult(
        predicted_count=predicted, actual_count=actual, production_affected=production, status="MISMATCH"
    )


DELETE_SPEC = get_tool_spec("delete_users")
DEACTIVATE_SPEC = get_tool_spec("deactivate_users")


# ---------------------------------------------------------------------------
# resolve_final_postcondition: pass-through for non-MISMATCH statuses
# ---------------------------------------------------------------------------


def test_verified_status_passes_through_unchanged_and_never_restores():
    verified = PostconditionResult(predicted_count=92, actual_count=92, production_affected=0, status="VERIFIED")
    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)

    with mock.patch("proofgate.rollback.restore_snapshot") as spy:
        result = resolve_final_postcondition(
            spec=DELETE_SPEC,
            rollback_proof=proof,
            rollback_proof_valid=True,
            selector_arguments={"inactive_days": 90, "environment": "test"},
            postcondition_result=verified,
        )
    assert result.status == "VERIFIED"
    spy.assert_not_called()


def test_production_impact_manual_review_passes_through_unchanged():
    review = PostconditionResult(predicted_count=92, actual_count=92, production_affected=3, status="MANUAL_REVIEW_REQUIRED")

    with mock.patch("proofgate.rollback.restore_snapshot") as spy:
        result = resolve_final_postcondition(
            spec=DELETE_SPEC,
            rollback_proof=None,
            rollback_proof_valid=False,
            selector_arguments={"inactive_days": 90, "environment": "test"},
            postcondition_result=review,
        )
    assert result.status == "MANUAL_REVIEW_REQUIRED"
    spy.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_final_postcondition: ineligibility -> MANUAL_REVIEW_REQUIRED
# ---------------------------------------------------------------------------


def test_mismatch_on_reversible_action_is_not_eligible_for_rollback():
    result = resolve_final_postcondition(
        spec=DEACTIVATE_SPEC,
        rollback_proof=None,
        rollback_proof_valid=False,
        selector_arguments={"inactive_days": 90, "environment": "test"},
        postcondition_result=_mismatch(),
    )
    assert result.status == "MANUAL_REVIEW_REQUIRED"


def test_mismatch_with_no_rollback_proof_is_not_eligible():
    result = resolve_final_postcondition(
        spec=DELETE_SPEC,
        rollback_proof=None,
        rollback_proof_valid=False,
        selector_arguments={"inactive_days": 90, "environment": "test"},
        postcondition_result=_mismatch(),
    )
    assert result.status == "MANUAL_REVIEW_REQUIRED"


def test_mismatch_with_invalid_proof_is_not_eligible():
    fake_proof = RollbackProof(
        snapshot_id="snap-does-not-exist", resource="users", selector_hash="irrelevant", max_affected_rows=92
    )
    result = resolve_final_postcondition(
        spec=DELETE_SPEC,
        rollback_proof=fake_proof,
        rollback_proof_valid=False,  # proof validation already failed upstream
        selector_arguments={"inactive_days": 90, "environment": "test"},
        postcondition_result=_mismatch(),
    )
    assert result.status == "MANUAL_REVIEW_REQUIRED"


def test_mismatch_with_missing_snapshot_metadata_is_manual_review():
    proof = RollbackProof(
        snapshot_id="snap-truly-does-not-exist",
        resource="users",
        selector_hash="irrelevant",
        max_affected_rows=92,
    )
    result = resolve_final_postcondition(
        spec=DELETE_SPEC,
        rollback_proof=proof,
        rollback_proof_valid=True,  # hypothetically valid, but snapshot store has nothing
        selector_arguments={"inactive_days": 90, "environment": "test"},
        postcondition_result=_mismatch(),
    )
    assert result.status == "MANUAL_REVIEW_REQUIRED"


# ---------------------------------------------------------------------------
# resolve_final_postcondition: real successful rollback and real failures
# ---------------------------------------------------------------------------


def test_successful_rollback_end_to_end():
    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)
    pre_digest = compute_users_digest(WORKING_DB_PATH, environment="test")
    pre_total = count_rows(WORKING_DB_PATH)

    delete_users(inactive_days=90, environment="test")
    mutation_actual = count_rows(WORKING_DB_PATH)
    assert mutation_actual == pre_total - 92

    result = resolve_final_postcondition(
        spec=DELETE_SPEC,
        rollback_proof=proof,
        rollback_proof_valid=True,
        selector_arguments={"inactive_days": 90, "environment": "test"},
        postcondition_result=_mismatch(predicted=92, actual=91),
    )

    assert result.status == "ROLLED_BACK"
    assert count_rows(WORKING_DB_PATH) == pre_total
    assert compute_users_digest(WORKING_DB_PATH, environment="test") == pre_digest


def test_corrupted_snapshot_yields_manual_review_never_rolled_back():
    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)
    metadata = get_snapshot_metadata(proof.snapshot_id)
    Path(metadata["snapshot_path"]).write_bytes(b"corrupted, not sqlite")

    delete_users(inactive_days=90, environment="test")
    result = resolve_final_postcondition(
        spec=DELETE_SPEC,
        rollback_proof=proof,
        rollback_proof_valid=True,
        selector_arguments={"inactive_days": 90, "environment": "test"},
        postcondition_result=_mismatch(),
    )
    assert result.status == "MANUAL_REVIEW_REQUIRED"
    assert result.status != "ROLLED_BACK"


def test_verification_only_reports_rolled_back_when_digest_actually_matches():
    """Even if restore_snapshot self-reports success, resolve_final_postcondition
    must not report ROLLED_BACK unless its own independent digest/count
    comparison agrees -- the restore function must not grade its own work."""
    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)
    delete_users(inactive_days=90, environment="test")

    fake_success = mock.Mock(success=True, production_rows_restored=0)
    with mock.patch("proofgate.rollback.restore_snapshot", return_value=fake_success):
        with mock.patch(
            "proofgate.rollback.compute_users_digest",
            side_effect=["expected-digest-from-snapshot", "different-digest-after-restore"],
        ):
            result = resolve_final_postcondition(
                spec=DELETE_SPEC,
                rollback_proof=proof,
                rollback_proof_valid=True,
                selector_arguments={"inactive_days": 90, "environment": "test"},
                postcondition_result=_mismatch(),
            )
    assert result.status == "MANUAL_REVIEW_REQUIRED"


# ---------------------------------------------------------------------------
# Full integration through guarded_delete_users
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_workflow_states():
    for wf in ("wf-rollback-1", "wf-rollback-2", "wf-rollback-3", "wf-rollback-4"):
        reset_workflow_state(wf)
    yield


def _read_audit_events():
    # audit_module.DEFAULT_AUDIT_PATH is monkeypatched per-test by
    # tests/conftest.py's autouse _isolate_audit_log fixture -- must be
    # read fresh at call time, never bound as a default argument value,
    # exactly like proofgate.audit's own functions do.
    path = audit_module.DEFAULT_AUDIT_PATH
    if not path.exists():
        return []
    import json

    return [json.loads(line) for line in path.read_text().strip().splitlines() if line]


def test_normal_corrected_delete_unaffected_when_flag_absent():
    """Part A regression: without the flag, behavior is exactly Slice 9's
    VERIFIED/92-budget outcome -- resolve_final_postcondition changes
    nothing here."""
    workflow_id = "wf-rollback-1"
    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)

    result = guarded_delete_users(
        action_context=_action_context(workflow_id), inactive_days=90, environment="test", rollback_proof=proof
    )

    assert result.verdict == "ALLOW"
    postcondition = get_last_postcondition_result(workflow_id)
    assert postcondition.status == "VERIFIED"
    assert get_workflow_budget(workflow_id).rows_mutated == 92


def test_guarded_delete_with_mismatch_hook_resolves_to_rolled_back(monkeypatch):
    monkeypatch.setenv("DEMO_SIMULATE_POSTCONDITION_MISMATCH", "true")
    reset_audit_log()
    workflow_id = "wf-rollback-2"
    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)

    pre_total = count_rows(WORKING_DB_PATH)
    pre_test_digest = compute_users_digest(WORKING_DB_PATH, environment="test")

    result = guarded_delete_users(
        action_context=_action_context(workflow_id), inactive_days=90, environment="test", rollback_proof=proof
    )

    assert result.verdict == "ALLOW"
    assert result.executed is True

    postcondition = get_last_postcondition_result(workflow_id)
    assert postcondition.status == "ROLLED_BACK"

    # Final database state matches pre-execution state exactly.
    assert count_rows(WORKING_DB_PATH) == pre_total
    assert compute_users_digest(WORKING_DB_PATH, environment="test") == pre_test_digest
    assert count_rows(WORKING_DB_PATH, environment="production") == 9981 + 500

    events = _read_audit_events()
    assert len(events) == 1
    assert events[0]["postcondition_result"]["status"] == "ROLLED_BACK"


def test_rollback_consumes_zero_additional_budget_and_refunds_zero(monkeypatch):
    monkeypatch.setenv("DEMO_SIMULATE_POSTCONDITION_MISMATCH", "true")
    workflow_id = "wf-rollback-3"
    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)

    result = guarded_delete_users(
        action_context=_action_context(workflow_id), inactive_days=90, environment="test", rollback_proof=proof
    )
    assert result.verdict == "ALLOW"

    budget = get_workflow_budget(workflow_id)
    # The real mutation genuinely deleted one extra row (92 intended + 1
    # demo-injected); budget honestly reflects the real total work done --
    # rollback itself adds nothing beyond that and refunds nothing.
    assert budget.rows_mutated == 93
    assert budget.production_rows_mutated == 0

    # Rollback happened entirely inside this one guarded_execute call --
    # budget after rollback equals budget right after the real mutation,
    # proving zero additional consumption and zero refund.
    assert budget.rows_mutated == 93  # unchanged by the subsequent rollback


def test_second_trusted_restore_after_automatic_rollback_is_idempotent_and_free(monkeypatch):
    monkeypatch.setenv("DEMO_SIMULATE_POSTCONDITION_MISMATCH", "true")
    workflow_id = "wf-rollback-4"
    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)

    guarded_delete_users(
        action_context=_action_context(workflow_id), inactive_days=90, environment="test", rollback_proof=proof
    )
    budget_after_rollback = get_workflow_budget(workflow_id).rows_mutated

    second = restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": "test"})
    assert second.status == "ALREADY_RESTORED"
    assert second.restored_count == 0

    # restore_snapshot is a plain Operations function -- it never touches
    # workflow budget or the audit log by itself.
    assert get_workflow_budget(workflow_id).rows_mutated == budget_after_rollback


def test_pristine_db_unaffected_by_full_rollback_flow(monkeypatch):
    import hashlib

    monkeypatch.setenv("DEMO_SIMULATE_POSTCONDITION_MISMATCH", "true")
    before_hash = hashlib.sha256(PRISTINE_DB_PATH.read_bytes()).hexdigest()

    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)
    guarded_delete_users(
        action_context=_action_context("wf-rollback-2"), inactive_days=90, environment="test", rollback_proof=proof
    )

    after_hash = hashlib.sha256(PRISTINE_DB_PATH.read_bytes()).hexdigest()
    assert before_hash == after_hash
