"""Slice 1 / P3: the generic policy engine in proofgate/policy.py.

Two kinds of test:

1. A test-local toy domain (ToyContext, ThresholdRule, LabelRule) runs
   through the same evaluate_policy the Operations binding uses, proving
   the engine evaluates a non-Operations context with no engine change.
2. An AST boundary check over the generic-engine definitions only.
   policy.py deliberately still holds two Operations-shaped compatibility
   helpers (compute_risk_score, build_suggested_repairs) and the rule-ID
   constants, so the check isolates the definitions we claim are generic
   rather than pretending the whole module is domain-pure.
"""

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

import proofgate.policy as policy_module
from proofgate.policy import (
    RULE_UNKNOWN_IMPACT,
    PolicyDecision,
    RuleOutcome,
    RuleSet,
    evaluate_policy,
    unknown_impact_decision,
)

# ---------------------------------------------------------------------------
# Toy domain: nothing here is an Operations type or concept
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToyContext:
    temperature: int
    label: str


@dataclass(frozen=True)
class ThresholdRule:
    rule_id: str
    limit: int
    requirement: str | None

    def evaluate(self, context: ToyContext, /) -> RuleOutcome:
        if context.temperature > self.limit:
            return RuleOutcome.failed(
                self.rule_id, f"{context.temperature} exceeds {self.limit}.", self.requirement
            )
        return RuleOutcome.passed(self.rule_id)


@dataclass(frozen=True)
class LabelRule:
    rule_id: str = "TOY_LABEL"

    def evaluate(self, context: ToyContext, /) -> RuleOutcome:
        if context.label != "approved":
            return RuleOutcome.failed(self.rule_id, "Label is not approved.", "label=approved")
        return RuleOutcome.passed(self.rule_id)


def _toy_risk_factors(context: ToyContext) -> list[str]:
    return [f"label {context.label}"]


# Declared high-limit first on purpose: output order must follow declaration,
# not rule_id or limit.
TOY_RULESET: RuleSet[ToyContext] = RuleSet(
    rules=(
        ThresholdRule("TOY_HIGH", limit=20, requirement="temperature <= 20"),
        LabelRule(),
        ThresholdRule("TOY_LOW", limit=10, requirement=None),
    ),
    risk_factors_fn=_toy_risk_factors,
)


def test_toy_all_pass_is_allow():
    decision = evaluate_policy(ToyContext(temperature=5, label="approved"), TOY_RULESET)

    assert decision.verdict == "ALLOW"
    assert decision.triggered_rules == []
    assert decision.missing_requirements == []
    assert [outcome.rule_id for outcome in decision.outcomes] == ["TOY_HIGH", "TOY_LABEL", "TOY_LOW"]
    assert not any(outcome.triggered for outcome in decision.outcomes)


def test_toy_single_failure_is_block():
    decision = evaluate_policy(ToyContext(temperature=15, label="approved"), TOY_RULESET)

    assert decision.verdict == "BLOCK"
    assert [rule.rule_id for rule in decision.triggered_rules] == ["TOY_LOW"]
    assert decision.triggered_rules[0].explanation == "15 exceeds 10."


def test_toy_failures_stay_in_declared_order():
    decision = evaluate_policy(ToyContext(temperature=30, label="draft"), TOY_RULESET)

    assert [rule.rule_id for rule in decision.triggered_rules] == ["TOY_HIGH", "TOY_LABEL", "TOY_LOW"]


def test_toy_missing_requirements_come_from_the_same_outcomes():
    decision = evaluate_policy(ToyContext(temperature=30, label="draft"), TOY_RULESET)

    # TOY_LOW fires but declares no requirement, so it contributes nothing.
    assert decision.missing_requirements == ["temperature <= 20", "label=approved"]
    assert decision.missing_requirements == [
        outcome.missing_requirement
        for outcome in decision.outcomes
        if outcome.triggered and outcome.missing_requirement is not None
    ]
    assert [rule.rule_id for rule in decision.triggered_rules] == [
        outcome.triggered_rule.rule_id for outcome in decision.outcomes if outcome.triggered
    ]


def test_toy_risk_factors_are_carried_but_never_decide_the_verdict():
    allow = evaluate_policy(ToyContext(temperature=5, label="approved"), TOY_RULESET)
    block = evaluate_policy(ToyContext(temperature=30, label="draft"), TOY_RULESET)

    assert allow.verdict == "ALLOW" and allow.risk_factors == ("label approved",)
    assert block.verdict == "BLOCK" and block.risk_factors == ("label draft",)


