"""Slice 20 tests: centralized runtime-mode resolution
(proofgate/runtime_mode.py).

tests/conftest.py's autouse fixtures already force PROOFGATE_RUNTIME_MODE
and the legacy NEBIUS_LIVE_ENABLED/CRAFT_LIVE_ENABLED flags to a safe,
deterministic offline state for every test. Tests here that need a
different starting point explicitly clear or override those variables
within their own body.
"""

import pytest

from proofgate.runtime_mode import (
    RuntimeMode,
    RuntimeModeError,
    apply_runtime_mode_to_environment,
    mode_label,
    resolve_mode,
)


def _clear_all_mode_env(monkeypatch):
    for name in (
        "PROOFGATE_RUNTIME_MODE",
        "DEMO_RELIABLE_MODE",
        "NEBIUS_LIVE_ENABLED",
        "CRAFT_LIVE_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Explicit PROOFGATE_RUNTIME_MODE
# ---------------------------------------------------------------------------


def test_explicit_live(monkeypatch):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")
    assert resolve_mode() == RuntimeMode.LIVE


def test_explicit_fallback(monkeypatch):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "fallback")
    assert resolve_mode() == RuntimeMode.FALLBACK


def test_explicit_reliable_demo(monkeypatch):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "reliable_demo")
    assert resolve_mode() == RuntimeMode.RELIABLE_DEMO


@pytest.mark.parametrize("alias", ["reliable-demo", "demo", "RELIABLE_DEMO", "Reliable_Demo"])
def test_explicit_reliable_demo_aliases_and_case_insensitive(monkeypatch, alias):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", alias)
    assert resolve_mode() == RuntimeMode.RELIABLE_DEMO


def test_invalid_explicit_mode_rejected_clearly(monkeypatch):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "yolo")
    with pytest.raises(RuntimeModeError, match="yolo"):
        resolve_mode()


def test_invalid_mode_error_names_only_valid_options_no_secrets(monkeypatch):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "yolo")
    with pytest.raises(RuntimeModeError) as exc_info:
        resolve_mode()
    message = str(exc_info.value)
    assert "live" in message and "fallback" in message and "reliable_demo" in message
    assert "API_KEY" not in message.upper() or "PROOFGATE_RUNTIME_MODE" in message


# ---------------------------------------------------------------------------
# Missing variable defaults to LIVE
# ---------------------------------------------------------------------------


def test_missing_variable_defaults_to_live(monkeypatch):
    _clear_all_mode_env(monkeypatch)
    assert resolve_mode() == RuntimeMode.LIVE


# ---------------------------------------------------------------------------
# Legacy flag mapping
# ---------------------------------------------------------------------------


def test_legacy_demo_reliable_mode_maps_to_reliable_demo(monkeypatch):
    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("DEMO_RELIABLE_MODE", "true")
    assert resolve_mode() == RuntimeMode.RELIABLE_DEMO


def test_legacy_both_flags_false_maps_to_fallback(monkeypatch):
    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "false")
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "false")
    assert resolve_mode() == RuntimeMode.FALLBACK


def test_legacy_both_flags_true_maps_to_live(monkeypatch):
    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "true")
    assert resolve_mode() == RuntimeMode.LIVE


def test_legacy_single_false_flag_maps_to_fallback(monkeypatch):
    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "false")
    assert resolve_mode() == RuntimeMode.FALLBACK


def test_legacy_single_true_flag_maps_to_live(monkeypatch):
    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "true")
    assert resolve_mode() == RuntimeMode.LIVE


def test_contradictory_legacy_flags_are_rejected(monkeypatch):
    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "false")
    with pytest.raises(RuntimeModeError, match="[Cc]ontradictory"):
        resolve_mode()


def test_contradictory_legacy_flags_error_has_no_secrets(monkeypatch):
    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "false")
    monkeypatch.setenv("NEBIUS_API_KEY", "sk-should-never-appear-in-error")
    with pytest.raises(RuntimeModeError) as exc_info:
        resolve_mode()
    assert "sk-should-never-appear" not in str(exc_info.value)


def test_explicit_mode_overrides_contradictory_legacy_flags(monkeypatch):
    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "false")
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "reliable_demo")
    assert resolve_mode() == RuntimeMode.RELIABLE_DEMO


# ---------------------------------------------------------------------------
# apply_runtime_mode_to_environment
# ---------------------------------------------------------------------------


def test_apply_live_leaves_legacy_flags_untouched(monkeypatch):
    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "true")

    mode = apply_runtime_mode_to_environment()

    assert mode == RuntimeMode.LIVE
    import os

    assert os.environ["NEBIUS_LIVE_ENABLED"] == "true"
    assert os.environ["CRAFT_LIVE_ENABLED"] == "true"


def test_apply_fallback_forces_legacy_flags_off(monkeypatch):
    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "fallback")
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "true")

    mode = apply_runtime_mode_to_environment()

    assert mode == RuntimeMode.FALLBACK
    import os

    assert os.environ["NEBIUS_LIVE_ENABLED"] == "false"
    assert os.environ["CRAFT_LIVE_ENABLED"] == "false"


def test_apply_reliable_demo_forces_legacy_flags_off(monkeypatch):
    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "reliable_demo")

    mode = apply_runtime_mode_to_environment()

    assert mode == RuntimeMode.RELIABLE_DEMO
    import os

    assert os.environ["NEBIUS_LIVE_ENABLED"] == "false"
    assert os.environ["CRAFT_LIVE_ENABLED"] == "false"


