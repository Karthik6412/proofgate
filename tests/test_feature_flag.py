"""Slice 25 tests: set_feature_flag, a genuinely non-user-management
consequential tool -- a filesystem-backed feature-flag configuration
resource, governed by the exact same registry, guarded execution
boundary, policy engine, selector hashing, workflow budget, postcondition
verification, audit schema, and repair guidance as delete_users/
deactivate_users, without any tool-specific special casing anywhere in
that shared machinery.
"""

import ast
import json
from pathlib import Path
from unittest import mock

import pytest

import operations.feature_flags as feature_flags_module
import proofgate.audit as audit_module
from operations.database import PRISTINE_DB_PATH, WORKING_DB_PATH, reset_working_db
from operations.feature_flags import (
    FEATURE_FLAG_AUDIENCE_PATH,
    FEATURE_FLAGS_PRISTINE_PATH,
    FEATURE_FLAGS_WORKING_PATH,
    load_audience,
    preview_set_feature_flag,
    reset_feature_flags_working,
    set_feature_flag,
)
from proofgate.budgets import get_last_postcondition_result, get_workflow_budget, reset_workflow_state
from proofgate.core import guarded_execute
from proofgate.models import ActionContext
from proofgate.policy import RULE_INTENT_BOUNDARY, RULE_RECOVERY_PROOF, RULE_UNKNOWN_IMPACT, RULE_WORKFLOW_BUDGET
from proofgate.registry import GuardedToolSpec, _REGISTRY, get_tool_spec
from proofgate.selector import compute_selector_hash

BROAD_FF_INSTRUCTION = "Enable the new checkout flow for test users."
CORRECTED_FF_INSTRUCTION = "Enable the new checkout flow for the test environment."
PRODUCTION_FF_INSTRUCTION = "Enable the new checkout flow for all production users."


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


def _fresh(workflow_id: str) -> None:
    reset_workflow_state(workflow_id)


def _audit_events() -> list[dict]:
    path = audit_module.DEFAULT_AUDIT_PATH
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().splitlines() if line]


def _working_state() -> dict:
    return json.loads(FEATURE_FLAGS_WORKING_PATH.read_text())


# ---------------------------------------------------------------------------
# Resource and fixture tests
# ---------------------------------------------------------------------------


def test_pristine_feature_flag_json_is_valid():
    state = json.loads(FEATURE_FLAGS_PRISTINE_PATH.read_text())
    assert state == {
        "new_checkout": {
            "test": {"enabled": False, "rollout_percentage": 0, "version": 1},
            "production": {"enabled": False, "rollout_percentage": 0, "version": 1},
        }
    }


def test_working_feature_flag_json_is_valid_after_reset():
    reset_feature_flags_working()
    assert _working_state() == json.loads(FEATURE_FLAGS_PRISTINE_PATH.read_text())


def test_audience_json_is_valid_and_matches_documented_counts():
    audience = load_audience()
    assert audience == {"test": 92, "production": 9981}
    assert audience["test"] == 92
    assert audience["production"] == 9981
    assert audience["test"] + audience["production"] == 10073


def test_reset_restores_working_state_after_mutation():
    set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    assert _working_state()["new_checkout"]["test"]["enabled"] is True

    reset_feature_flags_working()
    assert _working_state() == json.loads(FEATURE_FLAGS_PRISTINE_PATH.read_text())


def test_reset_leaves_pristine_state_unchanged():
    before = FEATURE_FLAGS_PRISTINE_PATH.read_text()
    set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    reset_feature_flags_working()
    after = FEATURE_FLAGS_PRISTINE_PATH.read_text()
    assert before == after


def test_reset_refuses_to_target_pristine_as_working():
    with pytest.raises(ValueError):
        reset_feature_flags_working(pristine_path=FEATURE_FLAGS_PRISTINE_PATH, working_path=FEATURE_FLAGS_PRISTINE_PATH)


def test_reset_is_safe_to_call_repeatedly():
    reset_feature_flags_working()
    reset_feature_flags_working()
    reset_feature_flags_working()
    assert _working_state() == json.loads(FEATURE_FLAGS_PRISTINE_PATH.read_text())