def test_ruleset_without_risk_factors_fn_yields_none():
    ruleset: RuleSet[ToyContext] = RuleSet(rules=(LabelRule(),))
    decision = evaluate_policy(ToyContext(temperature=0, label="approved"), ruleset)

    assert decision.risk_factors == ()
    assert decision.verdict == "ALLOW"


def test_empty_ruleset_is_allow():
    decision = evaluate_policy(ToyContext(temperature=99, label="draft"), RuleSet(rules=()))

    assert decision.verdict == "ALLOW"
    assert decision.outcomes == ()


def test_evaluation_is_deterministic():
    context = ToyContext(temperature=30, label="draft")

    assert evaluate_policy(context, TOY_RULESET) == evaluate_policy(context, TOY_RULESET)


def test_rule_returning_another_rules_outcome_is_rejected():
    @dataclass(frozen=True)
    class MislabeledRule:
        rule_id: str = "TOY_SELF"

        def evaluate(self, context: ToyContext, /) -> RuleOutcome:
            return RuleOutcome.failed("TOY_OTHER", "wrong id")

    with pytest.raises(ValueError, match="TOY_SELF"):
        evaluate_policy(ToyContext(temperature=0, label="approved"), RuleSet(rules=(MislabeledRule(),)))


@dataclass(frozen=True)
class DetailedThresholdRule:
    """Toy rule that attaches its own toy-defined detail keys."""

    rule_id: str = "TOY_DETAILED"
    limit: int = 10

    def evaluate(self, context: ToyContext, /) -> RuleOutcome:
        details = {"limit": self.limit, "observed": context.temperature}
        if context.temperature > self.limit:
            return RuleOutcome.failed(self.rule_id, "Too hot.", "cooler", details=details)
        return RuleOutcome.passed(self.rule_id, details=details)


def test_details_are_carried_unchanged_on_passing_and_failing_outcomes():
    ruleset: RuleSet[ToyContext] = RuleSet(rules=(DetailedThresholdRule(),))

    passing = evaluate_policy(ToyContext(temperature=5, label="approved"), ruleset)
    failing = evaluate_policy(ToyContext(temperature=15, label="approved"), ruleset)

    assert passing.outcomes[0].details == {"limit": 10, "observed": 5}
    assert failing.outcomes[0].details == {"limit": 10, "observed": 15}


def test_details_never_change_the_derived_outputs():
    with_details = evaluate_policy(
        ToyContext(temperature=15, label="approved"), RuleSet(rules=(DetailedThresholdRule(),))
    )
    without_details = PolicyDecision(outcomes=(RuleOutcome.failed("TOY_DETAILED", "Too hot.", "cooler"),))

    assert with_details.verdict == without_details.verdict == "BLOCK"
    assert with_details.triggered_rules == without_details.triggered_rules
    assert with_details.missing_requirements == without_details.missing_requirements == ["cooler"]


def test_details_default_to_none():
    assert RuleOutcome.passed("TOY").details is None
    assert RuleOutcome.failed("TOY", "x").details is None


def test_triggered_rule_schema_does_not_gain_details():
    outcome = RuleOutcome.failed("TOY", "x", details={"k": "v"})

    assert outcome.triggered_rule.model_dump() == {"rule_id": "TOY", "explanation": "x"}


def test_unknown_impact_decision_fails_closed_with_only_that_rule():
    decision = unknown_impact_decision("Impact is unknown.", "known impact")

    assert isinstance(decision, PolicyDecision)
    assert decision.verdict == "BLOCK"
    assert [(rule.rule_id, rule.explanation) for rule in decision.triggered_rules] == [
        (RULE_UNKNOWN_IMPACT, "Impact is unknown.")
    ]
    assert decision.missing_requirements == ["known impact"]
    assert decision.risk_factors == ()


# ---------------------------------------------------------------------------
# Structural boundary of the generic engine
# ---------------------------------------------------------------------------

GENERIC_ENGINE_DEFINITIONS = {
    "RuleOutcome",
    "Rule",
    "RuleSet",
    "PolicyDecision",
    "evaluate_policy",
    "unknown_impact_decision",
    "_no_risk_factors",
}
# Acknowledged Operations-shaped compatibility debt, deliberately outside
# the generic engine for this phase.
OPERATIONS_COMPATIBILITY_DEFINITIONS = {"compute_risk_score", "build_suggested_repairs"}

