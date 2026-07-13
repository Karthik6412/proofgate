"""Automatic-rollback orchestration (Slice 23).

Wires operations.restoration.restore_snapshot into the existing
post-ALLOW postcondition flow. This module never decides ALLOW/BLOCK
(that remains proofgate.policy's job) and never touches workflow-budget
accounting (proofgate.budgets.record_execution is called by
proofgate.core exactly as before, keyed only off the real MutationResult
from the original mutation -- untouched by anything here). It exists
solely to resolve what final PostconditionResult.status an executed,
rollback-capable ALLOW should report once verify_postcondition's genuine
output is known.

"The restore function must not grade its own work": restore_snapshot's
own RestoreResult.verification_passed is an internal operational sanity
check, not the deciding signal. The actual, independent verification
here recomputes its own row-content digest and row counts directly
against the snapshot artifact (the authoritative pre-execution state,
since it was copied before this call's mutation ran) and compares them
to the post-restoration state of operations/working.db -- never trusting
restore_snapshot's self-report alone.
"""

from pathlib import Path

from operations.actions import count_rows
from operations.database import WORKING_DB_PATH
from operations.restoration import compute_users_digest, restore_snapshot
from operations.snapshots import get_snapshot_metadata
from proofgate.models import PostconditionResult, RollbackProof
from proofgate.registry import GuardedToolSpec


def resolve_final_postcondition(
    *,
    spec: GuardedToolSpec,
    rollback_proof: RollbackProof | None,
    rollback_proof_valid: bool,
    selector_arguments: dict,
    postcondition_result: PostconditionResult,
) -> PostconditionResult:
    """Return the final PostconditionResult for this execution.

    Only ever inspects a MISMATCH result; VERIFIED and the existing
    production-impact MANUAL_REVIEW_REQUIRED case pass through unchanged
    -- automatic rollback is never attempted for a genuine production
    impact, since restoration here can never touch production rows by
    design (see operations/restoration.py), so it could never honestly
    resolve that case anyway.
    """
    if postcondition_result.status != "MISMATCH":
        return postcondition_result

    eligible = (
        spec.hard_delete
        and rollback_proof_valid
        and rollback_proof is not None
        and bool(rollback_proof.snapshot_id)
    )
    if not eligible:
        return postcondition_result.model_copy(update={"status": "MANUAL_REVIEW_REQUIRED"})

    metadata = get_snapshot_metadata(rollback_proof.snapshot_id)
    if metadata is None:
        return postcondition_result.model_copy(update={"status": "MANUAL_REVIEW_REQUIRED"})

    snapshot_path = Path(metadata["snapshot_path"])
    if not snapshot_path.exists():
        return postcondition_result.model_copy(update={"status": "MANUAL_REVIEW_REQUIRED"})

    # The snapshot IS the authoritative pre-execution state (copied before
    # this call's mutation ran) -- independent verification is computed
    # straight from it, never from anything proofgate.core already held
    # in memory, and never from restore_snapshot's own self-report.
    try:
        expected_total = count_rows(snapshot_path)
        expected_production = count_rows(snapshot_path, environment="production")
        expected_test_digest = compute_users_digest(snapshot_path, environment="test")
    except Exception:
        return postcondition_result.model_copy(update={"status": "MANUAL_REVIEW_REQUIRED"})

    restore_result = restore_snapshot(rollback_proof.snapshot_id, expected_selector=selector_arguments)

    if not restore_result.success or restore_result.production_rows_restored != 0:
        return postcondition_result.model_copy(update={"status": "MANUAL_REVIEW_REQUIRED"})

    actual_total = count_rows(WORKING_DB_PATH)
    actual_production = count_rows(WORKING_DB_PATH, environment="production")
    actual_test_digest = compute_users_digest(WORKING_DB_PATH, environment="test")

    verification_ok = (
        expected_total == actual_total
        and expected_production == actual_production
        and expected_test_digest == actual_test_digest
    )

    final_status = "ROLLED_BACK" if verification_ok else "MANUAL_REVIEW_REQUIRED"
    return postcondition_result.model_copy(update={"status": final_status})