def test_feature_flags_module_never_imports_operations_database_or_actions():
    source = Path(feature_flags_module.__file__).read_text()
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert "operations.database" not in imported
    assert "operations.actions" not in imported


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


def test_registry_has_exactly_three_tools():
    assert set(_REGISTRY.keys()) == {"delete_users", "deactivate_users", "set_feature_flag"}


def test_registry_third_tool_metadata_is_correct():
    spec = get_tool_spec("set_feature_flag")
    assert isinstance(spec, GuardedToolSpec)
    assert spec.tool_name == "set_feature_flag"
    assert spec.resource == "feature_flags"
    assert spec.resource != "users"
    assert spec.selector_argument_names == ("flag_name", "enabled", "environment", "rollout_percentage")
    assert spec.hard_delete is False
    assert spec.reversibility == "reversible"


def test_registry_feature_flag_preview_fn_matches_real_preview_function():
    reset_feature_flags_working()
    via_registry = get_tool_spec("set_feature_flag").preview_fn(
        flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100
    )
    direct = preview_set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    # generated_at legitimately differs by real wall-clock microseconds
    # between the two separate calls; every other field must match exactly.
    assert via_registry.model_dump(exclude={"generated_at"}) == direct.model_dump(exclude={"generated_at"})


def test_registry_feature_flag_mutation_fn_matches_real_mutation_function():
    reset_feature_flags_working()
    result = get_tool_spec("set_feature_flag").mutation_fn(
        flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100
    )
    assert result.affected_count == 92
    assert result.test_affected == 92
    assert result.production_affected == 0


def test_registry_existing_two_entries_remain_unchanged():
    delete_spec = get_tool_spec("delete_users")
    assert delete_spec.resource == "users"
    assert delete_spec.hard_delete is True
    assert delete_spec.reversibility == "irreversible_without_snapshot"
    assert delete_spec.selector_argument_names == ("inactive_days", "environment")

    deactivate_spec = get_tool_spec("deactivate_users")
    assert deactivate_spec.resource == "users"
    assert deactivate_spec.hard_delete is False
    assert deactivate_spec.reversibility == "reversible"
    assert deactivate_spec.selector_argument_names == ("inactive_days", "environment")


# ---------------------------------------------------------------------------
# Selector-hash tests
# ---------------------------------------------------------------------------


def test_feature_flag_selector_uses_the_canonical_hash_function():
    reset_feature_flags_working()
    impact = preview_set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    expected = compute_selector_hash(
        {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100}
    )
    assert impact.selector_hash == expected


