"""Friday v1 Slice L3: the four Legal Action Boundary rules through the
generic policy engine.

Rules compare the resolved effect's consequential resources against the
AuthorityGrant. Reads never count, the effect is never edited, coarse
permissions never widen authority, and the generic engine stays
domain-neutral.
"""

import ast
import datetime
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

import proofgate.bindings.legal.rules as rules_module
import proofgate.policy as policy_module
from legal.models import AuthorityGrant
from legal.permissions import SEEDED_USER_ID, get_permission_snapshot
from proofgate.bindings.legal.plan import CANONICAL_PLAN, CLEAN_PLAN, LegalPlan, PlanStep
from proofgate.bindings.legal.resolver import (
    LegalEffectDimensions,
    LegalResolvedEffect,
    RequestedScope,
    ResourceFacts,
    resolve_plan,
)
from proofgate.bindings.legal.rules import (
    LEGAL_RULESET,
    RULE_MATTER_BOUNDARY,
    RULE_PRIVILEGE_BOUNDARY,
    RULE_RECIPIENT_BOUNDARY,
    LegalPolicyContext,
    evaluate_legal_rules,
)
from proofgate.policy import RULE_WORKFLOW_BUDGET
from proofgate.selector import canonical_json

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
COUNSEL = "counsel@example.com"
RULE_ORDER = [RULE_MATTER_BOUNDARY, RULE_PRIVILEGE_BOUNDARY, RULE_WORKFLOW_BUDGET, RULE_RECIPIENT_BOUNDARY]


def _grant(**overrides) -> AuthorityGrant:
    fields = dict(
        grant_id="grant_001",
        workflow_id="wf_123",
        matter_id="M-102",
        max_documents=100,
        privilege_policy="exclude",
        allowed_recipients=(COUNSEL,),
        granted_by="partner-01",
        granted_at=T0,
        expires_at=T0 + datetime.timedelta(hours=8),
    )
    fields.update(overrides)
    return AuthorityGrant(**fields)


def _effect(resources, recipients=(COUNSEL,)) -> LegalResolvedEffect:
    """A consequential effect built directly from (doc_id, matter_id,
    privileged, restricted) tuples, for isolating one rule at a time."""
    facts = tuple(
        ResourceFacts(doc_id=doc_id, matter_id=matter, privileged=privileged, restricted=restricted, content_hash="h")
        for doc_id, matter, privileged, restricted in sorted(resources)
    )
    ids = tuple(fact.doc_id for fact in facts)
    matters: dict[str, int] = {}
    for fact in facts:
        matters[fact.matter_id] = matters.get(fact.matter_id, 0) + 1
    return LegalResolvedEffect(
        requested_scope=RequestedScope(matters=("M-102",), recipients=tuple(sorted(recipients))),
        steps=(),
        all_resolved_resource_ids=ids,
        consequential_resource_ids=ids,
        consequential_resources=facts,
        total_affected=len(ids),
        dimensions=LegalEffectDimensions(
            matters_touched=matters,
            privileged_count=sum(fact.privileged for fact in facts),
            restricted_count=sum(fact.restricted for fact in facts),
            external_recipients=tuple(sorted(recipients)),
        ),
    )


def _m102(count: int, start: int = 1) -> list[tuple[str, str, bool, bool]]:
    return [(f"D-102-{number:03d}", "M-102", False, False) for number in range(start, start + count)]


def _outcome(decision, rule_id):
    return next(outcome for outcome in decision.outcomes if outcome.rule_id == rule_id)


def _status(decision) -> dict[str, str]:
    return {outcome.rule_id: "FAIL" if outcome.triggered else "PASS" for outcome in decision.outcomes}


# ---------------------------------------------------------------------------
# Canonical BLOCK and clean ALLOW, end to end from the L2 resolver
# ---------------------------------------------------------------------------


def test_canonical_effect_blocks_with_three_failures_and_recipient_pass():
    effect = resolve_plan(CANONICAL_PLAN)
    decision = evaluate_legal_rules(_grant(), effect)

    assert decision.verdict == "BLOCK"
    assert [outcome.rule_id for outcome in decision.outcomes] == RULE_ORDER
    assert _status(decision) == {
        RULE_MATTER_BOUNDARY: "FAIL",
        RULE_PRIVILEGE_BOUNDARY: "FAIL",
        RULE_WORKFLOW_BUDGET: "FAIL",
        RULE_RECIPIENT_BOUNDARY: "PASS",
    }
    assert [(rule.rule_id, rule.explanation) for rule in decision.triggered_rules] == [
        (RULE_MATTER_BOUNDARY, "Consequential documents span matters ['M-118'] outside authorized matter M-102."),
        (RULE_PRIVILEGE_BOUNDARY, "9 privileged documents are consequential but the grant excludes privileged content."),
        (RULE_WORKFLOW_BUDGET, "143 unique consequential documents exceed the 100-document workflow budget."),
    ]
    assert decision.missing_requirements == [
        "consequential documents only in matter M-102",
        "no privileged consequential documents",
        "at most 100 unique consequential documents",
    ]


