"""Slice 25.1 tests: corrected feature-flag blast-radius accounting.

Blast radius is the number of users whose effective feature exposure
changes (abs(requested_exposed_users - current_exposed_users)), not
simply audience * requested_rollout_percentage // 100. This file proves
enabling/disabling/increasing/decreasing rollout are all accounted for
symmetrically, that preview and mutation agree exactly (same shared
EnvironmentChangePlan), and that configuration changes with zero
exposure delta are still honestly distinguished from exact no-ops.
"""

import json

import pytest

import proofgate.audit as audit_module
from operations.database import reset_working_db
from operations.feature_flags import (
    FEATURE_FLAGS_PRISTINE_PATH,
    FEATURE_FLAGS_WORKING_PATH,
    preview_set_feature_flag,
    reset_feature_flags_working,
    set_feature_flag,
)
from proofgate.budgets import get_last_postcondition_result, get_workflow_budget, reset_workflow_state
from proofgate.core import guarded_execute
from proofgate.models import ActionContext

TEST_AUDIENCE = 92

DISABLE_TEST_INSTRUCTION = "Disable the new checkout flow for the test environment."
BROAD_ENABLE_INSTRUCTION = "Enable the new checkout flow for test users."
CORRECTED_ENABLE_INSTRUCTION = "Enable the new checkout flow for the test environment."


@pytest.fixture(autouse=True)
def _reset_everything():
    reset_working_db()
    reset_feature_flags_working()
    yield
    reset_working_db()
    reset_feature_flags_working()


def _action_context(workflow_id: str, instruction: str) -> ActionContext:
    return ActionContext(
        workflow_id=workflow_id, requesting_user="demo-user", agent_id="demo-agent", original_instruction=instruction
    )


def _working_state() -> dict:
    return json.loads(FEATURE_FLAGS_WORKING_PATH.read_text())


def _audit_events() -> list[dict]:
    path = audit_module.DEFAULT_AUDIT_PATH
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().splitlines() if line]


def _seed_state(enabled: bool, rollout: int) -> None:
    """Get working state to (enabled, rollout) for the test environment,
    from a clean pristine reset, via the real mutation function."""
    reset_feature_flags_working()
    if enabled or rollout:
        set_feature_flag(flag_name="new_checkout", enabled=enabled, environment="test", rollout_percentage=rollout)


# ---------------------------------------------------------------------------
# Effective-exposure calculation (Part A/B)
# ---------------------------------------------------------------------------

TRANSITIONS = [
    # (label, current_enabled, current_rollout, requested_enabled, requested_rollout, expected_affected)
    ("disabled_0_to_enabled_100", False, 0, True, 100, 92),
    ("enabled_100_to_disabled_0", True, 100, False, 0, 92),
    ("enabled_100_to_enabled_50", True, 100, True, 50, 46),
    ("enabled_50_to_enabled_100", True, 50, True, 100, 46),
    ("disabled_0_to_enabled_33", False, 0, True, 33, 30),
    ("enabled_33_to_disabled_0", True, 33, False, 0, 30),
    ("disabled_80_to_disabled_0", False, 80, False, 0, 0),
    ("enabled_100_to_enabled_100", True, 100, True, 100, 0),
]


@pytest.mark.parametrize("label,cur_enabled,cur_rollout,req_enabled,req_rollout,expected", TRANSITIONS)
def test_effective_exposure_preview(label, cur_enabled, cur_rollout, req_enabled, req_rollout, expected):
    _seed_state(cur_enabled, cur_rollout)
    impact = preview_set_feature_flag(
        flag_name="new_checkout", enabled=req_enabled, environment="test", rollout_percentage=req_rollout
    )
    assert impact.environment_counts["test"] == expected, label
    assert impact.estimated_count == expected, label


