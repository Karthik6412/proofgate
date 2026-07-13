"""Slices 6+7 tests: real snapshot proof and the corrected same-tool ALLOW path.

guarded_delete_users always operates on the real operations/working.db (it
has no test-only db_path seam, per the frozen contract), so these tests
reset that real file before each test via an autouse fixture. Workflow
budget state now genuinely persists per workflow_id (Slices 8+9), so the
fixed workflow_ids reused across tests in this file are also reset before
each test to keep them isolated.
"""

import hashlib
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

import operations.actions as operations_actions
from operations.actions import count_rows, delete_users
from operations.database import PRISTINE_DB_PATH, WORKING_DB_PATH, reset_working_db
from operations.snapshots import create_snapshot, get_snapshot_metadata
from proofgate.budgets import reset_workflow_state
from proofgate.core import get_last_mutation_result, guarded_delete_users
from proofgate.models import ActionContext, ImpactEnvelope, RollbackProof
from proofgate.policy import (
    RULE_INTENT_BOUNDARY,
    RULE_RECOVERY_PROOF,
    RULE_WORKFLOW_BUDGET,
)
from proofgate.proofs import validate_rollback_proof
from proofgate.selector import compute_selector_hash

BROAD_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


@pytest.fixture(autouse=True)
def _reset_real_working_db():
    reset_working_db()
    yield
    reset_working_db()


@pytest.fixture(autouse=True)
def _reset_workflow_states():
    for workflow_id in ("wf-snapshot", "wf-allow", "wf-a", "wf-b"):
        reset_workflow_state(workflow_id)
    yield


