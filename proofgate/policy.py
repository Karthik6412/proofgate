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
    inactive_days: int,
) -> list[dict]:
    if intent.environment is None:
        return []

    return [
        {
            "tool": "delete_users",
            "arguments": {
                "inactive_days": intent.inactivity_days or inactive_days,
                "environment": intent.environment,
            },
            "next_step": "create_snapshot",
        }
    ]