def test_equivalent_feature_flag_selectors_produce_the_same_hash():
    a = compute_selector_hash({"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100})
    b = compute_selector_hash({"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100})
    assert a == b


def test_different_feature_flag_selectors_produce_different_hashes():
    base = {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100}
    base_hash = compute_selector_hash(base)
    assert compute_selector_hash({**base, "flag_name": "other_flag"}) != base_hash
    assert compute_selector_hash({**base, "enabled": False}) != base_hash
    assert compute_selector_hash({**base, "environment": "production"}) != base_hash
    assert compute_selector_hash({**base, "rollout_percentage": 50}) != base_hash


def test_existing_delete_deactivate_selector_hashes_unchanged():
    assert compute_selector_hash({"inactive_days": 90, "environment": "test"}) == compute_selector_hash(
        {"inactive_days": 90, "environment": "test"}
    )


def test_only_one_canonical_selector_hash_implementation_exists():
    import proofgate.selector as selector_module

    source = Path(selector_module.__file__).read_text()
    assert source.count("def compute_selector_hash") == 1
    assert source.count("def canonical_selector_json") == 1
    ff_source = Path(feature_flags_module.__file__).read_text()
    assert "def compute_selector_hash" not in ff_source
    assert "def canonical_selector_json" not in ff_source


# ---------------------------------------------------------------------------
# Preview tests
# ---------------------------------------------------------------------------


def test_broad_rollout_preview_matches_exact_demo_counts():
    reset_feature_flags_working()
    impact = preview_set_feature_flag(flag_name="new_checkout", enabled=True, environment=None, rollout_percentage=100)
    assert impact.estimated_count == 10073
    assert impact.environment_counts == {"test": 92, "production": 9981}
    assert impact.hard_delete is False
    assert impact.reversibility == "reversible"


def test_test_only_rollout_preview_matches_exact_demo_counts():
    reset_feature_flags_working()
    impact = preview_set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    assert impact.estimated_count == 92
    assert impact.environment_counts == {"test": 92, "production": 0}


def test_production_only_rollout_preview_matches_exact_demo_counts():
    reset_feature_flags_working()
    impact = preview_set_feature_flag(flag_name="new_checkout", enabled=True, environment="production", rollout_percentage=100)
    assert impact.estimated_count == 9981
    assert impact.environment_counts == {"test": 0, "production": 9981}


def test_partial_rollout_uses_deterministic_floor_rounding():
    reset_feature_flags_working()
    impact = preview_set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=33)
    # floor(92 * 33 / 100) = floor(30.36) = 30, never rounded up to 31.
    assert impact.environment_counts["test"] == 30
    assert impact.estimated_count == 30


def test_no_op_preview_is_zero_impact():
    reset_feature_flags_working()
    impact = preview_set_feature_flag(flag_name="new_checkout", enabled=False, environment="test", rollout_percentage=0)
    assert impact.estimated_count == 0
    assert impact.environment_counts == {"test": 0, "production": 0}


def test_mixed_broad_request_handles_one_no_op_and_one_changed_environment():
    reset_feature_flags_working()
    set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)

    impact = preview_set_feature_flag(flag_name="new_checkout", enabled=True, environment=None, rollout_percentage=100)
    assert impact.environment_counts == {"test": 0, "production": 9981}  # test already matches: no-op
    assert impact.estimated_count == 9981


def test_unknown_flag_is_rejected():
    reset_feature_flags_working()
    with pytest.raises(ValueError):
        preview_set_feature_flag(flag_name="does_not_exist", enabled=True, environment="test", rollout_percentage=100)


def test_invalid_environment_is_rejected():
    reset_feature_flags_working()
    with pytest.raises(ValueError):
        preview_set_feature_flag(flag_name="new_checkout", enabled=True, environment="staging", rollout_percentage=100)


def test_percentage_below_zero_is_rejected():
    reset_feature_flags_working()
    with pytest.raises(ValueError):
        preview_set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=-1)


def test_percentage_above_100_is_rejected():
    reset_feature_flags_working()
    with pytest.raises(ValueError):
        preview_set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=101)


def test_preview_never_mutates_working_state():
    reset_feature_flags_working()
    before = FEATURE_FLAGS_WORKING_PATH.read_text()
    preview_set_feature_flag(flag_name="new_checkout", enabled=True, environment=None, rollout_percentage=100)
    after = FEATURE_FLAGS_WORKING_PATH.read_text()
    assert before == after


# ---------------------------------------------------------------------------
# Operations tests
# ---------------------------------------------------------------------------


def test_corrected_test_rollout_updates_real_working_json():
    reset_feature_flags_working()
    set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    state = _working_state()
    assert state["new_checkout"]["test"]["enabled"] is True
    assert state["new_checkout"]["test"]["rollout_percentage"] == 100


def test_corrected_test_rollout_leaves_production_unchanged():
    reset_feature_flags_working()
    set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    state = _working_state()
    assert state["new_checkout"]["production"] == {"enabled": False, "rollout_percentage": 0, "version": 1}


def test_corrected_test_rollout_leaves_pristine_unchanged():
    before = FEATURE_FLAGS_PRISTINE_PATH.read_text()
    reset_feature_flags_working()
    set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    after = FEATURE_FLAGS_PRISTINE_PATH.read_text()
    assert before == after


def test_version_increments_only_for_changed_environment():
    reset_feature_flags_working()
    set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    state = _working_state()
    assert state["new_checkout"]["test"]["version"] == 2
    assert state["new_checkout"]["production"]["version"] == 1


def test_mutation_affected_counts_are_honest():
    reset_feature_flags_working()
    result = set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    assert result.affected_count == 92
    assert result.test_affected == 92
    assert result.production_affected == 0


def test_no_op_mutation_produces_zero_affected_and_no_version_bump():
    reset_feature_flags_working()
    result = set_feature_flag(flag_name="new_checkout", enabled=False, environment="test", rollout_percentage=0)
    assert result.affected_count == 0
    state = _working_state()
    assert state["new_checkout"]["test"]["version"] == 1