# ---------------------------------------------------------------------------
# mode_label
# ---------------------------------------------------------------------------


def test_mode_label_covers_all_three_modes():
    assert mode_label(RuntimeMode.LIVE) == "Live preferred"
    assert mode_label(RuntimeMode.FALLBACK) == "Fallback (deterministic)"
    assert mode_label(RuntimeMode.RELIABLE_DEMO) == "Reliable demo"


# ---------------------------------------------------------------------------
# End-to-end: apply_runtime_mode_to_environment correctly gates the
# existing, unchanged agent.nebius_client.live_enabled() and
# craft.config.live_enabled() -- proving FALLBACK/RELIABLE_DEMO really do
# prevent both integrations' own live-attempt checks from passing.
# ---------------------------------------------------------------------------


def test_fallback_mode_disables_both_integrations_live_checks(monkeypatch):
    import agent.nebius_client as nebius_client_module
    import craft.config as craft_config_module

    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "fallback")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")  # present, but must not matter
    monkeypatch.setenv("CRAFT_PROJECT_ID", "dummy-project")  # present, but must not matter

    apply_runtime_mode_to_environment()

    assert nebius_client_module.live_enabled() is False
    assert craft_config_module.live_enabled() is False


def test_reliable_demo_mode_disables_both_integrations_live_checks(monkeypatch):
    import agent.nebius_client as nebius_client_module
    import craft.config as craft_config_module

    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "reliable_demo")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")
    monkeypatch.setenv("CRAFT_PROJECT_ID", "dummy-project")

    apply_runtime_mode_to_environment()

    assert nebius_client_module.live_enabled() is False
    assert craft_config_module.live_enabled() is False


def test_live_mode_leaves_both_integrations_live_checks_at_their_own_default(monkeypatch):
    import agent.nebius_client as nebius_client_module
    import craft.config as craft_config_module

    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")

    apply_runtime_mode_to_environment()

    # Neither legacy flag was touched by LIVE mode -- both integrations'
    # own functions fall through to their own existing default ("true").
    assert nebius_client_module.live_enabled() is True
    assert craft_config_module.live_enabled() is True


# ---------------------------------------------------------------------------
# Part L: RELIABLE_DEMO preserves the exact existing demonstration
# ---------------------------------------------------------------------------


def test_reliable_demo_mode_preserves_exact_delete_and_deactivate_outcomes(monkeypatch):
    from operations.database import reset_working_db
    from operations.snapshots import create_snapshot
    from proofgate.budgets import get_workflow_budget, reset_workflow_state
    from proofgate.core import guarded_delete_users, guarded_execute
    from proofgate.models import ActionContext
    from proofgate.policy import RULE_INTENT_BOUNDARY, RULE_RECOVERY_PROOF, RULE_WORKFLOW_BUDGET

    _clear_all_mode_env(monkeypatch)
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "reliable_demo")
    apply_runtime_mode_to_environment()

    delete_workflow = "reliable-demo-delete"
    deactivate_workflow = "reliable-demo-deactivate"
    reset_working_db()
    reset_workflow_state(delete_workflow)
    reset_workflow_state(deactivate_workflow)

    def ctx(workflow_id, instruction):
        return ActionContext(
            workflow_id=workflow_id, requesting_user="u", agent_id="a", original_instruction=instruction
        )

    # Broad (non-mutating) calls first, in either order.
    broad_delete = guarded_delete_users(
        action_context=ctx(delete_workflow, "Clean up inactive test accounts that have not logged in for 90 days."),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )
    assert broad_delete.verdict == "BLOCK"
    assert broad_delete.executed is False
    assert {r.rule_id for r in broad_delete.triggered_rules} == {
        RULE_INTENT_BOUNDARY,
        RULE_RECOVERY_PROOF,
        RULE_WORKFLOW_BUDGET,
    }

    broad_deactivate = guarded_execute(
        "deactivate_users",
        ctx(deactivate_workflow, "Deactivate inactive test accounts that have not logged in for 90 days."),
        {"inactive_days": 90, "environment": None},
        None,
    )
    assert broad_deactivate.verdict == "BLOCK"
    assert {r.rule_id for r in broad_deactivate.triggered_rules} == {
        RULE_INTENT_BOUNDARY,
        RULE_WORKFLOW_BUDGET,
    }

    # Corrected deactivate BEFORE corrected delete: deactivate only sets
    # status='deactivated' (rows still exist), and delete_users' predicate
    # never inspects status, so both still affect exactly 92 rows -- see
    # Slice 18's discovery of this shared-database row conflict.
    corrected_deactivate = guarded_execute(
        "deactivate_users",
        ctx(deactivate_workflow, "Deactivate inactive test accounts that have not logged in for 90 days."),
        {"inactive_days": 90, "environment": "test"},
        None,
    )
    assert corrected_deactivate.verdict == "ALLOW"
    assert corrected_deactivate.executed is True
    deactivate_budget = get_workflow_budget(deactivate_workflow)
    assert deactivate_budget.rows_mutated == 92
    assert deactivate_budget.production_rows_mutated == 0

    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)
    corrected_delete = guarded_delete_users(
        action_context=ctx(delete_workflow, "Clean up inactive test accounts that have not logged in for 90 days."),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )
    assert corrected_delete.verdict == "ALLOW"
    assert corrected_delete.executed is True
    delete_budget = get_workflow_budget(delete_workflow)
    assert delete_budget.rows_mutated == 92
    assert delete_budget.production_rows_mutated == 0

    reset_working_db()