def test_canonical_evidence_shows_requested_m102_but_resolved_m102_and_m118():
    effect = resolve_plan(CANONICAL_PLAN)
    matter = _outcome(evaluate_legal_rules(_grant(), effect), RULE_MATTER_BOUNDARY).details

    assert effect.requested_scope.matters == ("M-102",)
    assert matter["authorized"] == "M-102"
    assert matter["resolved"] == ("M-102", "M-118")
    assert matter["reason"] == "consequential_matter_outside_grant"
    assert matter["unexpected_matters"] == ("M-118",)
    assert matter["unexpected_resource_ids"] == tuple(f"D-118-{number:03d}" for number in range(1, 18))


def test_authorization_never_edits_the_effect():
    effect = resolve_plan(CANONICAL_PLAN)
    before = effect.model_dump_json()

    evaluate_legal_rules(_grant(), effect)

    assert effect.model_dump_json() == before
    assert effect.total_affected == 143
    assert dict(effect.dimensions.matters_touched) == {"M-102": 126, "M-118": 17}


def test_clean_effect_allows_with_all_four_rules_passing():
    decision = evaluate_legal_rules(_grant(), resolve_plan(CLEAN_PLAN))

    assert decision.verdict == "ALLOW"
    assert _status(decision) == dict.fromkeys(RULE_ORDER, "PASS")
    assert decision.triggered_rules == []
    assert decision.missing_requirements == []


# ---------------------------------------------------------------------------
# Matter boundary
# ---------------------------------------------------------------------------


def test_matter_boundary_passes_for_m102_only():
    outcome = _outcome(evaluate_legal_rules(_grant(), _effect(_m102(5))), RULE_MATTER_BOUNDARY)

    assert not outcome.triggered
    assert outcome.details["resolved"] == ("M-102",)
    assert outcome.details["reason"] is None


def test_matter_boundary_fails_when_m118_is_consequential():
    effect = _effect(_m102(5) + [("D-118-001", "M-118", False, False)])
    outcome = _outcome(evaluate_legal_rules(_grant(), effect), RULE_MATTER_BOUNDARY)

    assert outcome.triggered
    assert outcome.details["unexpected_resource_ids"] == ("D-118-001",)


def test_matter_boundary_ignores_an_m118_document_that_was_only_read():
    effect = resolve_plan(
        LegalPlan(
            steps=(
                PlanStep(step_id="step1", tool="search_documents", arguments={"matter": "M-102", "query": "diligence"}),
                PlanStep(step_id="lookup", tool="search_documents", arguments={"matter": "M-118", "query": "agreement"}),
                PlanStep(
                    step_id="step2",
                    tool="compile_package",
                    arguments={"documents": "$step1.result", "include_related": False},
                ),
                PlanStep(
                    step_id="step3",
                    tool="share_package",
                    arguments={"package": "$step2.result", "recipient": COUNSEL},
                ),
            )
        )
    )
    assert any(doc_id.startswith("D-118-") for doc_id in effect.all_resolved_resource_ids)

    decision = evaluate_legal_rules(_grant(), effect)
    assert not _outcome(decision, RULE_MATTER_BOUNDARY).triggered
    assert decision.verdict == "ALLOW"


# ---------------------------------------------------------------------------
# Privilege boundary
# ---------------------------------------------------------------------------


def test_privilege_exclude_with_no_privileged_documents_passes():
    outcome = _outcome(evaluate_legal_rules(_grant(), _effect(_m102(5))), RULE_PRIVILEGE_BOUNDARY)

    assert not outcome.triggered
    assert outcome.details["resolved"] == 0


def test_privilege_exclude_with_one_privileged_document_fails():
    effect = _effect(_m102(4) + [("D-102-999", "M-102", True, False)])
    outcome = _outcome(evaluate_legal_rules(_grant(), effect), RULE_PRIVILEGE_BOUNDARY)

    assert outcome.triggered
    assert outcome.details["authorized"] == "exclude"
    assert outcome.details["resolved"] == 1
    assert outcome.details["privileged_resource_ids"] == ("D-102-999",)