def test_broad_operations_mutation_updates_both_environments():
    reset_feature_flags_working()
    result = set_feature_flag(flag_name="new_checkout", enabled=True, environment=None, rollout_percentage=100)
    assert result.affected_count == 10073
    assert result.test_affected == 92
    assert result.production_affected == 9981
    state = _working_state()
    assert state["new_checkout"]["test"]["enabled"] is True
    assert state["new_checkout"]["production"]["enabled"] is True
    assert state["new_checkout"]["test"]["version"] == 2
    assert state["new_checkout"]["production"]["version"] == 2


def test_malformed_working_json_fails_safely():
    reset_feature_flags_working()
    FEATURE_FLAGS_WORKING_PATH.write_text("{not valid json")
    with pytest.raises(ValueError):
        preview_set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)
    with pytest.raises(ValueError):
        set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)


def test_failed_atomic_write_preserves_prior_valid_working_file():
    reset_feature_flags_working()
    before = FEATURE_FLAGS_WORKING_PATH.read_text()

    with mock.patch("operations.feature_flags.os.replace", side_effect=OSError("simulated failure")):
        with pytest.raises(OSError):
            set_feature_flag(flag_name="new_checkout", enabled=True, environment="test", rollout_percentage=100)

    after = FEATURE_FLAGS_WORKING_PATH.read_text()
    assert after == before  # the prior valid file was never replaced

    leftover_temp_files = list(FEATURE_FLAGS_WORKING_PATH.parent.glob(FEATURE_FLAGS_WORKING_PATH.name + ".tmp-*"))
    assert leftover_temp_files == []  # temp file cleaned up despite the failure


def test_users_database_untouched_by_feature_flag_operations():
    import hashlib

    reset_working_db()
    before_hash = hashlib.sha256(WORKING_DB_PATH.read_bytes()).hexdigest()

    reset_feature_flags_working()
    set_feature_flag(flag_name="new_checkout", enabled=True, environment=None, rollout_percentage=100)

    after_hash = hashlib.sha256(WORKING_DB_PATH.read_bytes()).hexdigest()
    assert before_hash == after_hash


def test_feature_flags_operations_contains_no_intent_policy_proof_budget_audit_or_network_logic():
    source = Path(feature_flags_module.__file__).read_text()
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden = {"proofgate.policy", "proofgate.core", "proofgate.budgets", "proofgate.audit", "proofgate.proofs", "requests", "httpx", "urllib"}
    assert not (set(imported) & forbidden)
    # Neither function signature accepts these -- checked structurally,
    # not by a docstring-fragile text search (this module's own comments
    # legitimately discuss ActionContext/RollbackProof in prose).
    import inspect

    assert "action_context" not in inspect.signature(feature_flags_module.set_feature_flag).parameters
    assert "rollback_proof" not in inspect.signature(feature_flags_module.set_feature_flag).parameters


# ---------------------------------------------------------------------------
# Governance tests (through guarded_execute)
# ---------------------------------------------------------------------------


def test_broad_feature_flag_request_is_blocked_with_exact_impact_and_rules():
    workflow_id = "wf-ff-gov-broad"
    _fresh(workflow_id)
    reset_feature_flags_working()

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, BROAD_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": None, "rollout_percentage": 100},
        None,
    )

    assert result.verdict == "BLOCK"
    assert result.executed is False
    triggered_ids = {r.rule_id for r in result.triggered_rules}
    assert triggered_ids == {RULE_INTENT_BOUNDARY, RULE_WORKFLOW_BUDGET}
    assert RULE_RECOVERY_PROOF not in triggered_ids
    assert RULE_UNKNOWN_IMPACT not in triggered_ids

    state = _working_state()
    assert state == json.loads(FEATURE_FLAGS_PRISTINE_PATH.read_text())

    events = _audit_events()
    assert len(events) == 1
    assert events[0]["tool_name"] == "set_feature_flag"
    assert events[0]["impact_envelope"]["estimated_count"] == 10073
    assert events[0]["impact_envelope"]["environment_counts"] == {"test": 92, "production": 9981}


