"""Slice 4 tests: deterministic BLOCK and structured repair.

guarded_delete_users always previews against the real operations/working.db
(it has no test-only db_path seam, per the frozen contract), so these tests
reset that real file to its pristine state via a fixture and verify zero
mutation directly against it. This is safe because this slice never calls
operations.delete_users.
"""

from unittest import mock

import pytest

from agent.nebius_client import extract_intent, extract_risk_features
from operations.actions import count_rows, delete_users
from operations.database import PRISTINE_DB_PATH, WORKING_DB_PATH, reset_working_db
from proofgate.core import guarded_delete_users
from proofgate.models import ActionContext, ImpactEnvelope, IntentConstraints, WorkflowBudget
from proofgate.policy import (
    RULE_INTENT_BOUNDARY,
    RULE_RECOVERY_PROOF,
    RULE_UNKNOWN_IMPACT,
    RULE_WORKFLOW_BUDGET,
    evaluate_policy,
)

BROAD_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


@pytest.fixture(autouse=True)
def _reset_real_working_db():
    reset_working_db()
    yield
    reset_working_db()


def _broad_action_context() -> ActionContext:
    return ActionContext(
        workflow_id="wf-1",
        requesting_user="demo-user",
        agent_id="demo-agent",
        original_instruction=BROAD_INSTRUCTION,
    )


def _run_broad():
    return guarded_delete_users(
        action_context=_broad_action_context(),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )


def test_broad_action_returns_block():
    result = _run_broad()
    assert result.verdict == "BLOCK"


def test_broad_action_executed_false():
    result = _run_broad()
    assert result.executed is False


def test_zero_rows_mutated_after_block():
    before = count_rows(WORKING_DB_PATH)
    assert before == 10623

    _run_broad()

    after = count_rows(WORKING_DB_PATH)
    assert after == before == 10623


def test_triggered_rule_set_for_broad_action():
    result = _run_broad()
    triggered_ids = {rule.rule_id for rule in result.triggered_rules}
    assert triggered_ids == {RULE_INTENT_BOUNDARY, RULE_RECOVERY_PROOF, RULE_WORKFLOW_BUDGET}


def test_rule_unknown_impact_absent_when_preview_succeeds():
    result = _run_broad()
    triggered_ids = {rule.rule_id for rule in result.triggered_rules}
    assert RULE_UNKNOWN_IMPACT not in triggered_ids


def test_risk_factors_and_triggered_rules_are_separate_fields():
    result = _run_broad()
    assert isinstance(result.risk_factors, list)
    assert all(isinstance(item, str) for item in result.risk_factors)
    assert isinstance(result.triggered_rules, list)
    assert all(hasattr(item, "rule_id") for item in result.triggered_rules)

    expected_factors = {
        "mass scope",
        "production impact",
        "intent mismatch",
        "irreversible action",
        "missing proof",
    }
    assert expected_factors.issubset(set(result.risk_factors))


def test_suggested_repairs_contains_corrected_call_and_next_step():
    result = _run_broad()
    assert {
        "tool": "delete_users",
        "arguments": {"inactive_days": 90, "environment": "test"},
        "next_step": "create_snapshot",
    } in result.suggested_repairs


# ---------------------------------------------------------------------------
# Slice 24/25: build_suggested_repairs names the real tool being repaired
# and preserves whatever selector arguments that tool actually used.
# ---------------------------------------------------------------------------


def test_build_suggested_repairs_names_delete_users():
    from proofgate.policy import build_suggested_repairs

    intent = IntentConstraints(
        action_type="delete", target_resource="users", environment="test", inactivity_days=90, confidence=1.0
    )
    repairs = build_suggested_repairs(
        intent=intent,
        selector_arguments={"inactive_days": 90, "environment": None},
        tool="delete_users",
        hard_delete=True,
    )
    assert repairs == [
        {"tool": "delete_users", "arguments": {"inactive_days": 90, "environment": "test"}, "next_step": "create_snapshot"}
    ]


def test_build_suggested_repairs_names_deactivate_users():
    from proofgate.policy import build_suggested_repairs

    intent = IntentConstraints(
        action_type="deactivate", target_resource="users", environment="test", inactivity_days=90, confidence=1.0
    )
    repairs = build_suggested_repairs(
        intent=intent,
        selector_arguments={"inactive_days": 90, "environment": None},
        tool="deactivate_users",
        hard_delete=False,
    )
    assert repairs == [
        {
            "tool": "deactivate_users",
            "arguments": {"inactive_days": 90, "environment": "test"},
            "next_step": "retry_with_corrected_environment",
        }
    ]


