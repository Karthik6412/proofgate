"""Trusted Operations binding: registers the Operations tools into the
generic proofgate.registry and owns what Operations policy means.

operations/ is the domain; this module is the adapter between it and the
generic ProofGate machinery. The registry never imports this module --
each runtime entrypoint (tests/conftest.py, proofgate/mcp_server.py,
app.py) calls register_operations_tools() explicitly. Importing this
module registers nothing by itself.

Operations policy is OperationsPolicyContext plus OPERATIONS_RULESET,
evaluated by the generic proofgate.policy.evaluate_policy. Every spec
carries the same module-level evaluate_operations_policy as its
policy_fn, so proofgate.core reaches these rules without importing this
module.
"""

from dataclasses import dataclass

import operations.actions as operations_actions
import operations.feature_flags as operations_feature_flags
from proofgate.models import (
    ImpactEnvelope,
    IntentConstraints,
    MutationResult,
    RiskFeatures,
    WorkflowBudget,
)
from proofgate.policy import (
    RULE_INTENT_BOUNDARY,
    RULE_RECOVERY_PROOF,
    RULE_WORKFLOW_BUDGET,
    PolicyDecision,
    RuleOutcome,
    RuleSet,
    evaluate_policy,
)
from proofgate.registry import GuardedToolSpec, register_tool

MAX_MUTATED_ROWS_PER_WORKFLOW = 100


@dataclass(frozen=True)
class OperationsPolicyContext:
    """Everything the Operations rules read. Only ever built from a known
    ImpactEnvelope; unknown impact fails closed before a context exists."""

    intent: IntentConstraints
    impact: ImpactEnvelope
    risk: RiskFeatures | None
    rollback_proof_valid: bool
    workflow_budget: WorkflowBudget

    @property
    def production_impact(self) -> bool:
        return self.impact.environment_counts.get("production", 0) > 0


@dataclass(frozen=True)
class IntentBoundaryRule:
    rule_id: str = RULE_INTENT_BOUNDARY

    def evaluate(self, context: OperationsPolicyContext, /) -> RuleOutcome:
        if context.intent.environment == "test" and context.production_impact:
            return RuleOutcome.failed(
                self.rule_id,
                "The user requested test scope, but production rows are included.",
                f"environment={context.intent.environment}",
            )
        return RuleOutcome.passed(self.rule_id)


@dataclass(frozen=True)
class RecoveryProofRule:
    rule_id: str = RULE_RECOVERY_PROOF

    def evaluate(self, context: OperationsPolicyContext, /) -> RuleOutcome:
        if context.impact.hard_delete and not context.rollback_proof_valid:
            return RuleOutcome.failed(
                self.rule_id,
                "The irreversible action has no valid rollback proof.",
                "valid rollback proof",
            )
        return RuleOutcome.passed(self.rule_id)


@dataclass(frozen=True)
class WorkflowBudgetRule:
    """Operations budget unit: affected rows across the workflow."""

    rule_id: str = RULE_WORKFLOW_BUDGET

    def evaluate(self, context: OperationsPolicyContext, /) -> RuleOutcome:
        projected = context.workflow_budget.rows_mutated + context.impact.estimated_count
        if projected > MAX_MUTATED_ROWS_PER_WORKFLOW:
            return RuleOutcome.failed(
                self.rule_id,
                (
                    f"{context.impact.estimated_count} projected mutations exceed the "
                    f"{MAX_MUTATED_ROWS_PER_WORKFLOW}-row workflow budget."
                ),
                f"workflow impact within {MAX_MUTATED_ROWS_PER_WORKFLOW} rows",
            )
        return RuleOutcome.passed(self.rule_id)


def operations_risk_factors(context: OperationsPolicyContext) -> list[str]:
    """Explanatory only; never affects the verdict."""
    risk_factors: list[str] = []
    if context.risk is not None:
        risk_factors.append(f"{context.risk.scope_class} scope")
        if context.production_impact:
            risk_factors.append("production impact")
        if context.risk.intent_mismatch:
            risk_factors.append("intent mismatch")
        if context.risk.irreversible:
            risk_factors.append("irreversible action")
        if context.impact.hard_delete and not context.rollback_proof_valid:
            risk_factors.append("missing proof")
    return risk_factors