def test_corrected_feature_flag_request_is_allowed_and_executes():
    workflow_id = "wf-ff-gov-corrected"
    _fresh(workflow_id)
    reset_feature_flags_working()

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, CORRECTED_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100},
        None,
    )

    assert result.verdict == "ALLOW"
    assert result.executed is True
    assert result.triggered_rules == []

    postcondition = get_last_postcondition_result(workflow_id)
    assert postcondition.status == "VERIFIED"
    assert postcondition.predicted_count == 92
    assert postcondition.actual_count == 92
    assert postcondition.production_affected == 0

    budget = get_workflow_budget(workflow_id)
    assert budget.rows_mutated == 92
    assert budget.production_rows_mutated == 0

    state = _working_state()
    assert state["new_checkout"]["test"]["enabled"] is True
    assert state != json.loads(FEATURE_FLAGS_PRISTINE_PATH.read_text())

    events = _audit_events()
    assert len(events) == 1


def test_explicit_production_request_is_blocked_only_by_workflow_budget():
    workflow_id = "wf-ff-gov-prod"
    _fresh(workflow_id)
    reset_feature_flags_working()

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, PRODUCTION_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": "production", "rollout_percentage": 100},
        None,
    )

    triggered_ids = {r.rule_id for r in result.triggered_rules}
    assert RULE_INTENT_BOUNDARY not in triggered_ids
    assert RULE_WORKFLOW_BUDGET in triggered_ids
    assert result.executed is False

    state = _working_state()
    assert state["new_checkout"]["production"] == {"enabled": False, "rollout_percentage": 0, "version": 1}


def test_no_op_request_through_guarded_execute():
    workflow_id = "wf-ff-gov-noop"
    _fresh(workflow_id)
    reset_feature_flags_working()

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, CORRECTED_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": False, "environment": "test", "rollout_percentage": 0},
        None,
    )

    assert result.verdict == "ALLOW"
    assert result.executed is True
    postcondition = get_last_postcondition_result(workflow_id)
    assert postcondition.predicted_count == 0
    assert postcondition.actual_count == 0
    assert postcondition.status == "VERIFIED"
    assert get_workflow_budget(workflow_id).rows_mutated == 0


# ---------------------------------------------------------------------------
# Recovery-proof tests
# ---------------------------------------------------------------------------


def test_feature_flag_never_triggers_recovery_proof_rule():
    workflow_id = "wf-ff-proof"
    _fresh(workflow_id)
    reset_feature_flags_working()

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, BROAD_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": None, "rollout_percentage": 100},
        None,
    )
    assert RULE_RECOVERY_PROOF not in {r.rule_id for r in result.triggered_rules}


def test_feature_flag_proof_status_follows_existing_reversible_contract():
    workflow_id = "wf-ff-proof-status"
    _fresh(workflow_id)
    reset_feature_flags_working()

    guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, CORRECTED_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100},
        None,
    )
    events = _audit_events()
    assert events[0]["proof_status"] == "MISSING"  # same raw contract as deactivate_users -- no proof supplied, none required


def test_no_snapshot_created_for_feature_flag_action():
    from operations.snapshots import SNAPSHOTS_DIR

    before = set(SNAPSHOTS_DIR.glob("*")) if SNAPSHOTS_DIR.exists() else set()
    workflow_id = "wf-ff-no-snapshot"
    _fresh(workflow_id)
    reset_feature_flags_working()
    guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, CORRECTED_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100},
        None,
    )
    after = set(SNAPSHOTS_DIR.glob("*")) if SNAPSHOTS_DIR.exists() else set()
    assert after == before


