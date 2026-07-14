"""ProofGate protected action boundary.

guarded_execute is the sole generic caller path that may reach a
registered tool's mutation function, and only after a genuine
deterministic ALLOW. guarded_delete_users is a thin compatibility wrapper
around guarded_execute("delete_users", ...) -- the pipeline itself is
unchanged from prior slices, only how it's invoked. Proof establishes
recoverability, not authorization: a valid rollback proof can clear
RULE_RECOVERY_PROOF, but it never overrides RULE_INTENT_BOUNDARY or
RULE_WORKFLOW_BUDGET. Immediately after a genuine ALLOW, the mutation is
verified against its pre-execution ImpactEnvelope and the real
per-workflow budget is updated using the actual executed counts. Every
invocation writes exactly one audit event built from the values this
function already computed -- nothing is recomputed for audit purposes.
CRAFT evidence (upstream, read-only, never authoritative for mutation
impact) is only ever retrieved here by workflow_id from already-stored
state; this function never calls CRAFT.

Unregistered tool_name values fail closed: RULE_UNKNOWN_IMPACT fires and
nothing is previewed, executed, snapshotted, or budget-consumed. Intent
extraction still runs for an unknown tool -- it operates only on the
free-text original_instruction, is side-effect-free, and is needed to
honestly populate AuditEvent.intent_constraints (a required field).
"""

from agent.nebius_client import (
    extract_intent_live_or_fallback,
    extract_risk_features_live_or_fallback,
)
from proofgate.audit import SCHEMA_VERSION, append_audit_event, current_timestamp, new_event_id
from proofgate.budgets import (
    get_craft_evidence,
    get_last_mutation_result,
    get_last_postcondition_result,
    get_workflow_budget,
    record_execution,
    reset_workflow_state,
)
from proofgate.models import ActionContext, AuditEvent, EnforcementResult, RollbackProof, TriggeredRule
from proofgate.policy import (
    POLICY_VERSION,
    RULE_UNKNOWN_IMPACT,
    build_missing_requirements,
    build_suggested_repairs,
    compute_risk_score,
    evaluate_policy,
)
from proofgate.postcondition import verify_postcondition
from proofgate.proofs import validate_rollback_proof
from proofgate.registry import get_tool_spec
from proofgate.rollback import resolve_final_postcondition

# Re-exported so callers/tests can reach per-workflow state through
# proofgate.core, same as get_workflow_budget/reset_workflow_state.
__all__ = [
    "guarded_execute",
    "guarded_delete_users",
    "get_last_mutation_result",
    "get_last_postcondition_result",
    "get_workflow_budget",
    "reset_workflow_state",
]


