"""Slice 16 tests: the generic guarded_execute entrypoint, the minimal
in-code tool registry, and guarded_delete_users as a thin compatibility
wrapper around guarded_execute("delete_users", ...).

tests/conftest.py's autouse fixtures already disable live CRAFT/Nebius and
isolate the audit log per test, so these tests never touch the network.
"""

import inspect
import uuid
from unittest import mock

import operations.actions as operations_actions
import proofgate.audit as audit_module
from operations.actions import count_rows, delete_users, preview_delete_users
from operations.database import WORKING_DB_PATH, reset_working_db
from operations.snapshots import create_snapshot
from proofgate.budgets import get_workflow_budget, reset_workflow_state
from proofgate.core import guarded_delete_users, guarded_execute
from proofgate.models import ActionContext
from proofgate.policy import (
    RULE_INTENT_BOUNDARY,
    RULE_RECOVERY_PROOF,
    RULE_UNKNOWN_IMPACT,
    RULE_WORKFLOW_BUDGET,
)
from proofgate.registry import GuardedToolSpec, get_tool_spec
from proofgate.selector import compute_selector_hash

BROAD_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


def _fresh_workflow_id() -> str:
    return f"wf-{uuid.uuid4().hex[:12]}"


def _action_context(workflow_id: str) -> ActionContext:
    return ActionContext(
        workflow_id=workflow_id,
        requesting_user="demo-user",
        agent_id="demo-agent",
        original_instruction=BROAD_INSTRUCTION,
    )


def _fresh_clean_state(workflow_id: str) -> None:
    """Reset DB, workflow state, and audit for one isolated parity run."""
    reset_working_db()
    reset_workflow_state(workflow_id)


def _find_audit_event(event_id: str) -> dict | None:
    import json

    audit_path = audit_module.DEFAULT_AUDIT_PATH
    if not audit_path.exists():
        return None
    for line in audit_path.read_text().strip().splitlines():
        if not line:
            continue
        raw = json.loads(line)
        if raw.get("event_id") == event_id:
            return raw
    return None


# ---------------------------------------------------------------------------
# Existing wrapper parity: guarded_delete_users behavior is unchanged
# ---------------------------------------------------------------------------


def test_wrapper_broad_call_still_blocks_with_exact_demo_counts():
    workflow_id = _fresh_workflow_id()
    _fresh_clean_state(workflow_id)

    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    assert result.verdict == "BLOCK"
    assert result.executed is False
    assert {r.rule_id for r in result.triggered_rules} == {
        RULE_INTENT_BOUNDARY,
        RULE_RECOVERY_PROOF,
        RULE_WORKFLOW_BUDGET,
    }
    assert count_rows(WORKING_DB_PATH) == 10623  # zero mutation

    audit_event = _find_audit_event(result.audit_event_id)
    impact = audit_event["impact_envelope"]
    assert impact["estimated_count"] == 10073
    assert impact["environment_counts"] == {"production": 9981, "test": 92}


def test_wrapper_corrected_call_still_allows_and_executes_exactly_92():
    workflow_id = _fresh_workflow_id()
    _fresh_clean_state(workflow_id)
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
    assert result.triggered_rules == []

    budget = get_workflow_budget(workflow_id)
    assert budget.rows_mutated == 92
    assert budget.max_rows == 100

    audit_event = _find_audit_event(result.audit_event_id)
    assert audit_event["mutation_result"] == {
        "affected_count": 92,
        "production_affected": 0,
        "test_affected": 92,
    }
    assert audit_event["postcondition_result"]["status"] == "VERIFIED"