def test_feature_flag_never_enters_automatic_rollback():
    """hard_delete=False for set_feature_flag means
    proofgate.rollback.resolve_final_postcondition's own eligibility
    check (spec.hard_delete) structurally excludes it -- verified here by
    confirming a genuinely mismatched postcondition still reports
    MISMATCH honestly rather than being silently resolved."""
    from proofgate.models import PostconditionResult
    from proofgate.registry import get_tool_spec
    from proofgate.rollback import resolve_final_postcondition

    mismatch = PostconditionResult(predicted_count=92, actual_count=50, production_affected=0, status="MISMATCH")
    result = resolve_final_postcondition(
        spec=get_tool_spec("set_feature_flag"),
        rollback_proof=None,
        rollback_proof_valid=False,
        selector_arguments={"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100},
        postcondition_result=mismatch,
    )
    assert result.status == "MANUAL_REVIEW_REQUIRED"
    assert result.status != "ROLLED_BACK"


# ---------------------------------------------------------------------------
# Repair-guidance tests
# ---------------------------------------------------------------------------


def test_broad_feature_flag_repair_names_set_feature_flag():
    workflow_id = "wf-ff-repair"
    _fresh(workflow_id)
    reset_feature_flags_working()

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, BROAD_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": None, "rollout_percentage": 100},
        None,
    )

    assert result.suggested_repairs == [
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


def test_feature_flag_repair_never_names_delete_or_deactivate():
    workflow_id = "wf-ff-repair-2"
    _fresh(workflow_id)
    reset_feature_flags_working()

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, BROAD_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": None, "rollout_percentage": 100},
        None,
    )
    repair_tool = result.suggested_repairs[0]["tool"]
    assert repair_tool not in ("delete_users", "deactivate_users")


def test_feature_flag_repair_does_not_suggest_snapshot_or_proof():
    workflow_id = "wf-ff-repair-3"
    _fresh(workflow_id)
    reset_feature_flags_working()

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, BROAD_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": None, "rollout_percentage": 100},
        None,
    )
    next_step = result.suggested_repairs[0]["next_step"]
    assert next_step != "create_snapshot"
    assert "proof" not in next_step
    assert "snapshot" not in next_step


def test_repair_guidance_does_not_affect_verdict():
    workflow_id = "wf-ff-repair-4"
    _fresh(workflow_id)
    reset_feature_flags_working()

    result = guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, BROAD_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": None, "rollout_percentage": 100},
        None,
    )
    # The suggested repair exists, but the actual verdict is still BLOCK --
    # suggested_repairs is non-authoritative regardless of its content.
    assert result.verdict == "BLOCK"


# ---------------------------------------------------------------------------
# Policy/core genericity tests (AST-based, mirroring Slice 17's pattern)
# ---------------------------------------------------------------------------


def _assert_no_tool_identity_literal_comparison(module_path: Path) -> None:
    source = module_path.read_text()
    assert "set_feature_flag" not in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and comparator.value in (
                    "delete_users",
                    "deactivate_users",
                    "set_feature_flag",
                ):
                    raise AssertionError(f"{module_path.name} contains a tool-name literal comparison")


def test_policy_contains_no_set_feature_flag_comparison():
    import proofgate.policy as policy_module

    _assert_no_tool_identity_literal_comparison(Path(policy_module.__file__))


def test_core_contains_no_feature_flag_tool_specific_branch():
    import proofgate.core as core_module

    _assert_no_tool_identity_literal_comparison(Path(core_module.__file__))


def test_budgets_contains_no_feature_flag_branch():
    import proofgate.budgets as budgets_module

    _assert_no_tool_identity_literal_comparison(Path(budgets_module.__file__))


def test_proofs_contains_no_feature_flag_branch():
    import proofgate.proofs as proofs_module

    _assert_no_tool_identity_literal_comparison(Path(proofs_module.__file__))


def test_postcondition_contains_no_feature_flag_branch():
    import proofgate.postcondition as postcondition_module

    _assert_no_tool_identity_literal_comparison(Path(postcondition_module.__file__))


def test_audit_module_contains_no_feature_flag_branch():
    _assert_no_tool_identity_literal_comparison(Path(audit_module.__file__))


def test_no_policy_rule_reads_feature_flag_files():
    import proofgate.policy as policy_module

    source = Path(policy_module.__file__).read_text()
    assert "feature_flag" not in source.lower()
    assert ".json" not in source


# ---------------------------------------------------------------------------
# MCP tests
# ---------------------------------------------------------------------------


def test_mcp_discovery_returns_exactly_three_tools():
    import proofgate.mcp_server as mcp_server

    assert set(mcp_server._PUBLIC_MCP_TOOLS) == {"delete_users", "deactivate_users", "set_feature_flag"}


def test_mcp_delete_and_deactivate_schema_unchanged():
    from proofgate.mcp_server import GuardedToolRequest

    schema = GuardedToolRequest.model_json_schema()
    assert set(schema["required"]) == {"instruction", "workflow_id", "inactive_days"}
    assert "flag_name" not in schema["properties"]
    assert "rollout_percentage" not in schema["properties"]


