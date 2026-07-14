"""Deterministic ProofGate policy engine.

Nebius (agent.nebius_client) never returns a verdict. This module is the
sole authority for ALLOW/BLOCK: any triggered rule blocks. Risk score is
explanatory only and never overrides the triggered-rule verdict.
"""

from proofgate.models import (
    ImpactEnvelope,
    IntentConstraints,
    RiskFeatures,
    TriggeredRule,
    WorkflowBudget,
)

MAX_MUTATED_ROWS_PER_WORKFLOW = 100
POLICY_VERSION = "v1"

RULE_INTENT_BOUNDARY = "RULE_INTENT_BOUNDARY"
RULE_RECOVERY_PROOF = "RULE_RECOVERY_PROOF"
RULE_UNKNOWN_IMPACT = "RULE_UNKNOWN_IMPACT"
RULE_WORKFLOW_BUDGET = "RULE_WORKFLOW_BUDGET"


def evaluate_policy(
    intent: IntentConstraints,
    proposed_arguments: dict,
    impact: ImpactEnvelope | None,
    risk: RiskFeatures | None,
    rollback_proof_valid: bool,
    workflow_budget: WorkflowBudget,
) -> tuple[list[TriggeredRule], list[str]]:
    """Returns (triggered_rules, risk_factors). Fails closed on unknown impact."""
    if impact is None:
        return (
            [
                TriggeredRule(
                    rule_id=RULE_UNKNOWN_IMPACT,
                    explanation="Impact preview failed or is unknown for a consequential action.",
                )
            ],
            [],
        )

    triggered: list[TriggeredRule] = []
    production_impact = impact.environment_counts.get("production", 0) > 0
    intent_requires_test = intent.environment == "test"

    if intent_requires_test and production_impact:
        triggered.append(
            TriggeredRule(
                rule_id=RULE_INTENT_BOUNDARY,
                explanation="The user requested test scope, but production rows are included.",
            )
        )

    if impact.hard_delete and not rollback_proof_valid:
        triggered.append(
            TriggeredRule(
                rule_id=RULE_RECOVERY_PROOF,
                explanation="The irreversible action has no valid rollback proof.",
            )
        )

    projected = workflow_budget.rows_mutated + impact.estimated_count
    if projected > MAX_MUTATED_ROWS_PER_WORKFLOW:
        triggered.append(
            TriggeredRule(
                rule_id=RULE_WORKFLOW_BUDGET,
                explanation=(
                    f"{impact.estimated_count} projected mutations exceed the "
                    f"{MAX_MUTATED_ROWS_PER_WORKFLOW}-row workflow budget."
                ),
            )
        )

    risk_factors: list[str] = []
    if risk is not None:
        risk_factors.append(f"{risk.scope_class} scope")
        if production_impact:
            risk_factors.append("production impact")
        if risk.intent_mismatch:
            risk_factors.append("intent mismatch")
        if risk.irreversible:
            risk_factors.append("irreversible action")
        if impact.hard_delete and not rollback_proof_valid:
            risk_factors.append("missing proof")

    return triggered, risk_factors


def compute_risk_score(
    impact: ImpactEnvelope,
    risk: RiskFeatures,
    proof_required_and_missing: bool,
) -> float:
    """Explanatory only; triggered rules in evaluate_policy decide the verdict."""
    count = impact.estimated_count
    if count <= 10:
        score = 0.5
    elif count <= 100:
        score = 1.0
    elif count <= 1000:
        score = 2.0
    else:
        score = 3.0

    if risk.production_impact:
        score += 2.0
    if risk.intent_mismatch:
        score += 2.0
    if risk.irreversible:
        score += 1.5
    if proof_required_and_missing:
        score += 1.0

    score += risk.uncertainty

    return min(score, 10.0)


def build_missing_requirements(
    intent: IntentConstraints,
    rollback_proof_valid: bool,
    impact: ImpactEnvelope | None,
    workflow_budget: WorkflowBudget,
) -> list[str]:
    if impact is None:
        return ["known operational impact"]

    missing: list[str] = []
    production_impact = impact.environment_counts.get("production", 0) > 0

    if intent.environment == "test" and production_impact:
        missing.append(f"environment={intent.environment}")

    if impact.hard_delete and not rollback_proof_valid:
        missing.append("valid rollback proof")

    projected = workflow_budget.rows_mutated + impact.estimated_count
    if projected > MAX_MUTATED_ROWS_PER_WORKFLOW:
        missing.append(f"workflow impact within {MAX_MUTATED_ROWS_PER_WORKFLOW} rows")

    return missing


def build_suggested_repairs(
    intent: IntentConstraints,
    selector_arguments: dict,
    tool: str,
    hard_delete: bool,
) -> list[dict]:
    """tool is the real tool identity the caller already evaluated -- it
    is only ever placed directly into the returned suggestion, never
    compared or branched on, so this remains free of any special casing
    for a particular tool identity.

    selector_arguments is the same generic selector dict the caller
    already built from this tool's own registered selector_argument_
    names (proofgate.core.guarded_execute's own selector_arguments) --
    every submitted argument is preserved unchanged except environment,
    which is corrected to the intent's own resolved value. This is what
    lets a tool with a completely different argument shape (e.g.
    flag_name/enabled/rollout_percentage) receive an honest, correctly
    shaped repair suggestion without this function needing to know
    anything tool-specific.

    hard_delete is the same generic registry metadata already available
    to the caller (never a tool-name check): a hard-delete action's real
    next step is creating a recovery snapshot before retrying; a
    reversible action never needs one, and suggesting one would be
    actively misleading.
    """
    if intent.environment is None:
        return []

    corrected_arguments = dict(selector_arguments)
    corrected_arguments["environment"] = intent.environment
    if "inactive_days" in corrected_arguments and intent.inactivity_days is not None:
        corrected_arguments["inactive_days"] = intent.inactivity_days

    return [
        {
            "tool": tool,
            "arguments": corrected_arguments,
            "next_step": "create_snapshot" if hard_delete else "retry_with_corrected_environment",
        }
    ]