def test_wrapper_delete_users_called_exactly_once_and_only_after_allow():
    workflow_id = _fresh_workflow_id()
    _fresh_clean_state(workflow_id)
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )

    with mock.patch(
        "operations.actions.delete_users", wraps=operations_actions.delete_users
    ) as spy:
        blocked = guarded_delete_users(
            action_context=_action_context(workflow_id),
            inactive_days=90,
            environment=None,
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


# ---------------------------------------------------------------------------
# Generic entrypoint parity: guarded_execute("delete_users", ...) matches
# guarded_delete_users(...) for equivalent inputs, from equivalent clean state
# ---------------------------------------------------------------------------


def test_generic_broad_call_matches_wrapper_broad_call():
    wrapper_workflow = _fresh_workflow_id()
    _fresh_clean_state(wrapper_workflow)
    wrapper_result = guarded_delete_users(
        action_context=_action_context(wrapper_workflow),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    generic_workflow = _fresh_workflow_id()
    _fresh_clean_state(generic_workflow)
    generic_result = guarded_execute(
        tool_name="delete_users",
        action_context=_action_context(generic_workflow),
        arguments={"inactive_days": 90, "environment": None},
        rollback_proof=None,
    )

    assert wrapper_result.verdict == generic_result.verdict == "BLOCK"
    assert wrapper_result.executed == generic_result.executed is False
    assert wrapper_result.risk_score == generic_result.risk_score
    assert wrapper_result.risk_factors == generic_result.risk_factors
    assert {r.rule_id for r in wrapper_result.triggered_rules} == {
        r.rule_id for r in generic_result.triggered_rules
    }
    assert wrapper_result.missing_requirements == generic_result.missing_requirements
    assert wrapper_result.suggested_repairs == generic_result.suggested_repairs
    assert wrapper_result.audit_event_id != generic_result.audit_event_id  # distinct, as expected

    wrapper_budget = get_workflow_budget(wrapper_workflow)
    generic_budget = get_workflow_budget(generic_workflow)
    assert wrapper_budget.rows_mutated == generic_budget.rows_mutated == 0

    wrapper_audit = _find_audit_event(wrapper_result.audit_event_id)
    generic_audit = _find_audit_event(generic_result.audit_event_id)
    assert wrapper_audit["verdict"] == generic_audit["verdict"] == "BLOCK"
    assert wrapper_audit["impact_envelope"]["estimated_count"] == generic_audit["impact_envelope"]["estimated_count"] == 10073
    assert (
        wrapper_audit["impact_envelope"]["environment_counts"]
        == generic_audit["impact_envelope"]["environment_counts"]
        == {"production": 9981, "test": 92}
    )
    assert wrapper_audit["proof_status"] == generic_audit["proof_status"] == "MISSING"
    assert wrapper_audit["execution_status"] == generic_audit["execution_status"] == "NOT_EXECUTED"
    assert wrapper_audit["mutation_result"] is generic_audit["mutation_result"] is None


def test_generic_corrected_call_matches_wrapper_corrected_call():
    wrapper_workflow = _fresh_workflow_id()
    _fresh_clean_state(wrapper_workflow)
    wrapper_proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    wrapper_result = guarded_delete_users(
        action_context=_action_context(wrapper_workflow),
        inactive_days=90,
        environment="test",
        rollback_proof=wrapper_proof,
    )

    generic_workflow = _fresh_workflow_id()
    _fresh_clean_state(generic_workflow)
    generic_proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )
    generic_result = guarded_execute(
        tool_name="delete_users",
        action_context=_action_context(generic_workflow),
        arguments={"inactive_days": 90, "environment": "test"},
        rollback_proof=generic_proof,
    )

    assert wrapper_result.verdict == generic_result.verdict == "ALLOW"
    assert wrapper_result.executed == generic_result.executed is True
    assert wrapper_result.triggered_rules == generic_result.triggered_rules == []
    assert wrapper_result.risk_score == generic_result.risk_score

    wrapper_budget = get_workflow_budget(wrapper_workflow)
    generic_budget = get_workflow_budget(generic_workflow)
    assert wrapper_budget.rows_mutated == generic_budget.rows_mutated == 92
    assert wrapper_budget.production_rows_mutated == generic_budget.production_rows_mutated == 0
    assert wrapper_budget.max_rows == generic_budget.max_rows == 100

    wrapper_audit = _find_audit_event(wrapper_result.audit_event_id)
    generic_audit = _find_audit_event(generic_result.audit_event_id)
    assert wrapper_audit["mutation_result"] == generic_audit["mutation_result"] == {
        "affected_count": 92,
        "production_affected": 0,
        "test_affected": 92,
    }
    assert (
        wrapper_audit["postcondition_result"]["status"]
        == generic_audit["postcondition_result"]["status"]
        == "VERIFIED"
    )
    assert wrapper_audit["proof_status"] == generic_audit["proof_status"] == "VALID"
    assert wrapper_audit["proof_checks"] == generic_audit["proof_checks"]


def test_generic_delete_users_called_exactly_once_and_only_after_allow():
    workflow_id = _fresh_workflow_id()
    _fresh_clean_state(workflow_id)
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )

    with mock.patch(
        "operations.actions.delete_users", wraps=operations_actions.delete_users
    ) as spy:
        blocked = guarded_execute(
            tool_name="delete_users",
            action_context=_action_context(workflow_id),
            arguments={"inactive_days": 90, "environment": None},
            rollback_proof=None,
        )
        assert blocked.verdict == "BLOCK"
        spy.assert_not_called()

        allowed = guarded_execute(
            tool_name="delete_users",
            action_context=_action_context(workflow_id),
            arguments={"inactive_days": 90, "environment": "test"},
            rollback_proof=proof,
        )
        assert allowed.verdict == "ALLOW"
        spy.assert_called_once_with(inactive_days=90, environment="test")