def test_mcp_feature_flag_schema_is_correct():
    from proofgate.mcp_server import FeatureFlagToolRequest

    schema = FeatureFlagToolRequest.model_json_schema()
    assert set(schema["required"]) == {"instruction", "workflow_id", "flag_name", "enabled", "rollout_percentage"}
    assert "flag_name" in schema["properties"]
    assert "enabled" in schema["properties"]
    assert "rollout_percentage" in schema["properties"]
    assert "inactive_days" not in schema["properties"]


def test_mcp_feature_flag_request_rejects_unknown_fields():
    from pydantic import ValidationError

    from proofgate.mcp_server import FeatureFlagToolRequest

    with pytest.raises(ValidationError):
        FeatureFlagToolRequest.model_validate(
            {
                "instruction": "x",
                "workflow_id": "wf-1",
                "flag_name": "new_checkout",
                "enabled": True,
                "rollout_percentage": 100,
                "unexpected_field": "surprise",
            }
        )


def test_mcp_feature_flag_request_rejects_out_of_range_rollout():
    from pydantic import ValidationError

    from proofgate.mcp_server import FeatureFlagToolRequest

    with pytest.raises(ValidationError):
        FeatureFlagToolRequest.model_validate(
            {"instruction": "x", "workflow_id": "wf-1", "flag_name": "new_checkout", "enabled": True, "rollout_percentage": 101}
        )
    with pytest.raises(ValidationError):
        FeatureFlagToolRequest.model_validate(
            {"instruction": "x", "workflow_id": "wf-1", "flag_name": "new_checkout", "enabled": True, "rollout_percentage": -1}
        )


def test_mcp_server_module_never_imports_feature_flags_operations_directly():
    """The MCP handler must never bypass guarded_execute -- it should
    have no direct dependency on operations.feature_flags at all."""
    import proofgate.mcp_server as mcp_server

    source = Path(mcp_server.__file__).read_text()
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert "operations.feature_flags" not in imported
    assert "operations.actions" not in imported


def test_mcp_broad_feature_flag_request_routes_through_guarded_execution():
    import asyncio

    import mcp.shared.memory as memory
    import proofgate.mcp_server as mcp_server

    workflow_id = "wf-ff-mcp-broad"
    _fresh(workflow_id)
    reset_feature_flags_working()

    async def go():
        async with memory.create_connected_server_and_client_session(mcp_server.server) as client:
            return await client.call_tool(
                "set_feature_flag",
                {
                    "instruction": BROAD_FF_INSTRUCTION,
                    "workflow_id": workflow_id,
                    "flag_name": "new_checkout",
                    "enabled": True,
                    "environment": None,
                    "rollout_percentage": 100,
                },
            )

    result = asyncio.run(go())
    assert result.isError is False
    payload = result.structuredContent
    assert payload["verdict"] == "BLOCK"
    assert payload["impact_envelope"]["estimated_count"] == 10073
    assert {r["rule_id"] for r in payload["triggered_rules"]} == {RULE_INTENT_BOUNDARY, RULE_WORKFLOW_BUDGET}
    assert payload["executed"] is False

    events = _audit_events()
    assert len(events) == 1


def test_mcp_corrected_feature_flag_request_routes_through_guarded_execution():
    import asyncio

    import mcp.shared.memory as memory
    import proofgate.mcp_server as mcp_server

    workflow_id = "wf-ff-mcp-corrected"
    _fresh(workflow_id)
    reset_feature_flags_working()

    async def go():
        async with memory.create_connected_server_and_client_session(mcp_server.server) as client:
            return await client.call_tool(
                "set_feature_flag",
                {
                    "instruction": CORRECTED_FF_INSTRUCTION,
                    "workflow_id": workflow_id,
                    "flag_name": "new_checkout",
                    "enabled": True,
                    "environment": "test",
                    "rollout_percentage": 100,
                },
            )

    result = asyncio.run(go())
    payload = result.structuredContent
    assert payload["verdict"] == "ALLOW"
    assert payload["executed"] is True
    assert payload["mutation_result"]["test_affected"] == 92
    assert payload["mutation_result"]["production_affected"] == 0
    assert payload["postcondition_result"]["status"] == "VERIFIED"
    assert payload["workflow_budget"]["rows_mutated"] == 92

    events = _audit_events()
    assert len(events) == 1


