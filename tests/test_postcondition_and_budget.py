"""Slices 8+9 tests: formal postcondition verification and real per-workflow
cumulative budget state.

guarded_delete_users always operates on the real operations/working.db (no
test-only db_path seam), so these tests reset that real file before each
test. Workflow state lives in a module-level store in proofgate.budgets, so
most tests use a fresh uuid4 workflow_id to stay isolated from other tests;
tests that specifically prove isolation/reset use fixed ids deliberately.
"""

import uuid
from unittest import mock

import pytest

import operations.actions as operations_actions
from operations.actions import count_rows, delete_users
from operations.database import PRISTINE_DB_PATH, WORKING_DB_PATH, reset_working_db
from operations.snapshots import create_snapshot
from proofgate.budgets import (
    get_last_mutation_result,
    get_last_postcondition_result,
    get_workflow_budget,
    record_execution,
    reset_workflow_state,
)
from proofgate.core import guarded_delete_users
from proofgate.models import (
    ActionContext,
    ImpactEnvelope,
    MutationResult,
    PostconditionResult,
    RollbackProof,
)
from proofgate.policy import RULE_WORKFLOW_BUDGET
from proofgate.postcondition import verify_postcondition

BROAD_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


@pytest.fixture(autouse=True)
def _reset_real_working_db():
    reset_working_db()
    yield
    reset_working_db()


def _fresh_workflow_id() -> str:
    return f"wf-{uuid.uuid4().hex[:12]}"


def _action_context(workflow_id: str) -> ActionContext:
    return ActionContext(
        workflow_id=workflow_id,
        requesting_user="demo-user",
        agent_id="demo-agent",
        original_instruction=BROAD_INSTRUCTION,
    )


def _make_impact(estimated_count: int, selector_hash: str = "irrelevant") -> ImpactEnvelope:
    return ImpactEnvelope(
        tool_name="delete_users",
        estimated_count=estimated_count,
        environment_counts={"production": 0, "test": estimated_count},
        hard_delete=True,
        reversibility="irreversible_without_snapshot",
        selector_hash=selector_hash,
        generated_at="2026-07-11T00:00:00+00:00",
    )


def _make_mutation(affected: int, production: int, test: int) -> MutationResult:
    return MutationResult(affected_count=affected, production_affected=production, test_affected=test)


# ---------------------------------------------------------------------------
# verify_postcondition (pure function)
# ---------------------------------------------------------------------------


def test_postcondition_result_has_frozen_fields_and_valid_statuses():
    for status in ("VERIFIED", "MISMATCH", "ROLLED_BACK", "MANUAL_REVIEW_REQUIRED"):
        result = PostconditionResult(
            predicted_count=1, actual_count=1, production_affected=0, status=status
        )
        assert result.status == status
    assert set(PostconditionResult.model_fields) == {
        "predicted_count",
        "actual_count",
        "production_affected",
        "status",
    }


def test_verify_postcondition_verified_when_predicted_equals_actual_zero_production():
    result = verify_postcondition(_make_impact(92), _make_mutation(92, 0, 92))
    assert result.predicted_count == 92
    assert result.actual_count == 92
    assert result.production_affected == 0
    assert result.status == "VERIFIED"


def test_verify_postcondition_mismatch_when_counts_differ_zero_production():
    result = verify_postcondition(_make_impact(92), _make_mutation(90, 0, 90))
    assert result.status == "MISMATCH"
    assert result.production_affected == 0


def test_verify_postcondition_manual_review_when_any_production_affected():
    result = verify_postcondition(_make_impact(92), _make_mutation(93, 1, 92))
    assert result.status == "MANUAL_REVIEW_REQUIRED"
    assert result.production_affected == 1

    # Even when predicted == actual, any production impact forces manual review.
    result_matched_counts = verify_postcondition(_make_impact(92), _make_mutation(92, 5, 87))
    assert result_matched_counts.status == "MANUAL_REVIEW_REQUIRED"