def test_privilege_include_allows_privileged_documents():
    effect = _effect(_m102(4) + [("D-102-999", "M-102", True, False)])
    outcome = _outcome(evaluate_legal_rules(_grant(privilege_policy="include"), effect), RULE_PRIVILEGE_BOUNDARY)

    assert not outcome.triggered
    assert outcome.details["privileged_resource_ids"] == ("D-102-999",)


def test_restricted_is_not_treated_as_privileged():
    effect = _effect(_m102(4) + [("D-102-999", "M-102", False, True)])
    decision = evaluate_legal_rules(_grant(), effect)

    assert not _outcome(decision, RULE_PRIVILEGE_BOUNDARY).triggered
    assert decision.verdict == "ALLOW"


# ---------------------------------------------------------------------------
# Workflow budget
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "consequential,triggered", [(80, False), (100, False), (101, True)], ids=["80", "exactly_100", "101"]
)
def test_budget_boundary(consequential, triggered):
    outcome = _outcome(evaluate_legal_rules(_grant(), _effect(_m102(consequential))), RULE_WORKFLOW_BUDGET)

    assert outcome.triggered is triggered
    assert outcome.details["authorized"] == 100
    assert outcome.details["resolved"] == consequential
    assert outcome.details["projected_unique_count"] == consequential


def test_budget_spec_example_disjoint_committed_ids_block():
    """Spec: previously committed 20 plus 92 disjoint proposed IDs gives 112 -> BLOCK."""
    committed = tuple(f"D-102-{number:03d}" for number in range(501, 521))
    outcome = _outcome(evaluate_legal_rules(_grant(), _effect(_m102(92)), committed), RULE_WORKFLOW_BUDGET)

    assert outcome.triggered
    assert (
        outcome.details["committed_unique_count"],
        outcome.details["proposed_unique_count"],
        outcome.details["projected_unique_count"],
    ) == (20, 92, 112)
    assert outcome.details["overlap_resource_ids"] == ()


def test_budget_spec_example_overlap_counts_once():
    """Spec: if 12 of the 20 committed IDs overlap the 92 proposed, the union is 100 -> PASS."""
    committed = tuple(f"D-102-{number:03d}" for number in range(1, 13)) + tuple(
        f"D-102-{number:03d}" for number in range(501, 509)
    )
    outcome = _outcome(evaluate_legal_rules(_grant(), _effect(_m102(92)), committed), RULE_WORKFLOW_BUDGET)

    assert not outcome.triggered
    assert outcome.details["projected_unique_count"] == 100
    assert len(outcome.details["overlap_resource_ids"]) == 12


def test_duplicate_committed_ids_count_once():
    committed = ("D-102-501", "D-102-501", "D-102-502")
    context = LegalPolicyContext(grant=_grant(), effect=_effect(_m102(98)), committed_resource_ids=committed)

    assert context.committed_resource_ids == ("D-102-501", "D-102-502")
    outcome = _outcome(evaluate_legal_rules(_grant(), _effect(_m102(98)), committed), RULE_WORKFLOW_BUDGET)
    assert not outcome.triggered
    assert outcome.details["projected_unique_count"] == 100


def test_reads_do_not_consume_budget():
    """97 documents resolved (80 packaged + 17 only searched); only the 80
    consequential ones count against a budget of exactly 80."""
    effect = resolve_plan(
        LegalPlan(
            steps=(
                PlanStep(step_id="step1", tool="search_documents", arguments={"matter": "M-102", "query": "diligence"}),
                PlanStep(step_id="lookup", tool="search_documents", arguments={"matter": "M-118", "query": "agreement"}),
                PlanStep(
                    step_id="step2",
                    tool="compile_package",
                    arguments={"documents": "$step1.result", "include_related": False},
                ),
                PlanStep(
                    step_id="step3",
                    tool="share_package",
                    arguments={"package": "$step2.result", "recipient": COUNSEL},
                ),
            )
        )
    )
    assert len(effect.all_resolved_resource_ids) == 97

    outcome = _outcome(evaluate_legal_rules(_grant(max_documents=80), effect), RULE_WORKFLOW_BUDGET)
    assert not outcome.triggered
    assert outcome.details["projected_unique_count"] == 80


# ---------------------------------------------------------------------------
# Recipient boundary
# ---------------------------------------------------------------------------


def test_recipient_allowed_passes():
    outcome = _outcome(evaluate_legal_rules(_grant(), _effect(_m102(3))), RULE_RECIPIENT_BOUNDARY)

    assert not outcome.triggered
    assert outcome.details["authorized"] == (COUNSEL,)
    assert outcome.details["resolved"] == (COUNSEL,)