OPERATIONS_RULESET: RuleSet[OperationsPolicyContext] = RuleSet(
    rules=(IntentBoundaryRule(), RecoveryProofRule(), WorkflowBudgetRule()),
    risk_factors_fn=operations_risk_factors,
)


def evaluate_operations_policy(
    *,
    intent: IntentConstraints,
    impact: ImpactEnvelope,
    risk: RiskFeatures | None,
    rollback_proof_valid: bool,
    workflow_budget: WorkflowBudget,
) -> PolicyDecision:
    """The policy_fn every Operations spec carries: builds the Operations
    context from guarded_execute's runtime facts and evaluates
    OPERATIONS_RULESET with the generic engine."""
    context = OperationsPolicyContext(
        intent=intent,
        impact=impact,
        risk=risk,
        rollback_proof_valid=rollback_proof_valid,
        workflow_budget=workflow_budget,
    )
    return evaluate_policy(context, OPERATIONS_RULESET)


def _delete_users_preview(**kwargs) -> ImpactEnvelope:
    """Thin indirection that resolves operations.actions.preview_delete_users
    by module-attribute lookup at call time (not a frozen reference bound
    at registry-construction time), so tests can still patch
    operations.actions.preview_delete_users directly -- the real, dumb
    Operations function this ultimately calls."""
    return operations_actions.preview_delete_users(**kwargs)


def _delete_users_mutation(**kwargs) -> MutationResult:
    """Same module-attribute indirection as _delete_users_preview, for
    operations.actions.delete_users."""
    return operations_actions.delete_users(**kwargs)


def _deactivate_users_preview(**kwargs) -> ImpactEnvelope:
    """Call-time module-attribute lookup, same pattern as
    _delete_users_preview, so operations.actions.preview_deactivate_users
    stays patchable in tests."""
    return operations_actions.preview_deactivate_users(**kwargs)


def _deactivate_users_mutation(**kwargs) -> MutationResult:
    """Call-time module-attribute lookup, same pattern as
    _delete_users_mutation, for operations.actions.deactivate_users."""
    return operations_actions.deactivate_users(**kwargs)


def _set_feature_flag_preview(**kwargs) -> ImpactEnvelope:
    """Call-time module-attribute lookup, same pattern as
    _delete_users_preview, so operations.feature_flags.
    preview_set_feature_flag stays patchable in tests. A genuinely
    different resource (filesystem-backed feature-flag JSON, not the
    users database) reached through the exact same registry-dispatch
    mechanism as the two users-table tools."""
    return operations_feature_flags.preview_set_feature_flag(**kwargs)


def _set_feature_flag_mutation(**kwargs) -> MutationResult:
    """Call-time module-attribute lookup, same pattern as
    _delete_users_mutation, for operations.feature_flags.set_feature_flag."""
    return operations_feature_flags.set_feature_flag(**kwargs)


OPERATIONS_TOOL_SPECS: tuple[GuardedToolSpec, ...] = (
    GuardedToolSpec(
        tool_name="delete_users",
        preview_fn=_delete_users_preview,
        mutation_fn=_delete_users_mutation,
        selector_argument_names=("inactive_days", "environment"),
        resource="users",
        hard_delete=True,
        reversibility="irreversible_without_snapshot",
        policy_fn=evaluate_operations_policy,
    ),
    GuardedToolSpec(
        tool_name="deactivate_users",
        preview_fn=_deactivate_users_preview,
        mutation_fn=_deactivate_users_mutation,
        selector_argument_names=("inactive_days", "environment"),
        resource="users",
        hard_delete=False,
        reversibility="reversible",
        policy_fn=evaluate_operations_policy,
    ),
    GuardedToolSpec(
        tool_name="set_feature_flag",
        preview_fn=_set_feature_flag_preview,
        mutation_fn=_set_feature_flag_mutation,
        selector_argument_names=("flag_name", "enabled", "environment", "rollout_percentage"),
        resource="feature_flags",
        hard_delete=False,
        reversibility="reversible",
        policy_fn=evaluate_operations_policy,
    ),
)


def register_operations_tools() -> None:
    """Register the three Operations tools. Idempotent: the specs above are
    built once per process from module-level functions, so a repeat call
    (Streamlit rerun, a second entrypoint in the same process) re-registers
    equal specs and is a no-op."""
    for spec in OPERATIONS_TOOL_SPECS:
        register_tool(spec)