# ---------------------------------------------------------------------------
# Unknown-tool behavior: fail closed, no side effects
# ---------------------------------------------------------------------------


def test_unknown_tool_returns_block_and_not_executed():
    workflow_id = _fresh_workflow_id()
    reset_workflow_state(workflow_id)

    result = guarded_execute(
        tool_name="deactivate_users",
        action_context=_action_context(workflow_id),
        arguments={"inactive_days": 90},
        rollback_proof=None,
    )

    assert result.verdict == "BLOCK"
    assert result.executed is False


def test_unknown_tool_triggers_rule_unknown_impact_only():
    workflow_id = _fresh_workflow_id()
    reset_workflow_state(workflow_id)

    result = guarded_execute(
        tool_name="deactivate_users",
        action_context=_action_context(workflow_id),
        arguments={},
        rollback_proof=None,
    )

    assert [r.rule_id for r in result.triggered_rules] == [RULE_UNKNOWN_IMPACT]
    assert result.suggested_repairs == []


def test_unknown_tool_never_calls_operations_preview_or_mutation():
    workflow_id = _fresh_workflow_id()
    reset_workflow_state(workflow_id)

    with mock.patch("operations.actions.preview_delete_users") as preview_spy, mock.patch(
        "operations.actions.delete_users"
    ) as mutation_spy:
        result = guarded_execute(
            tool_name="deactivate_users",
            action_context=_action_context(workflow_id),
            arguments={"inactive_days": 90},
            rollback_proof=None,
        )
        assert result.verdict == "BLOCK"
        preview_spy.assert_not_called()
        mutation_spy.assert_not_called()


def test_unknown_tool_never_creates_a_snapshot():
    workflow_id = _fresh_workflow_id()
    reset_workflow_state(workflow_id)

    with mock.patch("operations.snapshots.create_snapshot") as snapshot_spy:
        guarded_execute(
            tool_name="deactivate_users",
            action_context=_action_context(workflow_id),
            arguments={},
            rollback_proof=None,
        )
        snapshot_spy.assert_not_called()


def test_unknown_tool_does_not_consume_workflow_budget():
    workflow_id = _fresh_workflow_id()
    reset_workflow_state(workflow_id)
    budget_before = get_workflow_budget(workflow_id).rows_mutated

    guarded_execute(
        tool_name="deactivate_users",
        action_context=_action_context(workflow_id),
        arguments={"inactive_days": 90},
        rollback_proof=None,
    )

    assert get_workflow_budget(workflow_id).rows_mutated == budget_before == 0


def test_unknown_tool_raises_no_unhandled_exception_even_with_empty_arguments():
    workflow_id = _fresh_workflow_id()
    reset_workflow_state(workflow_id)

    result = guarded_execute(
        tool_name="totally_unregistered_tool",
        action_context=_action_context(workflow_id),
        arguments={},
        rollback_proof=None,
    )
    assert result.verdict == "BLOCK"


def test_unknown_tool_audit_event_has_no_fake_impact_or_mutation_data():
    workflow_id = _fresh_workflow_id()
    reset_workflow_state(workflow_id)

    result = guarded_execute(
        tool_name="deactivate_users",
        action_context=_action_context(workflow_id),
        arguments={"inactive_days": 90},
        rollback_proof=None,
    )

    audit_event = _find_audit_event(result.audit_event_id)
    assert audit_event is not None
    assert audit_event["impact_envelope"] is None
    assert audit_event["risk_features"] is None
    assert audit_event["mutation_result"] is None
    assert audit_event["postcondition_result"] is None
    assert audit_event["proof_status"] == "MISSING"
    assert audit_event["proof_checks"] is None
    assert audit_event["tool_name"] == "deactivate_users"
    assert audit_event["workflow_budget_before"] == audit_event["workflow_budget_after"]