@pytest.mark.parametrize("label,cur_enabled,cur_rollout,req_enabled,req_rollout,expected", TRANSITIONS)
def test_effective_exposure_mutation(label, cur_enabled, cur_rollout, req_enabled, req_rollout, expected):
    _seed_state(cur_enabled, cur_rollout)
    result = set_feature_flag(
        flag_name="new_checkout", enabled=req_enabled, environment="test", rollout_percentage=req_rollout
    )
    assert result.test_affected == expected, label
    assert result.production_affected == 0, label


# ---------------------------------------------------------------------------
# Preview/mutation consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,cur_enabled,cur_rollout,req_enabled,req_rollout,expected", TRANSITIONS)
def test_preview_and_mutation_agree(label, cur_enabled, cur_rollout, req_enabled, req_rollout, expected):
    _seed_state(cur_enabled, cur_rollout)
    impact = preview_set_feature_flag(
        flag_name="new_checkout", enabled=req_enabled, environment="test", rollout_percentage=req_rollout
    )
    result = set_feature_flag(
        flag_name="new_checkout", enabled=req_enabled, environment="test", rollout_percentage=req_rollout
    )
    assert impact.environment_counts["test"] == result.test_affected == expected, label


# ---------------------------------------------------------------------------
# Mixed broad-state aggregation (Part D)
# ---------------------------------------------------------------------------


def test_mixed_broad_state_one_env_no_op_other_changing():
    reset_feature_flags_working()
    set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    # production remains disabled@0

    impact = preview_set_feature_flag(flag_name="new_checkout", enabled=True, environment=None, rollout_percentage=100)
    assert impact.environment_counts == {"test": 0, "production": 9981}
    assert impact.estimated_count == 9981

    result = set_feature_flag(flag_name="new_checkout", enabled=True, environment=None, rollout_percentage=100)
    assert result.test_affected == 0
    assert result.production_affected == 9981
    assert result.affected_count == 9981


def test_mixed_broad_state_both_environments_changing_independently():
    reset_feature_flags_working()
    set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    set_feature_flag(flag_name="new_checkout", enabled=True, environment="production", rollout_percentage=100)

    # Now disable both via one broad request.
    impact = preview_set_feature_flag(flag_name="new_checkout", enabled=False, environment=None, rollout_percentage=0)
    assert impact.environment_counts == {"test": 92, "production": 9981}
    assert impact.estimated_count == 10073


# ---------------------------------------------------------------------------
# Configuration state vs. exposure state (Part B)
# ---------------------------------------------------------------------------


def test_exact_no_op_produces_no_file_mutation_and_no_version_bump():
    reset_feature_flags_working()
    set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    before_mtime = FEATURE_FLAGS_WORKING_PATH.stat().st_mtime_ns
    before_state = _working_state()

    result = set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)

    after_mtime = FEATURE_FLAGS_WORKING_PATH.stat().st_mtime_ns
    after_state = _working_state()
    assert result.test_affected == 0
    assert before_mtime == after_mtime  # no write occurred at all
    assert before_state == after_state
    assert after_state["new_checkout"]["test"]["version"] == 2  # unchanged from the seeding call


def test_configuration_change_with_zero_exposure_delta_still_writes_and_bumps_version():
    reset_feature_flags_working()
    set_feature_flag(flag_name="new_checkout", enabled=False, environment="test", rollout_percentage=80)
    before_state = _working_state()
    assert before_state["new_checkout"]["test"] == {"enabled": False, "rollout_percentage": 80, "version": 2}

    result = set_feature_flag(flag_name="new_checkout", enabled=False, environment="test", rollout_percentage=0)

    after_state = _working_state()
    assert result.test_affected == 0  # no user's effective exposure changed (disabled -> disabled)
    assert after_state["new_checkout"]["test"] == {"enabled": False, "rollout_percentage": 0, "version": 3}
    assert after_state != before_state  # the file did change