def guarded_execute(
    tool_name: str,
    action_context: ActionContext,
    arguments: dict,
    rollback_proof: RollbackProof | None,
) -> EnforcementResult:
    """Generic enforcement entrypoint. Looks up tool_name in the registry
    (proofgate.registry); unregistered tools fail closed via
    _guarded_execute_unknown_tool. For a registered tool, this runs the
    same pipeline guarded_delete_users always ran: intent extraction,
    authoritative preview, risk extraction, deterministic policy, proof
    validation, workflow budget, mutation (only after ALLOW), postcondition
    verification, and exactly one audit event.
    """
    event_id = new_event_id()

    spec = get_tool_spec(tool_name)
    if spec is None:
        return _guarded_execute_unknown_tool(
            event_id, tool_name, action_context, arguments, rollback_proof
        )

    # Only the mutation-selecting arguments this tool declares -- never
    # rollback_proof, action_context, or any policy/risk/workflow/audit
    # metadata. Selector hashing itself still happens only inside the
    # registered preview/mutation functions, via the one shared
    # proofgate.selector implementation.
    selector_arguments = {
        name: arguments[name] for name in spec.selector_argument_names if name in arguments
    }

    intent_outcome = extract_intent_live_or_fallback(action_context.original_instruction)
    intent = intent_outcome.value

    try:
        impact = spec.preview_fn(**arguments)
    except Exception:
        impact = None

    proof_validation = None
    if impact is not None:
        proof_validation = validate_rollback_proof(
            rollback_proof, resource=spec.resource, impact=impact
        )
        rollback_proof_valid = proof_validation.valid
    else:
        rollback_proof_valid = False

    risk_outcome = (
        extract_risk_features_live_or_fallback(
            action_context.original_instruction, intent, selector_arguments, impact
        )
        if impact is not None
        else None
    )
    risk = risk_outcome.value if risk_outcome is not None else None

    workflow_budget = get_workflow_budget(action_context.workflow_id)
    budget_before = workflow_budget.model_copy()

    triggered_rules, risk_factors = evaluate_policy(
        intent=intent,
        proposed_arguments=selector_arguments,
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
    suggested_repairs = build_suggested_repairs(
        intent=intent, selector_arguments=selector_arguments, tool=tool_name, hard_delete=spec.hard_delete
    )

    mutation_result = None
    postcondition_result = None
    executed = False
    if verdict == "ALLOW":
        mutation_result = spec.mutation_fn(**arguments)
        postcondition_result = verify_postcondition(impact, mutation_result)
        # Slice 23: resolve MISMATCH into ROLLED_BACK or
        # MANUAL_REVIEW_REQUIRED via one bounded, trusted automatic
        # restoration attempt, before budget/audit ever see the result.
        # A no-op for VERIFIED and for the pre-existing production-impact
        # MANUAL_REVIEW_REQUIRED case.
        postcondition_result = resolve_final_postcondition(
            spec=spec,
            rollback_proof=rollback_proof,
            rollback_proof_valid=rollback_proof_valid,
            selector_arguments=selector_arguments,
            postcondition_result=postcondition_result,
        )
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
        tool_name=tool_name,
        tool_arguments=arguments,
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


def _guarded_execute_unknown_tool(
    event_id: str,
    tool_name: str,
    action_context: ActionContext,
    arguments: dict,
    rollback_proof: RollbackProof | None,
) -> EnforcementResult:
    """Fail-closed path for an unregistered tool_name.

    No Operations preview, no risk extraction, no mutation, no snapshot,
    no workflow-budget consumption. suggested_repairs is deliberately
    always [] here (Slice 24): a genuinely unregistered tool_name has no
    known preview/mutation function, snapshot support, or resource
    binding to retry against -- there is nothing safe to suggest, no
    matter what tool name would label it.
    """
    intent_outcome = extract_intent_live_or_fallback(action_context.original_instruction)
    intent = intent_outcome.value

    workflow_budget = get_workflow_budget(action_context.workflow_id)
    budget_snapshot = workflow_budget.model_copy()

    triggered_rules = [
        TriggeredRule(
            rule_id=RULE_UNKNOWN_IMPACT,
            explanation=f"Tool {tool_name!r} is not registered; impact cannot be measured.",
        )
    ]
    risk_score = 10.0
    missing_requirements = build_missing_requirements(
        intent=intent, rollback_proof_valid=False, impact=None, workflow_budget=workflow_budget
    )

    extraction_mode = "mixed" if intent_outcome.mode == "nebius_live" else "deterministic_fallback"
    craft_evidence = get_craft_evidence(action_context.workflow_id)

    # Same "impact unknown" convention as the registered-tool path: no
    # preview ever ran, so no proof validation had an ImpactEnvelope to
    # check against, regardless of whether a proof was supplied.
    proof_status = "MISSING" if rollback_proof is None else "INVALID"

    audit_event = AuditEvent(
        schema_version=SCHEMA_VERSION,
        event_id=event_id,
        workflow_id=action_context.workflow_id,
        timestamp=current_timestamp(),
        original_instruction=action_context.original_instruction,
        requesting_user=action_context.requesting_user,
        agent_id=action_context.agent_id,
        tool_name=tool_name,
        tool_arguments=arguments,
        intent_constraints=intent,
        impact_envelope=None,
        risk_features=None,
        risk_score=risk_score,
        risk_factors=[],
        triggered_rules=triggered_rules,
        policy_version=POLICY_VERSION,
        extraction_mode=extraction_mode,
        nebius_model=intent_outcome.model,
        craft_evidence=craft_evidence,
        verdict="BLOCK",
        missing_requirements=missing_requirements,
        suggested_repairs=[],
        proof_status=proof_status,
        proof_checks=None,
        execution_status="NOT_EXECUTED",
        mutation_result=None,
        postcondition_result=None,
        workflow_budget_before=budget_snapshot,
        workflow_budget_after=budget_snapshot,
    )
    append_audit_event(audit_event)

    return EnforcementResult(
        verdict="BLOCK",
        risk_score=risk_score,
        risk_factors=[],
        triggered_rules=triggered_rules,
        missing_requirements=missing_requirements,
        suggested_repairs=[],
        executed=False,
        audit_event_id=event_id,
    )


def guarded_delete_users(
    action_context: ActionContext,
    inactive_days: int,
    environment: str | None,
    rollback_proof: RollbackProof | None,
) -> EnforcementResult:
    """Thin compatibility wrapper around guarded_execute("delete_users",
    ...). Kept so existing callers (app.py, existing tests) never need to
    change; behaviorally identical to guarded_execute for the same inputs.
    """
    return guarded_execute(
        tool_name="delete_users",
        action_context=action_context,
        arguments={"inactive_days": inactive_days, "environment": environment},
        rollback_proof=rollback_proof,
    )