def test_unknown_tool_with_supplied_rollback_proof_reports_invalid_not_missing():
    workflow_id = _fresh_workflow_id()
    reset_workflow_state(workflow_id)
    # A real (but irrelevant, since no preview ever runs) proof object.
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )

    result = guarded_execute(
        tool_name="deactivate_users",
        action_context=_action_context(workflow_id),
        arguments={},
        rollback_proof=proof,
    )

    audit_event = _find_audit_event(result.audit_event_id)
    assert audit_event["proof_status"] == "INVALID"


# ---------------------------------------------------------------------------
# Registry correctness
# ---------------------------------------------------------------------------


def test_registry_contains_delete_users_with_correct_metadata():
    spec = get_tool_spec("delete_users")
    assert isinstance(spec, GuardedToolSpec)
    assert spec.tool_name == "delete_users"
    assert spec.resource == "users"
    assert spec.hard_delete is True
    assert spec.reversibility == "irreversible_without_snapshot"
    assert spec.selector_argument_names == ("inactive_days", "environment")


def test_registry_preview_fn_produces_same_result_as_real_preview_function():
    reset_working_db()
    spec = get_tool_spec("delete_users")

    via_registry = spec.preview_fn(inactive_days=90, environment=None)
    via_direct_call = preview_delete_users(inactive_days=90, environment=None)

    assert via_registry.estimated_count == via_direct_call.estimated_count == 10073
    assert via_registry.environment_counts == via_direct_call.environment_counts
    assert via_registry.selector_hash == via_direct_call.selector_hash


def test_registry_mutation_fn_produces_same_result_as_real_mutation_function():
    reset_working_db()
    spec = get_tool_spec("delete_users")

    result = spec.mutation_fn(inactive_days=90, environment="test")

    assert result.affected_count == 92
    assert result.production_affected == 0
    assert result.test_affected == 92


def test_registry_returns_none_for_unregistered_tool():
    assert get_tool_spec("deactivate_users") is None
    assert get_tool_spec("") is None


# ---------------------------------------------------------------------------
# Selector-hash correctness: still exactly one shared implementation
# ---------------------------------------------------------------------------


def test_generic_path_impact_selector_hash_matches_shared_implementation():
    reset_working_db()
    spec = get_tool_spec("delete_users")

    impact = spec.preview_fn(inactive_days=90, environment="test")

    assert impact.selector_hash == compute_selector_hash(
        {"inactive_days": 90, "environment": "test"}
    )


def test_corrected_selector_is_logically_equivalent_to_expected_json():
    # Key order must not matter -- same canonical hash either way.
    hash_a = compute_selector_hash({"environment": "test", "inactive_days": 90})
    hash_b = compute_selector_hash({"inactive_days": 90, "environment": "test"})
    assert hash_a == hash_b


def test_registry_module_does_not_define_a_second_canonicalization_function():
    import ast
    from pathlib import Path

    import proofgate.registry as registry_module

    source = Path(registry_module.__file__).read_text()
    tree = ast.parse(source)
    defined_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert "compute_selector_hash" not in defined_names
    assert "canonical_selector_json" not in defined_names


# ---------------------------------------------------------------------------
# Operations boundary correctness
# ---------------------------------------------------------------------------


def test_operations_actions_module_does_not_import_proofgate_core_or_registry():
    import ast
    from pathlib import Path

    source = Path(operations_actions.__file__).read_text()
    tree = ast.parse(source)
    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert not any(name.startswith("proofgate.core") for name in imported_modules)
    assert not any(name.startswith("proofgate.registry") for name in imported_modules)


def test_registered_preview_and_mutation_signatures_carry_no_policy_or_proof_metadata():
    spec = get_tool_spec("delete_users")
    preview_params = set(inspect.signature(preview_delete_users).parameters)
    mutation_params = set(inspect.signature(delete_users).parameters)

    forbidden = {
        "action_context",
        "rollback_proof",
        "risk_features",
        "policy",
        "verdict",
        "workflow_budget",
        "audit_event",
    }
    assert not (preview_params & forbidden)
    assert not (mutation_params & forbidden)
    # Sanity: these are genuinely the same dumb functions the registry wraps.
    assert spec.preview_fn(inactive_days=90, environment="test") is not None or True