def test_rolled_back_is_never_emitted_by_verify_postcondition():
    scenarios = [
        (_make_impact(92), _make_mutation(92, 0, 92)),
        (_make_impact(92), _make_mutation(90, 0, 90)),
        (_make_impact(92), _make_mutation(93, 1, 92)),
        (_make_impact(0), _make_mutation(0, 0, 0)),
        (_make_impact(10073), _make_mutation(10073, 9981, 92)),
    ]
    for impact, mutation in scenarios:
        result = verify_postcondition(impact, mutation)
        assert result.status != "ROLLED_BACK"


# ---------------------------------------------------------------------------
# guarded_delete_users: automatic postcondition + real budget on ALLOW
# ---------------------------------------------------------------------------


def test_corrected_valid_proof_allow_stores_92_92_0_verified_automatically():
    workflow_id = _fresh_workflow_id()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )

    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )

    assert result.verdict == "ALLOW"
    assert result.executed is True

    # No separate verification call is required by the caller.
    postcondition = get_last_postcondition_result(workflow_id)
    assert postcondition is not None
    assert postcondition.predicted_count == 92
    assert postcondition.actual_count == 92
    assert postcondition.production_affected == 0
    assert postcondition.status == "VERIFIED"


def test_initial_workflow_budget_is_0_of_100():
    workflow_id = _fresh_workflow_id()
    budget = get_workflow_budget(workflow_id)
    assert budget.rows_mutated == 0
    assert budget.production_rows_mutated == 0
    assert budget.max_rows == 100


def test_successful_execution_updates_budget_to_92_of_100():
    workflow_id = _fresh_workflow_id()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )

    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )

    budget = get_workflow_budget(workflow_id)
    assert budget.rows_mutated == 92
    assert budget.max_rows == 100


def test_budget_accounting_uses_mutation_result_not_predicted_count():
    workflow_id = _fresh_workflow_id()
    fabricated_impact = _make_impact(estimated_count=999)
    fabricated_mutation = _make_mutation(affected=50, production=0, test=50)
    fabricated_postcondition = verify_postcondition(fabricated_impact, fabricated_mutation)

    record_execution(workflow_id, fabricated_mutation, fabricated_postcondition)

    budget = get_workflow_budget(workflow_id)
    assert budget.rows_mutated == 50  # actual, not the fabricated predicted_count of 999


def test_budget_updates_even_on_mismatch_or_manual_review_status():
    workflow_id = _fresh_workflow_id()
    mismatch_mutation = _make_mutation(affected=10, production=0, test=10)
    mismatch_postcondition = verify_postcondition(_make_impact(20), mismatch_mutation)
    assert mismatch_postcondition.status == "MISMATCH"
    record_execution(workflow_id, mismatch_mutation, mismatch_postcondition)
    assert get_workflow_budget(workflow_id).rows_mutated == 10

    workflow_id_2 = _fresh_workflow_id()
    review_mutation = _make_mutation(affected=5, production=5, test=0)
    review_postcondition = verify_postcondition(_make_impact(5), review_mutation)
    assert review_postcondition.status == "MANUAL_REVIEW_REQUIRED"
    record_execution(workflow_id_2, review_mutation, review_postcondition)
    assert get_workflow_budget(workflow_id_2).rows_mutated == 5
    assert get_workflow_budget(workflow_id_2).production_rows_mutated == 5


# ---------------------------------------------------------------------------
# BLOCK leaves budget/mutation/postcondition untouched
# ---------------------------------------------------------------------------


def test_broad_block_leaves_budget_unchanged():
    workflow_id = _fresh_workflow_id()

    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    assert result.verdict == "BLOCK"
    budget = get_workflow_budget(workflow_id)
    assert budget.rows_mutated == 0
    assert budget.production_rows_mutated == 0


def test_corrected_block_without_proof_leaves_budget_unchanged():
    workflow_id = _fresh_workflow_id()

    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment="test",
        rollback_proof=None,
    )

    assert result.verdict == "BLOCK"
    budget = get_workflow_budget(workflow_id)
    assert budget.rows_mutated == 0


def test_block_creates_no_mutation_or_postcondition_result():
    workflow_id = _fresh_workflow_id()

    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    assert get_last_mutation_result(workflow_id) is None
    assert get_last_postcondition_result(workflow_id) is None