def test_changed_exposure_writes_and_bumps_version_with_correct_affected_count():
    reset_feature_flags_working()
    set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)

    result = set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=50)

    after_state = _working_state()
    assert result.test_affected == 46
    assert after_state["new_checkout"]["test"] == {"enabled": True, "rollout_percentage": 50, "version": 3}


def test_pristine_state_unchanged_across_all_transitions():
    before = FEATURE_FLAGS_PRISTINE_PATH.read_text()
    for _, cur_enabled, cur_rollout, req_enabled, req_rollout, _ in TRANSITIONS:
        _seed_state(cur_enabled, cur_rollout)
        set_feature_flag(flag_name="new_checkout", enabled=req_enabled, environment="test", rollout_percentage=req_rollout)
    after = FEATURE_FLAGS_PRISTINE_PATH.read_text()
    assert before == after


def test_users_database_unchanged_across_disable_transition():
    import hashlib

    from operations.database import WORKING_DB_PATH

    reset_working_db()
    before_hash = hashlib.sha256(WORKING_DB_PATH.read_bytes()).hexdigest()

    _seed_state(True, 100)
    set_feature_flag(flag_name="new_checkout", enabled=False, environment="test", rollout_percentage=0)

    after_hash = hashlib.sha256(WORKING_DB_PATH.read_bytes()).hexdigest()
    assert before_hash == after_hash


# ---------------------------------------------------------------------------
# Budget (Part E)
# ---------------------------------------------------------------------------


def test_budget_full_disable_consumes_92():
    workflow_id = "wf-ff-exp-budget-1"
    reset_workflow_state(workflow_id)
    _seed_state(True, 100)

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, DISABLE_TEST_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": False, "environment": "test", "rollout_percentage": 0},
        None,
    )
    assert result.verdict == "ALLOW"
    assert get_workflow_budget(workflow_id).rows_mutated == 92
    assert get_workflow_budget(workflow_id).production_rows_mutated == 0


def test_budget_reduce_100_to_50_consumes_46():
    workflow_id = "wf-ff-exp-budget-2"
    reset_workflow_state(workflow_id)
    _seed_state(True, 100)

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, "Reduce the new checkout flow rollout in the test environment."),
        {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 50},
        None,
    )
    assert result.verdict == "ALLOW"
    assert get_workflow_budget(workflow_id).rows_mutated == 46


def test_budget_increase_50_to_100_consumes_46():
    workflow_id = "wf-ff-exp-budget-3"
    reset_workflow_state(workflow_id)
    _seed_state(True, 50)

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, "Increase the new checkout flow rollout in the test environment."),
        {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100},
        None,
    )
    assert result.verdict == "ALLOW"
    assert get_workflow_budget(workflow_id).rows_mutated == 46


def test_budget_zero_exposure_configuration_change_consumes_zero():
    workflow_id = "wf-ff-exp-budget-4"
    reset_workflow_state(workflow_id)
    _seed_state(False, 80)

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, "Update the new checkout flow configuration for the test environment."),
        {"flag_name": "new_checkout", "enabled": False, "environment": "test", "rollout_percentage": 0},
        None,
    )
    assert result.verdict == "ALLOW"
    assert get_workflow_budget(workflow_id).rows_mutated == 0


def test_budget_exact_no_op_consumes_zero():
    workflow_id = "wf-ff-exp-budget-5"
    reset_workflow_state(workflow_id)
    _seed_state(True, 100)

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, "Update the new checkout flow configuration for the test environment."),
        {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100},
        None,
    )
    assert result.verdict == "ALLOW"
    assert get_workflow_budget(workflow_id).rows_mutated == 0


# ---------------------------------------------------------------------------
# Postcondition (Part F)
# ---------------------------------------------------------------------------


def test_postcondition_disable_full_rollout_verified():
    workflow_id = "wf-ff-exp-post-1"
    reset_workflow_state(workflow_id)
    _seed_state(True, 100)

    guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, DISABLE_TEST_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": False, "environment": "test", "rollout_percentage": 0},
        None,
    )
    postcondition = get_last_postcondition_result(workflow_id)
    assert postcondition.predicted_count == 92
    assert postcondition.actual_count == 92
    assert postcondition.status == "VERIFIED"