OPERATIONS_TYPES = {
    "IntentConstraints",
    "ImpactEnvelope",
    "RiskFeatures",
    "WorkflowBudget",
    "MutationResult",
    "RollbackProof",
    "ActionContext",
    "GuardedToolSpec",
    "OperationsPolicyContext",
    "OPERATIONS_RULESET",
}
OPERATIONS_FIELDS = {
    "environment",
    "inactivity_days",
    "inactive_days",
    "environment_counts",
    "production",
    "hard_delete",
    "rows_mutated",
    "estimated_count",
    "rollback_proof_valid",
    "tool_name",
}
FORBIDDEN_IMPORT_PREFIXES = ("operations", "craft", "agent", "legal", "proofgate.bindings", "proofgate.core")


def _top_level_definitions(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _coupling_violations(definition: ast.AST) -> list[str]:
    """Every Operations type, Operations field name, domain import or
    string-literal comparison referenced anywhere inside one definition,
    including annotations and string annotations."""
    forbidden = OPERATIONS_TYPES | OPERATIONS_FIELDS
    violations = []
    for node in ast.walk(definition):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            names.append(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.extend(token for token in forbidden if token in node.value)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            violations.extend(f"import {module}" for module in modules if module.startswith(FORBIDDEN_IMPORT_PREFIXES))
            continue
        elif isinstance(node, ast.Compare):
            for comparator in [node.left, *node.comparators]:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    violations.append(f"string comparison {comparator.value!r}")
        violations.extend(name for name in names if name in forbidden)
    return violations


def _policy_tree() -> ast.Module:
    return ast.parse(Path(policy_module.__file__).read_text())


def test_every_policy_definition_is_classified():
    """A new top-level definition must be consciously placed in the generic
    engine (and pass the boundary check) or in the named compatibility debt."""
    assert set(_top_level_definitions(_policy_tree())) == (
        GENERIC_ENGINE_DEFINITIONS | OPERATIONS_COMPATIBILITY_DEFINITIONS
    )


@pytest.mark.parametrize("name", sorted(GENERIC_ENGINE_DEFINITIONS))
def test_generic_engine_definition_has_no_operations_coupling(name):
    definition = _top_level_definitions(_policy_tree())[name]
    assert _coupling_violations(definition) == []


def test_policy_module_imports_no_domain_or_binding():
    imported = []
    for node in ast.walk(_policy_tree()):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not [module for module in imported if module.startswith(FORBIDDEN_IMPORT_PREFIXES)]


def test_policy_module_exposes_exactly_the_four_rule_ids():
    assert {name for name in dir(policy_module) if name.startswith("RULE_")} == {
        "RULE_INTENT_BOUNDARY",
        "RULE_RECOVERY_PROOF",
        "RULE_UNKNOWN_IMPACT",
        "RULE_WORKFLOW_BUDGET",
    }


def test_engine_never_reads_outcome_details():
    """details belongs to the domain: no generic definition other than
    RuleOutcome itself (which declares and constructs it) may read it."""
    definitions = _top_level_definitions(_policy_tree())
    for name in GENERIC_ENGINE_DEFINITIONS - {"RuleOutcome"}:
        attributes = {node.attr for node in ast.walk(definitions[name]) if isinstance(node, ast.Attribute)}
        assert "details" not in attributes, name


def test_operations_budget_constant_is_not_in_generic_policy():
    assert not hasattr(policy_module, "MAX_MUTATED_ROWS_PER_WORKFLOW")
    assert not hasattr(policy_module, "build_missing_requirements")


@pytest.mark.parametrize(
    "leak",
    [
        "    if context.impact.hard_delete:\n        pass\n",
        "    budget: WorkflowBudget = context\n",
        "    if context.domain == 'operations':\n        pass\n",
        "    import operations.actions\n",
        "    x = context.environment_counts.get('production', 0)\n",
    ],
    ids=["field", "type", "domain_branch", "domain_import", "production_key"],
)
def test_boundary_check_detects_a_leak(leak):
    """Positive control: the checker actually flags each kind of leak."""
    source = f"def evaluate_policy(context, ruleset):\n{leak}"
    definition = ast.parse(source).body[0]
    assert _coupling_violations(definition) != []
