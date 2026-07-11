"""ProofGate protected action boundary.

guarded_delete_users is the sole caller path that may reach
operations.delete_users, and only after a genuine deterministic ALLOW.
Proof establishes recoverability, not authorization: a valid rollback
proof can clear RULE_RECOVERY_PROOF, but it never overrides
RULE_INTENT_BOUNDARY or RULE_WORKFLOW_BUDGET. Immediately after a genuine
ALLOW, the mutation is verified against its pre-execution ImpactEnvelope
and the real per-workflow budget is updated using the actual executed
counts. Every invocation writes exactly one audit event built from the
values this function already computed -- nothing is recomputed for
audit purposes. CRAFT evidence (upstream, read-only, never authoritative
for mutation impact) is only ever retrieved here by workflow_id from
already-stored state; this function never calls CRAFT.
"""

from agent.nebius_client import (
    extract_intent_live_or_fallback,
    extract_risk_features_live_or_fallback,
)
from operations.actions import delete_users, preview_delete_users
from proofgate.audit import SCHEMA_VERSION, append_audit_event, current_timestamp, new_event_id
from proofgate.budgets import (
    get_craft_evidence,
    get_last_mutation_result,
    get_last_postcondition_result,
    get_workflow_budget,
    record_execution,
    reset_workflow_state,
)
from proofgate.models import ActionContext, AuditEvent, EnforcementResult, RollbackProof
from proofgate.policy import (
    POLICY_VERSION,
    build_missing_requirements,
    build_suggested_repairs,
    compute_risk_score,
    evaluate_policy,
)
from proofgate.postcondition import verify_postcondition
from proofgate.proofs import validate_rollback_proof

RESOURCE_USERS = "users"
TOOL_NAME = "delete_users"

# Re-exported so callers/tests can reach per-workflow state through
# proofgate.core, same as get_workflow_budget/reset_workflow_state.
__all__ = [
    "guarded_delete_users",
    "get_last_mutation_result",
    "get_last_postcondition_result",
    "get_workflow_budget",
    "reset_workflow_state",
]


def guarded_delete_users(
    action_context: ActionContext,
    inactive_days: int,
    environment: str | None,
    rollback_proof: RollbackProof | None,
) -> EnforcementResult:
    event_id = new_event_id()
    proposed_arguments = {"inactive_days": inactive_days, "environment": environment}

    intent_outcome = extract_intent_live_or_fallback(action_context.original_instruction)
    intent = intent_outcome.value

    try:
        impact = preview_delete_users(inactive_days=inactive_days, environment=environment)
    except Exception:
        impact = None

    proof_validation = None
    if impact is not None:
        proof_validation = validate_rollback_proof(
            rollback_proof, resource=RESOURCE_USERS, impact=impact
        )
        rollback_proof_valid = proof_validation.valid
    else:
        rollback_proof_valid = False

    risk_outcome = (
        extract_risk_features_live_or_fallback(
            action_context.original_instruction, intent, proposed_arguments, impact
        )
        if impact is not None
        else None
    )
    risk = risk_outcome.value if risk_outcome is not None else None

    workflow_budget = get_workflow_budget(action_context.workflow_id)
    budget_before = workflow_budget.model_copy()

    triggered_rules, risk_factors = evaluate_policy(
        intent=intent,
        proposed_arguments=proposed_arguments,
        impact=impact,
        risk=risk,
        rollback_proof_valid=rollback_proof_valid,
        workflow_budget=workflow_budget,
    )

    verdict = "BLOCK" if triggered_rules else "ALLOW"

    if impact is not None and risk is not None:
        risk_score = compute_risk_score(
            impact=impact,
            risk=risk,
            proof_required_and_missing=impact.hard_delete and not rollback_proof_valid,
        )
    else:
        risk_score = 10.0

    missing_requirements = build_missing_requirements(
        intent=intent,
        rollback_proof_valid=rollback_proof_valid,
        impact=impact,
        workflow_budget=workflow_budget,
    )
    suggested_repairs = build_suggested_repairs(intent=intent, inactive_days=inactive_days)

    mutation_result = None
    postcondition_result = None
    executed = False
    if verdict == "ALLOW":
        mutation_result = delete_users(inactive_days=inactive_days, environment=environment)
        postcondition_result = verify_postcondition(impact, mutation_result)
        record_execution(action_context.workflow_id, mutation_result, postcondition_result)
        executed = True

    budget_after = get_workflow_budget(action_context.workflow_id).model_copy()

    if rollback_proof is None:
        proof_status = "MISSING"
        proof_checks = None
    elif proof_validation is None:
        # Impact was unknown, so no proof validation could actually run.
        proof_status = "INVALID"
        proof_checks = None
    else:
        proof_checks = {
            "snapshot_exists": proof_validation.snapshot_exists,
            "resource_matches": proof_validation.resource_matches,
            "selector_hash_matches": proof_validation.selector_hash_matches,
            "count_within_approved_maximum": proof_validation.count_within_approved_maximum,
        }
        proof_status = "VALID" if proof_validation.valid else "INVALID"

    if risk_outcome is None:
        # Risk extraction never ran because impact was unknown. Never
        # report "nebius_live" when only one of the two extractions could
        # possibly have succeeded live.
        extraction_mode = "mixed" if intent_outcome.mode == "nebius_live" else "deterministic_fallback"
    elif intent_outcome.mode == "nebius_live" and risk_outcome.mode == "nebius_live":
        extraction_mode = "nebius_live"
    elif intent_outcome.mode == "nebius_live" or risk_outcome.mode == "nebius_live":
        extraction_mode = "mixed"
    else:
        extraction_mode = "deterministic_fallback"

    nebius_model = intent_outcome.model or (risk_outcome.model if risk_outcome else None)

    # Read-only retrieval of whatever CRAFT evidence the orchestrator/UI
    # already prepared (via craft.evidence.prepare_craft_evidence) and
    # stored for this workflow_id -- never a live CRAFT call from here.
    craft_evidence = get_craft_evidence(action_context.workflow_id)

    audit_event = AuditEvent(
        schema_version=SCHEMA_VERSION,
        event_id=event_id,
        workflow_id=action_context.workflow_id,
        timestamp=current_timestamp(),
        original_instruction=action_context.original_instruction,
        requesting_user=action_context.requesting_user,
        agent_id=action_context.agent_id,
        tool_name=TOOL_NAME,
        tool_arguments=proposed_arguments,
        intent_constraints=intent,
        impact_envelope=impact,
        risk_features=risk,
        risk_score=risk_score,
        risk_factors=risk_factors,
        triggered_rules=triggered_rules,
        policy_version=POLICY_VERSION,
        extraction_mode=extraction_mode,
        nebius_model=nebius_model,
        craft_evidence=craft_evidence,
        verdict=verdict,
        missing_requirements=missing_requirements,
        suggested_repairs=suggested_repairs,
        proof_status=proof_status,
        proof_checks=proof_checks,
        execution_status="EXECUTED" if executed else "NOT_EXECUTED",
        mutation_result=mutation_result,
        postcondition_result=postcondition_result,
        workflow_budget_before=budget_before,
        workflow_budget_after=budget_after,
    )
    append_audit_event(audit_event)

    return EnforcementResult(
        verdict=verdict,
        risk_score=risk_score,
        risk_factors=risk_factors,
        triggered_rules=triggered_rules,
        missing_requirements=missing_requirements,
        suggested_repairs=suggested_repairs,
        executed=executed,
        audit_event_id=event_id,
    )