def test_mcp_malformed_feature_flag_request_is_rejected():
    import asyncio

    import mcp.shared.memory as memory
    import proofgate.mcp_server as mcp_server

    async def go():
        async with memory.create_connected_server_and_client_session(mcp_server.server) as client:
            return await client.call_tool(
                "set_feature_flag",
                {
                    "instruction": "x",
                    "workflow_id": "wf-ff-malformed",
                    "flag_name": "new_checkout",
                    "enabled": True,
                    "rollout_percentage": 150,  # out of range
                },
            )

    result = asyncio.run(go())
    assert result.isError is True


# ---------------------------------------------------------------------------
# Audit tests
# ---------------------------------------------------------------------------


def test_broad_audit_event_records_all_expected_fields():
    workflow_id = "wf-ff-audit-broad"
    _fresh(workflow_id)
    reset_feature_flags_working()
    guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, BROAD_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": None, "rollout_percentage": 100},
        None,
    )
    events = _audit_events()
    assert len(events) == 1
    event = events[0]
    assert event["tool_name"] == "set_feature_flag"
    assert event["tool_arguments"] == {
        "flag_name": "new_checkout",
        "enabled": True,
        "environment": None,
        "rollout_percentage": 100,
    }
    assert event["verdict"] == "BLOCK"
    assert {r["rule_id"] for r in event["triggered_rules"]} == {RULE_INTENT_BOUNDARY, RULE_WORKFLOW_BUDGET}
    assert event["execution_status"] == "NOT_EXECUTED"
    assert event["mutation_result"] is None
    assert event["postcondition_result"] is None
    assert event["suggested_repairs"][0]["tool"] == "set_feature_flag"
    assert event["workflow_id"] == workflow_id


def test_corrected_audit_event_records_all_expected_fields():
    workflow_id = "wf-ff-audit-corrected"
    _fresh(workflow_id)
    reset_feature_flags_working()
    guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, CORRECTED_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100},
        None,
    )
    events = _audit_events()
    assert len(events) == 1
    event = events[0]
    assert event["verdict"] == "ALLOW"
    assert event["execution_status"] == "EXECUTED"
    assert event["mutation_result"]["test_affected"] == 92
    assert event["mutation_result"]["production_affected"] == 0
    assert event["postcondition_result"]["status"] == "VERIFIED"
    assert event["workflow_budget_after"]["rows_mutated"] == 92


def test_audit_schema_unchanged_by_this_slice():
    from proofgate.models import AuditEvent

    expected_fields = {
        "schema_version", "event_id", "workflow_id", "timestamp",
        "original_instruction", "requesting_user", "agent_id", "tool_name",
        "tool_arguments", "intent_constraints", "impact_envelope",
        "risk_features", "risk_score", "risk_factors", "triggered_rules",
        "policy_version", "extraction_mode", "nebius_model", "craft_evidence",
        "verdict", "missing_requirements", "suggested_repairs", "proof_status",
        "proof_checks", "execution_status", "mutation_result",
        "postcondition_result", "workflow_budget_before", "workflow_budget_after",
    }
    assert set(AuditEvent.model_fields) == expected_fields


def test_no_duplicate_audit_event_for_one_feature_flag_call():
    workflow_id = "wf-ff-audit-dup"
    _fresh(workflow_id)
    reset_feature_flags_working()
    guarded_execute(
        "set_feature_flag",
        _action_context(workflow_id, CORRECTED_FF_INSTRUCTION),
        {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100},
        None,
    )
    assert len(_audit_events()) == 1


# ---------------------------------------------------------------------------
# App regression: no Streamlit exposure
# ---------------------------------------------------------------------------


def test_app_py_and_app_logic_do_not_mention_feature_flags():
    app_source = Path("app.py").read_text()
    app_logic_source = Path("app_logic.py").read_text()
    assert "set_feature_flag" not in app_source
    assert "set_feature_flag" not in app_logic_source
    assert "feature_flag" not in app_source.lower()
    assert "feature_flag" not in app_logic_source.lower()


def test_app_imports_successfully_with_three_registered_tools():
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "-c", "import app"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
