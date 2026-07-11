"""Rollback-proof validation.

Proof establishes recoverability, not authorization. Validation never
trusts caller-supplied RollbackProof fields alone: every field is
cross-checked against the authoritative server-side snapshot metadata
retrieved by snapshot_id. A valid proof must never override an intent
mismatch or a workflow-budget violation -- that is enforced separately
by proofgate.policy, not here.
"""

from pathlib import Path

from operations.snapshots import get_snapshot_metadata
from proofgate.models import ImpactEnvelope, ProofValidationResult, RollbackProof


def validate_rollback_proof(
    rollback_proof: RollbackProof | None,
    resource: str,
    impact: ImpactEnvelope,
) -> ProofValidationResult:
    if rollback_proof is None:
        return ProofValidationResult(
            snapshot_exists=False,
            resource_matches=False,
            selector_hash_matches=False,
            count_within_approved_maximum=False,
            valid=False,
        )

    stored = get_snapshot_metadata(rollback_proof.snapshot_id)
    snapshot_exists = stored is not None and Path(stored["snapshot_path"]).exists()

    resource_matches = snapshot_exists and (
        stored["resource"] == resource and rollback_proof.resource == stored["resource"]
    )

    selector_hash_matches = snapshot_exists and (
        stored["selector_hash"] == impact.selector_hash
        and rollback_proof.selector_hash == stored["selector_hash"]
    )

    count_within_approved_maximum = snapshot_exists and (
        rollback_proof.max_affected_rows == stored["max_affected_rows"]
        and impact.estimated_count <= stored["max_affected_rows"]
    )

    valid = (
        snapshot_exists
        and resource_matches
        and selector_hash_matches
        and count_within_approved_maximum
    )

    return ProofValidationResult(
        snapshot_exists=snapshot_exists,
        resource_matches=resource_matches,
        selector_hash_matches=selector_hash_matches,
        count_within_approved_maximum=count_within_approved_maximum,
        valid=valid,
    )