def _action_context(workflow_id: str = "wf-snapshot") -> ActionContext:
    return ActionContext(
        workflow_id=workflow_id,
        requesting_user="demo-user",
        agent_id="demo-agent",
        original_instruction=BROAD_INSTRUCTION,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Snapshot creation
# ---------------------------------------------------------------------------


def test_create_snapshot_creates_a_real_artifact():
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    metadata = get_snapshot_metadata(proof.snapshot_id)
    snapshot_path = Path(metadata["snapshot_path"])

    assert snapshot_path.exists()
    # It must be a real, openable sqlite database, not a placeholder file.
    conn = sqlite3.connect(str(snapshot_path))
    try:
        row_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()
    assert row_count == 10623


def test_snapshot_id_is_generated_server_side():
    import inspect

    params = set(inspect.signature(create_snapshot).parameters)
    assert "snapshot_id" not in params

    proof_1 = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    proof_2 = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    assert proof_1.snapshot_id
    assert proof_2.snapshot_id
    assert proof_1.snapshot_id != proof_2.snapshot_id


def test_server_side_metadata_is_persisted():
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    metadata = get_snapshot_metadata(proof.snapshot_id)

    assert metadata is not None
    assert metadata["snapshot_id"] == proof.snapshot_id
    assert metadata["resource"] == "users"
    assert metadata["selector_hash"] == proof.selector_hash
    assert metadata["max_affected_rows"] == 92
    assert "snapshot_path" in metadata
    assert "created_at" in metadata


def test_pristine_db_unchanged_by_snapshot_and_allow_flow():
    before_hash = _sha256(PRISTINE_DB_PATH)

    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    guarded_delete_users(
        action_context=_action_context(),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )

    after_hash = _sha256(PRISTINE_DB_PATH)
    assert before_hash == after_hash


def test_corrected_selector_hash_is_stable():
    hash_a = compute_selector_hash({"inactive_days": 90, "environment": "test"})
    hash_b = compute_selector_hash({"inactive_days": 90, "environment": "test"})
    assert hash_a == hash_b

    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    assert proof.selector_hash == hash_a


def test_pristine_db_rejected_as_snapshot_target():
    with pytest.raises(ValueError):
        create_snapshot(
            resource="users",
            inactive_days=90,
            environment="test",
            max_affected_rows=92,
            db_path=PRISTINE_DB_PATH,
        )


# ---------------------------------------------------------------------------
# Proof validation
# ---------------------------------------------------------------------------


def _corrected_impact() -> ImpactEnvelope:
    from operations.actions import preview_delete_users

    return preview_delete_users(inactive_days=90, environment="test")


def test_valid_proof_passes_every_validation_check():
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    result = validate_rollback_proof(proof, resource="users", impact=_corrected_impact())

    assert result.snapshot_exists is True
    assert result.resource_matches is True
    assert result.selector_hash_matches is True
    assert result.count_within_approved_maximum is True
    assert result.valid is True


def test_unknown_snapshot_id_fails_validation():
    fake_proof = RollbackProof(
        snapshot_id="snap-does-not-exist",
        resource="users",
        selector_hash=compute_selector_hash({"inactive_days": 90, "environment": "test"}),
        max_affected_rows=92,
    )
    result = validate_rollback_proof(fake_proof, resource="users", impact=_corrected_impact())

    assert result.snapshot_exists is False
    assert result.valid is False


def test_missing_snapshot_file_fails_validation():
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    metadata = get_snapshot_metadata(proof.snapshot_id)
    Path(metadata["snapshot_path"]).unlink()

    result = validate_rollback_proof(proof, resource="users", impact=_corrected_impact())

    assert result.snapshot_exists is False
    assert result.valid is False


def test_resource_mismatch_fails_validation():
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    result = validate_rollback_proof(
        proof, resource="some_other_resource", impact=_corrected_impact()
    )

    assert result.resource_matches is False
    assert result.valid is False


def test_selector_mismatch_fails_validation():
    from operations.actions import preview_delete_users

    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    broad_impact = preview_delete_users(inactive_days=90, environment=None)

    result = validate_rollback_proof(proof, resource="users", impact=broad_impact)

    assert result.selector_hash_matches is False
    assert result.valid is False


def test_supplied_max_affected_rows_cannot_override_stored_metadata():
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    tampered_proof = RollbackProof(
        snapshot_id=proof.snapshot_id,
        resource=proof.resource,
        selector_hash=proof.selector_hash,
        max_affected_rows=999999,
    )

    result = validate_rollback_proof(
        tampered_proof, resource="users", impact=_corrected_impact()
    )

    assert result.count_within_approved_maximum is False
    assert result.valid is False


def test_supplied_selector_hash_cannot_override_stored_metadata():
    from operations.actions import preview_delete_users

    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    broad_impact = preview_delete_users(inactive_days=90, environment=None)
    tampered_proof = RollbackProof(
        snapshot_id=proof.snapshot_id,
        resource=proof.resource,
        selector_hash=broad_impact.selector_hash,
        max_affected_rows=proof.max_affected_rows,
    )

    result = validate_rollback_proof(tampered_proof, resource="users", impact=broad_impact)

    assert result.selector_hash_matches is False
    assert result.valid is False


def test_affected_count_above_stored_max_fails_validation():
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=50
    )
    result = validate_rollback_proof(proof, resource="users", impact=_corrected_impact())

    assert result.count_within_approved_maximum is False
    assert result.valid is False


# ---------------------------------------------------------------------------
# Corrected-call behavior through guarded_delete_users
# ---------------------------------------------------------------------------


def test_corrected_call_without_proof_is_blocked():
    result = guarded_delete_users(
        action_context=_action_context(),
        inactive_days=90,
        environment="test",
        rollback_proof=None,
    )

    assert result.verdict == "BLOCK"
    assert {r.rule_id for r in result.triggered_rules} == {RULE_RECOVERY_PROOF}
    assert result.executed is False


def test_corrected_call_with_invalid_proof_is_blocked():
    fake_proof = RollbackProof(
        snapshot_id="snap-does-not-exist",
        resource="users",
        selector_hash=compute_selector_hash({"inactive_days": 90, "environment": "test"}),
        max_affected_rows=92,
    )
    result = guarded_delete_users(
        action_context=_action_context(),
        inactive_days=90,
        environment="test",
        rollback_proof=fake_proof,
    )

    assert result.verdict == "BLOCK"
    assert {r.rule_id for r in result.triggered_rules} == {RULE_RECOVERY_PROOF}
    assert result.executed is False


