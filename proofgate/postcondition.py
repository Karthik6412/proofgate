"""Pure postcondition verifier.

MutationResult is the authoritative actual impact. This never re-queries
the database or re-runs a preview after deletion; it only compares the
pre-execution ImpactEnvelope against the MutationResult the mutation
itself returned.
"""

from proofgate.models import ImpactEnvelope, MutationResult, PostconditionResult


def verify_postcondition(
    impact_envelope: ImpactEnvelope,
    mutation_result: MutationResult,
) -> PostconditionResult:
    predicted_count = impact_envelope.estimated_count
    actual_count = mutation_result.affected_count
    production_affected = mutation_result.production_affected

    if production_affected > 0:
        status = "MANUAL_REVIEW_REQUIRED"
    elif predicted_count == actual_count:
        status = "VERIFIED"
    else:
        status = "MISMATCH"

    return PostconditionResult(
        predicted_count=predicted_count,
        actual_count=actual_count,
        production_affected=production_affected,
        status=status,
    )