def test_postcondition_reduce_100_to_50_verified():
    workflow_id = "wf-ff-exp-post-2"
    reset_workflow_state(workflow_id)
    _seed_state(True, 100)

    guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, "Reduce the new checkout flow rollout in the test environment."),
        {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 50},
        None,
    )
    postcondition = get_last_postcondition_result(workflow_id)
    assert postcondition.predicted_count == 46
    assert postcondition.actual_count == 46
    assert postcondition.status == "VERIFIED"


def test_postcondition_zero_exposure_configuration_change_verified():
    workflow_id = "wf-ff-exp-post-3"
    reset_workflow_state(workflow_id)
    _seed_state(False, 80)

    guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, "Update the new checkout flow configuration for the test environment."),
        {"flag_name": "new_checkout", "enabled": False, "environment": "test", "rollout_percentage": 0},
        None,
    )
    postcondition = get_last_postcondition_result(workflow_id)
    assert postcondition.predicted_count == 0
    assert postcondition.actual_count == 0
    assert postcondition.status == "VERIFIED"


# ---------------------------------------------------------------------------
# Part G: Slice 25 primary outcomes unchanged
# ---------------------------------------------------------------------------


def test_broad_enable_from_pristine_unchanged():
    workflow_id = "wf-ff-exp-g1"
    reset_workflow_state(workflow_id)
    reset_feature_flags_working()

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, BROAD_ENABLE_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": None, "rollout_percentage": 100},
        None,
    )
    assert result.verdict == "BLOCK"
    assert result.executed is False
    events = _audit_events()
    assert events[-1]["impact_envelope"]["estimated_count"] == 10073
    assert events[-1]["impact_envelope"]["environment_counts"] == {"test": 92, "production": 9981}
    from proofgate.policy import RULE_INTENT_BOUNDARY, RULE_WORKFLOW_BUDGET

    assert {r.rule_id for r in result.triggered_rules} == {RULE_INTENT_BOUNDARY, RULE_WORKFLOW_BUDGET}


def test_corrected_enable_from_pristine_unchanged():
    workflow_id = "wf-ff-exp-g2"
    reset_workflow_state(workflow_id)
    reset_feature_flags_working()

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, CORRECTED_ENABLE_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100},
        None,
    )
    assert result.verdict == "ALLOW"
    assert result.executed is True
    postcondition = get_last_postcondition_result(workflow_id)
    assert postcondition.actual_count == 92
    assert postcondition.production_affected == 0
    assert postcondition.status == "VERIFIED"
    assert get_workflow_budget(workflow_id).rows_mutated == 92


# ---------------------------------------------------------------------------
# Part H: governed disable demonstration
# ---------------------------------------------------------------------------


def test_governed_disable_from_enabled_100_percent():
    workflow_id = "wf-ff-exp-disable"
    reset_workflow_state(workflow_id)
    reset_feature_flags_working()
    set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, DISABLE_TEST_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": False, "environment": "test", "rollout_percentage": 0},
        None,
    )

    assert result.verdict == "ALLOW"
    assert result.executed is True

    postcondition = get_last_postcondition_result(workflow_id)
    assert postcondition.predicted_count == 92
    assert postcondition.actual_count == 92
    assert postcondition.production_affected == 0
    assert postcondition.status == "VERIFIED"

    budget = get_workflow_budget(workflow_id)
    assert budget.rows_mutated == 92
    assert budget.production_rows_mutated == 0

    state = _working_state()
    assert state["new_checkout"]["test"]["enabled"] is False
    assert state["new_checkout"]["test"]["rollout_percentage"] == 0
    assert state["new_checkout"]["production"] == {"enabled": False, "rollout_percentage": 0, "version": 1}