def test_build_suggested_repairs_empty_when_intent_environment_is_none():
    from proofgate.policy import build_suggested_repairs

    intent = IntentConstraints(
        action_type="delete", target_resource="users", environment=None, inactivity_days=90, confidence=1.0
    )
    assert (
        build_suggested_repairs(
            intent=intent, selector_arguments={"inactive_days": 90, "environment": None}, tool="delete_users", hard_delete=True
        )
        == []
    )


def test_build_suggested_repairs_preserves_non_environment_selector_arguments_generically():
    """The generalization Slice 25 needed: a tool with a completely
    different selector shape (flag_name/enabled/rollout_percentage) must
    get an honestly-shaped repair suggestion, with only environment
    corrected -- proving this function performs no tool-specific
    argument-shape assumptions."""
    from proofgate.policy import build_suggested_repairs

    intent = IntentConstraints(
        action_type="unknown", target_resource="unknown", environment="test", inactivity_days=None, confidence=0.4
    )
    repairs = build_suggested_repairs(
        intent=intent,
        selector_arguments={"flag_name": "new_checkout", "enabled": True, "environment": None, "rollout_percentage": 100},
        tool="set_feature_flag",
        hard_delete=False,
    )
    assert repairs == [
        {
            "tool": "set_feature_flag",
            "arguments": {
                "flag_name": "new_checkout",
                "enabled": True,
                "environment": "test",
                "rollout_percentage": 100,
            },
            "next_step": "retry_with_corrected_environment",
        }
    ]


def test_missing_requirements_contains_expected_three():
    result = _run_broad()
    assert "environment=test" in result.missing_requirements
    assert "valid rollback proof" in result.missing_requirements
    assert "workflow impact within 100 rows" in result.missing_requirements


def test_workflow_budget_starts_at_0_of_100():
    from proofgate.policy import MAX_MUTATED_ROWS_PER_WORKFLOW

    budget = WorkflowBudget(workflow_id="wf-1")
    assert budget.rows_mutated == 0
    assert MAX_MUTATED_ROWS_PER_WORKFLOW == 100


def test_risk_features_counts_derived_only_from_impact_envelope():
    intent = IntentConstraints(
        action_type="delete_users",
        target_resource="users",
        environment="test",
        inactivity_days=90,
        confidence=0.98,
    )
    fabricated_impact = ImpactEnvelope(
        tool_name="delete_users",
        estimated_count=5,
        environment_counts={"production": 0, "test": 5},
        hard_delete=True,
        reversibility="irreversible_without_snapshot",
        selector_hash="deadbeef",
        generated_at="2026-07-11T00:00:00+00:00",
    )
    risk = extract_risk_features(
        intent, {"inactive_days": 90, "environment": None}, fabricated_impact
    )
    # production_impact must reflect the passed-in envelope (0 production
    # rows), not any assumption from intent or the real database.
    assert risk.production_impact is False
    assert risk.scope_class == "small"


def test_operations_delete_users_never_invoked_on_block():
    with mock.patch("operations.actions.delete_users") as mocked_delete:
        result = _run_broad()
        assert result.verdict == "BLOCK"
        mocked_delete.assert_not_called()


def test_unknown_impact_rule_triggers_when_preview_unavailable():
    intent = extract_intent(BROAD_INSTRUCTION)
    triggered, risk_factors = evaluate_policy(
        intent=intent,
        proposed_arguments={"inactive_days": 90, "environment": None},
        impact=None,
        risk=None,
        rollback_proof_valid=False,
        workflow_budget=WorkflowBudget(workflow_id="wf-1"),
    )
    triggered_ids = {rule.rule_id for rule in triggered}
    assert triggered_ids == {RULE_UNKNOWN_IMPACT}


def test_pristine_db_rejected_as_mutation_target():
    with pytest.raises(ValueError):
        delete_users(inactive_days=90, environment=None, db_path=PRISTINE_DB_PATH)

    with pytest.raises(ValueError):
        reset_working_db(working_path=PRISTINE_DB_PATH)


def test_verdict_is_produced_deterministically_by_policy_not_hardcoded():
    first = _run_broad()
    second = _run_broad()

    assert first.verdict == second.verdict == "BLOCK"
    assert {r.rule_id for r in first.triggered_rules} == {
        r.rule_id for r in second.triggered_rules
    }
    assert first.risk_factors == second.risk_factors