def test_unauthorized_recipient_fails():
    effect = _effect(_m102(3), recipients=("outside@example.com",))
    outcome = _outcome(evaluate_legal_rules(_grant(), effect), RULE_RECIPIENT_BOUNDARY)

    assert outcome.triggered
    assert outcome.details["unauthorized_recipients"] == ("outside@example.com",)


def test_every_resolved_recipient_is_checked():
    effect = _effect(_m102(3), recipients=(COUNSEL, "outside@example.com"))
    outcome = _outcome(evaluate_legal_rules(_grant(), effect), RULE_RECIPIENT_BOUNDARY)

    assert outcome.triggered
    assert outcome.details["resolved"] == (COUNSEL, "outside@example.com")
    assert outcome.details["unauthorized_recipients"] == ("outside@example.com",)


def test_recipient_comparison_does_not_case_fold():
    effect = _effect(_m102(3), recipients=("Counsel@example.com",))

    assert _outcome(evaluate_legal_rules(_grant(), effect), RULE_RECIPIENT_BOUNDARY).triggered


# ---------------------------------------------------------------------------
# Permission is not authority
# ---------------------------------------------------------------------------


def test_permission_to_access_m118_does_not_authorize_m118():
    snapshot = get_permission_snapshot(SEEDED_USER_ID, captured_at=T0)
    assert "M-118" in snapshot.permitted_matters

    decision = evaluate_legal_rules(_grant(), resolve_plan(CANONICAL_PLAN))

    assert decision.verdict == "BLOCK"
    assert _outcome(decision, RULE_MATTER_BOUNDARY).details["unexpected_matters"] == ("M-118",)


def test_rules_take_no_permission_snapshot():
    assert list(inspect.signature(evaluate_legal_rules).parameters) == ["grant", "effect", "committed_resource_ids"]
    assert set(LegalPolicyContext.model_fields) == {"grant", "effect", "committed_resource_ids"}


# ---------------------------------------------------------------------------
# Determinism, immutability, serialization
# ---------------------------------------------------------------------------


def test_same_inputs_give_equal_decisions_and_details():
    effect = resolve_plan(CANONICAL_PLAN)
    first = evaluate_legal_rules(_grant(), effect, ("D-102-001",))
    second = evaluate_legal_rules(_grant(), resolve_plan(CANONICAL_PLAN), ("D-102-001",))

    assert first == second
    assert [dict(outcome.details) for outcome in first.outcomes] == [
        dict(outcome.details) for outcome in second.outcomes
    ]


def test_details_are_read_only_and_canonically_serializable():
    decision = evaluate_legal_rules(_grant(), resolve_plan(CANONICAL_PLAN))

    for outcome in decision.outcomes:
        assert {"authorized", "resolved", "reason"} <= set(outcome.details)
        with pytest.raises(TypeError):
            outcome.details["reason"] = None
        for value in outcome.details.values():
            assert value is None or isinstance(value, (str, int, tuple))
        assert canonical_json(dict(outcome.details)) == canonical_json(dict(outcome.details))


def test_details_contain_no_document_text():
    decision = evaluate_legal_rules(_grant(), resolve_plan(CANONICAL_PLAN))
    serialized = b"".join(canonical_json(dict(outcome.details)) for outcome in decision.outcomes)

    assert b"Synthetic text" not in serialized
    assert b"BLUE ORCHID" not in serialized


def test_policy_context_is_immutable():
    context = LegalPolicyContext(grant=_grant(), effect=_effect(_m102(1)), committed_resource_ids=("D-102-900",))

    with pytest.raises(ValidationError):
        context.committed_resource_ids = ()
    with pytest.raises(AttributeError):
        context.committed_resource_ids.append("D-102-901")


# ---------------------------------------------------------------------------
# Generic engine boundary
# ---------------------------------------------------------------------------


def test_ruleset_is_exactly_the_four_spec_rules_in_spec_order():
    assert [rule.rule_id for rule in LEGAL_RULESET.rules] == RULE_ORDER


def test_generic_policy_module_knows_no_legal_concepts():
    source = Path(policy_module.__file__).read_text().lower()
    for token in ("matter", "privilege", "recipient", "grant", "legal"):
        assert token not in source, token
    assert {name for name in dir(policy_module) if name.startswith("RULE_")} == {
        "RULE_INTENT_BOUNDARY",
        "RULE_RECOVERY_PROOF",
        "RULE_UNKNOWN_IMPACT",
        "RULE_WORKFLOW_BUDGET",
    }


def test_legal_rules_do_not_touch_operations_registry_or_core():
    tree = ast.parse(Path(rules_module.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = ("operations", "proofgate.core", "proofgate.registry", "proofgate.bindings.operations", "agent", "craft")
    assert not [module for module in imported if module.startswith(forbidden)]
