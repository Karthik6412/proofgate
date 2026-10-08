"""Deterministic ProofGate policy engine.

Nebius (agent.nebius_client) never returns a verdict. The generic engine
below (RuleOutcome, Rule, RuleSet, PolicyDecision, evaluate_policy,
unknown_impact_decision) is the sole authority for ALLOW/BLOCK: a domain
supplies its own context type and a RuleSet over it, the rules run in
declared order, and any triggered rule blocks. The engine never knows a
concrete context -- the Operations context and rules live in
proofgate/bindings/operations.py, which this module never imports.

compute_risk_score and build_suggested_repairs are not part of the
generic engine. They are Operations-shaped helpers kept here for
compatibility; both are explanatory only and never affect the verdict.
"""

from dataclasses import dataclass
from typing import Callable, Generic, Literal, Mapping, Protocol, TypeVar

from proofgate.models import (
    ImpactEnvelope,
    IntentConstraints,
    RiskFeatures,
    TriggeredRule,
)

POLICY_VERSION = "v1"

RULE_INTENT_BOUNDARY = "RULE_INTENT_BOUNDARY"
RULE_RECOVERY_PROOF = "RULE_RECOVERY_PROOF"
RULE_UNKNOWN_IMPACT = "RULE_UNKNOWN_IMPACT"
RULE_WORKFLOW_BUDGET = "RULE_WORKFLOW_BUDGET"

ContextT = TypeVar("ContextT")
ContextT_contra = TypeVar("ContextT_contra", contravariant=True)


@dataclass(frozen=True)
class RuleOutcome:
    """The result of evaluating one rule once.

    A triggered outcome carries everything derived from that single
    evaluation -- its TriggeredRule and, when the domain defines one, the
    requirement whose absence made it fire -- so triggered rules and
    missing requirements can never come from separate conditions.

    details is optional machine-readable evidence from the same
    evaluation, on a passing or a failing outcome. Its keys and meaning
    belong entirely to the domain that defines the rule; the engine never
    reads, validates or interprets it, and it never affects the verdict.
    """

    rule_id: str
    triggered_rule: TriggeredRule | None = None
    missing_requirement: str | None = None
    details: Mapping[str, object] | None = None

    @property
    def triggered(self) -> bool:
        return self.triggered_rule is not None

    @classmethod
    def passed(cls, rule_id: str, details: Mapping[str, object] | None = None) -> "RuleOutcome":
        return cls(rule_id=rule_id, details=details)

    @classmethod
    def failed(
        cls,
        rule_id: str,
        explanation: str,
        missing_requirement: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> "RuleOutcome":
        return cls(
            rule_id=rule_id,
            triggered_rule=TriggeredRule(rule_id=rule_id, explanation=explanation),
            missing_requirement=missing_requirement,
            details=details,
        )


class Rule(Protocol[ContextT_contra]):
    rule_id: str

    def evaluate(self, context: ContextT_contra, /) -> RuleOutcome: ...


def _no_risk_factors(context: object) -> list[str]:
    return []


@dataclass(frozen=True)
class RuleSet(Generic[ContextT]):
    """An ordered set of rules over one domain context type.

    risk_factors_fn supplies the domain's explanatory risk factors for
    the same context. They are carried into the PolicyDecision for
    display and audit only; they never affect the verdict.
    """

    rules: tuple[Rule[ContextT], ...]
    risk_factors_fn: Callable[[ContextT], list[str]] = _no_risk_factors


@dataclass(frozen=True)
class PolicyDecision:
    """Every output is derived from the ordered outcomes, so the verdict,
    triggered rules and missing requirements cannot disagree."""

    outcomes: tuple[RuleOutcome, ...]
    risk_factors: tuple[str, ...] = ()

    @property
    def triggered_rules(self) -> list[TriggeredRule]:
        return [outcome.triggered_rule for outcome in self.outcomes if outcome.triggered_rule is not None]

    @property
    def missing_requirements(self) -> list[str]:
        return [
            outcome.missing_requirement
            for outcome in self.outcomes
            if outcome.triggered and outcome.missing_requirement is not None
        ]

    @property
    def verdict(self) -> Literal["ALLOW", "BLOCK"]:
        return "BLOCK" if any(outcome.triggered for outcome in self.outcomes) else "ALLOW"


def evaluate_policy(context: ContextT, ruleset: RuleSet[ContextT]) -> PolicyDecision:
    """Run every rule in declared order against an evaluable context.

    Deterministic and side-effect free: no I/O, no model calls, no
    mutation. The context must already be complete -- unknown impact is
    decided before a context exists, via unknown_impact_decision.
    """
    outcomes = []
    for rule in ruleset.rules:
        outcome = rule.evaluate(context)
        if outcome.rule_id != rule.rule_id:
            raise ValueError(f"Rule {rule.rule_id!r} returned an outcome for {outcome.rule_id!r}.")
        outcomes.append(outcome)
    return PolicyDecision(
        outcomes=tuple(outcomes),
        risk_factors=tuple(ruleset.risk_factors_fn(context)),
    )


def unknown_impact_decision(explanation: str, missing_requirement: str) -> PolicyDecision:
    """Fail closed when consequential impact is unknown: RULE_UNKNOWN_IMPACT
    alone, no domain rule evaluated, no risk factors."""
    return PolicyDecision(
        outcomes=(RuleOutcome.failed(RULE_UNKNOWN_IMPACT, explanation, missing_requirement),),
    )


def compute_risk_score(
    impact: ImpactEnvelope,
    risk: RiskFeatures,
    proof_required_and_missing: bool,
) -> float:
    """Explanatory only; the triggered rules decide the verdict."""
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