def test_both_blocked_corrected_calls_mutate_zero_rows():
    before = count_rows(WORKING_DB_PATH)

    guarded_delete_users(
        action_context=_action_context("wf-a"),
        inactive_days=90,
        environment="test",
        rollback_proof=None,
    )
    assert count_rows(WORKING_DB_PATH) == before

    fake_proof = RollbackProof(
        snapshot_id="snap-does-not-exist",
        resource="users",
        selector_hash=compute_selector_hash({"inactive_days": 90, "environment": "test"}),
        max_affected_rows=92,
    )
    guarded_delete_users(
        action_context=_action_context("wf-b"),
        inactive_days=90,
        environment="test",
        rollback_proof=fake_proof,
    )
    assert count_rows(WORKING_DB_PATH) == before


def test_corrected_call_with_valid_proof_is_allowed_and_deletes_exactly_92_test_rows():
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    workflow_id = "wf-allow"

    with mock.patch(
        "operations.actions.delete_users", wraps=operations_actions.delete_users
    ) as spy:
        result = guarded_delete_users(
            action_context=_action_context(workflow_id),
            inactive_days=90,
            environment="test",
            rollback_proof=proof,
        )
        spy.assert_called_once_with(inactive_days=90, environment="test")

    assert result.verdict == "ALLOW"
    assert result.triggered_rules == []
    assert result.executed is True

    mutation_result = get_last_mutation_result(workflow_id)
    assert mutation_result is not None
    assert mutation_result.affected_count == 92
    assert mutation_result.test_affected == 92
    assert mutation_result.production_affected == 0

    assert count_rows(WORKING_DB_PATH) == 10623 - 92
    assert count_rows(WORKING_DB_PATH, environment="production") == 9981 + 500


def test_delete_users_never_invoked_before_allow():
    with mock.patch(
        "operations.actions.delete_users", wraps=operations_actions.delete_users
    ) as spy:
        result = guarded_delete_users(
            action_context=_action_context(),
            inactive_days=90,
            environment="test",
            rollback_proof=None,
        )
        assert result.verdict == "BLOCK"
        spy.assert_not_called()


# ---------------------------------------------------------------------------
# Proof does not override intent mismatch or workflow budget
# ---------------------------------------------------------------------------


def test_broad_action_remains_blocked_with_a_genuinely_valid_broad_proof():
    broad_proof = create_snapshot(
        resource="users", inactive_days=90, environment=None, max_affected_rows=10073
    )

    from operations.actions import preview_delete_users

    broad_impact = preview_delete_users(inactive_days=90, environment=None)
    proof_check = validate_rollback_proof(broad_proof, resource="users", impact=broad_impact)
    assert proof_check.valid is True  # sanity: this really is a valid proof

    result = guarded_delete_users(
        action_context=_action_context(),
        inactive_days=90,
        environment=None,
        rollback_proof=broad_proof,
    )

    assert result.verdict == "BLOCK"
    assert result.executed is False


def test_valid_broad_proof_does_not_override_intent_boundary_or_workflow_budget():
    broad_proof = create_snapshot(
        resource="users", inactive_days=90, environment=None, max_affected_rows=10073
    )

    result = guarded_delete_users(
        action_context=_action_context(),
        inactive_days=90,
        environment=None,
        rollback_proof=broad_proof,
    )

    triggered_ids = {r.rule_id for r in result.triggered_rules}
    assert RULE_INTENT_BOUNDARY in triggered_ids
    assert RULE_WORKFLOW_BUDGET in triggered_ids
    # Recovery proof rule may legitimately be absent since the proof is valid.
    assert triggered_ids == {RULE_INTENT_BOUNDARY, RULE_WORKFLOW_BUDGET}


def test_pristine_db_rejected_as_mutation_target_still_holds():
    with pytest.raises(ValueError):
        delete_users(inactive_days=90, environment="test", db_path=PRISTINE_DB_PATH)