# ---------------------------------------------------------------------------
# Cumulative budget behavior
# ---------------------------------------------------------------------------


def test_existing_budget_20_plus_proposed_92_triggers_workflow_budget_rule():
    workflow_id = _fresh_workflow_id()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )

    # Seed an existing budget of 20 rows for this workflow via the public accessor.
    budget = get_workflow_budget(workflow_id)
    budget.rows_mutated = 20

    before_rows = count_rows(WORKING_DB_PATH)

    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )

    assert result.verdict == "BLOCK"
    assert {r.rule_id for r in result.triggered_rules} == {RULE_WORKFLOW_BUDGET}
    assert result.executed is False
    assert count_rows(WORKING_DB_PATH) == before_rows
    assert get_workflow_budget(workflow_id).rows_mutated == 20


# ---------------------------------------------------------------------------
# Isolation and reset
# ---------------------------------------------------------------------------


def test_workflow_states_are_isolated_by_workflow_id():
    workflow_a = _fresh_workflow_id()
    workflow_b = _fresh_workflow_id()

    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    guarded_delete_users(
        action_context=_action_context(workflow_a),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )

    assert get_workflow_budget(workflow_a).rows_mutated == 92
    assert get_workflow_budget(workflow_b).rows_mutated == 0
    assert get_last_mutation_result(workflow_b) is None


def test_reset_workflow_state_clears_budget_mutation_and_postcondition():
    workflow_id = _fresh_workflow_id()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )
    assert get_workflow_budget(workflow_id).rows_mutated == 92
    assert get_last_mutation_result(workflow_id) is not None
    assert get_last_postcondition_result(workflow_id) is not None

    reset_workflow_state(workflow_id)

    assert get_workflow_budget(workflow_id).rows_mutated == 0
    assert get_last_mutation_result(workflow_id) is None
    assert get_last_postcondition_result(workflow_id) is None


def test_reset_working_db_does_not_reset_proofgate_state():
    workflow_id = _fresh_workflow_id()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )
    assert get_workflow_budget(workflow_id).rows_mutated == 92

    reset_working_db()

    assert get_workflow_budget(workflow_id).rows_mutated == 92
    assert get_last_mutation_result(workflow_id) is not None


def test_reset_workflow_state_does_not_mutate_database():
    reset_working_db()
    before = count_rows(WORKING_DB_PATH)

    reset_workflow_state(_fresh_workflow_id())

    assert count_rows(WORKING_DB_PATH) == before


def test_operations_module_does_not_import_proofgate():
    import ast
    from pathlib import Path

    database_source = Path(operations_actions.__file__).parent / "database.py"
    tree = ast.parse(database_source.read_text())
    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert not any(name.startswith("proofgate") for name in imported_modules)


# ---------------------------------------------------------------------------
# Execution boundary and pristine safety, re-verified in this slice's context
# ---------------------------------------------------------------------------


def test_operations_delete_users_called_exactly_once_and_only_after_allow():
    workflow_id = _fresh_workflow_id()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )

    with mock.patch(
        "operations.actions.delete_users", wraps=operations_actions.delete_users
    ) as spy:
        blocked = guarded_delete_users(
            action_context=_action_context(workflow_id),
            inactive_days=90,
            environment="test",
            rollback_proof=None,
        )
        assert blocked.verdict == "BLOCK"
        spy.assert_not_called()

        allowed = guarded_delete_users(
            action_context=_action_context(workflow_id),
            inactive_days=90,
            environment="test",
            rollback_proof=proof,
        )
        assert allowed.verdict == "ALLOW"
        spy.assert_called_once_with(inactive_days=90, environment="test")


def test_pristine_db_unchanged_across_full_postcondition_and_budget_flow():
    import hashlib

    before_hash = hashlib.sha256(PRISTINE_DB_PATH.read_bytes()).hexdigest()

    workflow_id = _fresh_workflow_id()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )

    after_hash = hashlib.sha256(PRISTINE_DB_PATH.read_bytes()).hexdigest()
    assert before_hash == after_hash
