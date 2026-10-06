"""Slice 1 / P3 Part A: golden parity for externally visible Operations
policy output.

Captured from the implementation before the policy engine was
parameterized by ruleset. Every value below is pinned exactly -- rule
order, explanation strings, missing-requirement order, risk-factor order,
risk score, suggested repairs, proof status/checks and the policy fields
of the persisted AuditEvent -- so the refactor cannot silently change
any of them. Only nondeterministic fields (audit_event_id, event_id,
timestamp, ImpactEnvelope.generated_at, snapshot ids) are excluded.

Scenarios are driven through the public guarded_execute boundary, never
by calling policy functions directly, so these tests stay valid across
any internal policy API change.
"""

import json
import uuid
from unittest import mock

import pytest

import proofgate.audit as audit_module
from operations.database import reset_working_db
from operations.feature_flags import reset_feature_flags_working
from operations.snapshots import create_snapshot
from proofgate.budgets import get_workflow_budget, reset_workflow_state
from proofgate.core import guarded_execute
from proofgate.models import ActionContext

DELETE_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."
DEACTIVATE_INSTRUCTION = "Deactivate inactive test accounts that have not logged in for 90 days."
# Names no environment, so the fallback intent has environment=None and
# no intent mismatch is possible.
DEACTIVATE_NO_ENVIRONMENT_INSTRUCTION = "Deactivate inactive accounts that have not logged in for 90 days."
FEATURE_FLAG_INSTRUCTION = "Enable the new checkout flow for test users."

INTENT_BOUNDARY = {
    "rule_id": "RULE_INTENT_BOUNDARY",
    "explanation": "The user requested test scope, but production rows are included.",
}
RECOVERY_PROOF = {
    "rule_id": "RULE_RECOVERY_PROOF",
    "explanation": "The irreversible action has no valid rollback proof.",
}
BUDGET_10073 = {
    "rule_id": "RULE_WORKFLOW_BUDGET",
    "explanation": "10073 projected mutations exceed the 100-row workflow budget.",
}
# Pinned as-is: the explanation quotes the proposed estimated_count (92),
# not the projected workflow total (20 + 92 = 112).
BUDGET_92 = {
    "rule_id": "RULE_WORKFLOW_BUDGET",
    "explanation": "92 projected mutations exceed the 100-row workflow budget.",
}
PREVIEW_UNKNOWN = {
    "rule_id": "RULE_UNKNOWN_IMPACT",
    "explanation": "Impact preview failed or is unknown for a consequential action.",
}
UNREGISTERED_UNKNOWN = {
    "rule_id": "RULE_UNKNOWN_IMPACT",
    "explanation": "Tool 'reactivate_users' is not registered; impact cannot be measured.",
}

DELETE_REPAIR = {
    "tool": "delete_users",
    "arguments": {"inactive_days": 90, "environment": "test"},
    "next_step": "create_snapshot",
}
DEACTIVATE_REPAIR = {
    "tool": "deactivate_users",
    "arguments": {"inactive_days": 90, "environment": "test"},
    "next_step": "retry_with_corrected_environment",
}


def _feature_flag_repair(rollout_percentage: int) -> dict:
    return {
        "tool": "set_feature_flag",
        "arguments": {
            "flag_name": "new_checkout",
            "enabled": True,
            "environment": "test",
            "rollout_percentage": rollout_percentage,
        },
        "next_step": "retry_with_corrected_environment",
    }


ALL_PROOF_CHECKS_PASS = {
    "snapshot_exists": True,
    "resource_matches": True,
    "selector_hash_matches": True,
    "count_within_approved_maximum": True,
}


def _corrected_proof():
    return create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)


def _broad_proof():
    # Real snapshot, wrong selector for the corrected call.
    return create_snapshot(resource="users", inactive_days=90, environment=None, max_affected_rows=10073)


def _raise_preview(**kwargs):
    raise RuntimeError("preview failed")


SCENARIOS = {
    "broad_delete": dict(
        tool="delete_users",
        instruction=DELETE_INSTRUCTION,
        arguments={"inactive_days": 90, "environment": None},
        expected=dict(
            verdict="BLOCK",
            executed=False,
            triggered_rules=[INTENT_BOUNDARY, RECOVERY_PROOF, BUDGET_10073],
            missing_requirements=["environment=test", "valid rollback proof", "workflow impact within 100 rows"],
            risk_factors=["mass scope", "production impact", "intent mismatch", "irreversible action", "missing proof"],
            risk_score=9.53,
            suggested_repairs=[DELETE_REPAIR],
        ),
        proof_status="MISSING",
        proof_checks=None,
        rows_before=0,
        rows_after=0,
    ),
    "corrected_delete_valid_proof": dict(
        tool="delete_users",
        instruction=DELETE_INSTRUCTION,
        arguments={"inactive_days": 90, "environment": "test"},
        proof=_corrected_proof,
        expected=dict(
            verdict="ALLOW",
            executed=True,
            triggered_rules=[],
            missing_requirements=[],
            risk_factors=["moderate scope", "irreversible action"],
            risk_score=2.53,
            suggested_repairs=[DELETE_REPAIR],
        ),
        proof_status="VALID",
        proof_checks=ALL_PROOF_CHECKS_PASS,
        rows_before=0,
        rows_after=92,
    ),
    "corrected_delete_missing_proof": dict(
        tool="delete_users",
        instruction=DELETE_INSTRUCTION,
        arguments={"inactive_days": 90, "environment": "test"},
        expected=dict(
            verdict="BLOCK",
            executed=False,
            triggered_rules=[RECOVERY_PROOF],
            missing_requirements=["valid rollback proof"],
            risk_factors=["moderate scope", "irreversible action", "missing proof"],
            risk_score=3.53,
            suggested_repairs=[DELETE_REPAIR],
        ),
        proof_status="MISSING",
        proof_checks=None,
        rows_before=0,
        rows_after=0,
    ),
    "corrected_delete_invalid_proof": dict(
        tool="delete_users",
        instruction=DELETE_INSTRUCTION,
        arguments={"inactive_days": 90, "environment": "test"},
        proof=_broad_proof,
        expected=dict(
            verdict="BLOCK",
            executed=False,
            triggered_rules=[RECOVERY_PROOF],
            missing_requirements=["valid rollback proof"],
            risk_factors=["moderate scope", "irreversible action", "missing proof"],
            risk_score=3.53,
            suggested_repairs=[DELETE_REPAIR],
        ),
        proof_status="INVALID",
        proof_checks={**ALL_PROOF_CHECKS_PASS, "selector_hash_matches": False},
        rows_before=0,
        rows_after=0,
    ),
    "workflow_budget_overflow": dict(
        tool="delete_users",
        instruction=DELETE_INSTRUCTION,
        arguments={"inactive_days": 90, "environment": "test"},
        proof=_corrected_proof,
        seed_rows_mutated=20,
        expected=dict(
            verdict="BLOCK",
            executed=False,
            triggered_rules=[BUDGET_92],
            missing_requirements=["workflow impact within 100 rows"],
            risk_factors=["moderate scope", "irreversible action"],
            risk_score=2.53,
            suggested_repairs=[DELETE_REPAIR],
        ),
        proof_status="VALID",
        proof_checks=ALL_PROOF_CHECKS_PASS,
        rows_before=20,
        rows_after=20,
    ),
    "workflow_budget_exactly_100": dict(
        # 8 existing + 92 projected == 100: RULE_WORKFLOW_BUDGET fires only
        # above 100 (> 100, not >= 100), so this is ALLOW.
        tool="delete_users",
        instruction=DELETE_INSTRUCTION,
        arguments={"inactive_days": 90, "environment": "test"},
        proof=_corrected_proof,
        seed_rows_mutated=8,
        expected=dict(
            verdict="ALLOW",
            executed=True,
            triggered_rules=[],
            missing_requirements=[],
            risk_factors=["moderate scope", "irreversible action"],
            risk_score=2.53,
            suggested_repairs=[DELETE_REPAIR],
        ),
        proof_status="VALID",
        proof_checks=ALL_PROOF_CHECKS_PASS,
        rows_before=8,
        rows_after=100,
    ),
    "broad_deactivate": dict(
        tool="deactivate_users",
        instruction=DEACTIVATE_INSTRUCTION,
        arguments={"inactive_days": 90, "environment": None},
        expected=dict(
            verdict="BLOCK",
            executed=False,
            triggered_rules=[INTENT_BOUNDARY, BUDGET_10073],
            missing_requirements=["environment=test", "workflow impact within 100 rows"],
            risk_factors=["mass scope", "production impact", "intent mismatch"],
            risk_score=7.03,
            suggested_repairs=[DEACTIVATE_REPAIR],
        ),
        proof_status="MISSING",
        proof_checks=None,
        rows_before=0,
        rows_after=0,
    ),
    "production_impact_without_intent_mismatch": dict(
        # Instruction names no environment: production rows are included
        # but there is no intent mismatch, so "production impact" appears
        # without "intent mismatch" and RULE_INTENT_BOUNDARY does not fire.
        # Repairs are [] because the intent has no environment to correct to.
        tool="deactivate_users",
        instruction=DEACTIVATE_NO_ENVIRONMENT_INSTRUCTION,
        arguments={"inactive_days": 90, "environment": None},
        expected=dict(
            verdict="BLOCK",
            executed=False,
            triggered_rules=[BUDGET_10073],
            missing_requirements=["workflow impact within 100 rows"],
            risk_factors=["mass scope", "production impact"],
            risk_score=5.03,
            suggested_repairs=[],
        ),
        proof_status="MISSING",
        proof_checks=None,
        rows_before=0,
        rows_after=0,
    ),
    "broad_feature_flag": dict(
        tool="set_feature_flag",
        instruction=FEATURE_FLAG_INSTRUCTION,
        arguments={"flag_name": "new_checkout", "enabled": True, "environment": None, "rollout_percentage": 100},
        expected=dict(
            verdict="BLOCK",
            executed=False,
            triggered_rules=[INTENT_BOUNDARY, BUDGET_10073],
            missing_requirements=["environment=test", "workflow impact within 100 rows"],
            risk_factors=["mass scope", "production impact", "intent mismatch"],
            risk_score=7.03,
            suggested_repairs=[_feature_flag_repair(100)],
        ),
        proof_status="MISSING",
        proof_checks=None,
        rows_before=0,
        rows_after=0,
    ),
    "preview_failure": dict(
        tool="delete_users",
        instruction=DELETE_INSTRUCTION,
        arguments={"inactive_days": 90, "environment": None},
        preview_raises=True,
        expected=dict(
            verdict="BLOCK",
            executed=False,
            triggered_rules=[PREVIEW_UNKNOWN],
            missing_requirements=["known operational impact"],
            risk_factors=[],
            risk_score=10.0,
            # Pinned as-is: unlike the unregistered-tool path, a registered
            # tool whose preview fails still gets a repair suggestion.
            suggested_repairs=[DELETE_REPAIR],
        ),
        proof_status="MISSING",
        proof_checks=None,
        rows_before=0,
        rows_after=0,
    ),
    "unregistered_tool": dict(
        tool="reactivate_users",
        instruction=DELETE_INSTRUCTION,
        arguments={"inactive_days": 90},
        expected=dict(
            verdict="BLOCK",
            executed=False,
            triggered_rules=[UNREGISTERED_UNKNOWN],
            missing_requirements=["known operational impact"],
            risk_factors=[],
            risk_score=10.0,
            suggested_repairs=[],
        ),
        proof_status="MISSING",
        proof_checks=None,
        rows_before=0,
        rows_after=0,
    ),
    "production_impact_test_intent_no_hard_delete": dict(
        # 1% of the 9,981-user production audience = 99 users: within the
        # 100-row budget, reversible, so only the intent boundary fires.
        tool="set_feature_flag",
        instruction=FEATURE_FLAG_INSTRUCTION,
        arguments={"flag_name": "new_checkout", "enabled": True, "environment": "production", "rollout_percentage": 1},
        expected=dict(
            verdict="BLOCK",
            executed=False,
            triggered_rules=[INTENT_BOUNDARY],
            missing_requirements=["environment=test"],
            risk_factors=["moderate scope", "production impact", "intent mismatch"],
            risk_score=5.03,
            suggested_repairs=[_feature_flag_repair(1)],
        ),
        proof_status="MISSING",
        proof_checks=None,
        rows_before=0,
        rows_after=0,
    ),
}


@pytest.fixture(autouse=True)
def _clean_operations_state():
    reset_working_db()
    reset_feature_flags_working()
    yield
    reset_working_db()
    reset_feature_flags_working()


def _run(scenario: dict) -> tuple[dict, dict]:
    workflow_id = f"wf-golden-{uuid.uuid4().hex[:12]}"
    reset_workflow_state(workflow_id)
    get_workflow_budget(workflow_id).rows_mutated = scenario.get("seed_rows_mutated", 0)

    proof = scenario["proof"]() if "proof" in scenario else None
    action_context = ActionContext(
        workflow_id=workflow_id,
        requesting_user="golden-user",
        agent_id="golden-agent",
        original_instruction=scenario["instruction"],
    )

    if scenario.get("preview_raises"):
        with mock.patch("operations.actions.preview_delete_users", _raise_preview):
            result = guarded_execute(scenario["tool"], action_context, scenario["arguments"], proof)
    else:
        result = guarded_execute(scenario["tool"], action_context, scenario["arguments"], proof)

    events = [
        json.loads(line)
        for line in audit_module.DEFAULT_AUDIT_PATH.read_text().splitlines()
        if line
    ]
    matching = [event for event in events if event["event_id"] == result.audit_event_id]
    assert len(matching) == 1
    return result.model_dump(mode="json"), matching[0]


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_enforcement_result_matches_golden(name):
    scenario = SCENARIOS[name]
    result, _ = _run(scenario)

    result.pop("audit_event_id")
    assert result == scenario["expected"]


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_audit_event_policy_fields_match_golden(name):
    scenario = SCENARIOS[name]
    expected = scenario["expected"]
    _, event = _run(scenario)

    assert event["policy_version"] == "v1"
    # The golden contract is captured from the deterministic regex fallback;
    # fail loudly rather than silently compare against a live model.
    assert event["extraction_mode"] == "deterministic_fallback"
    assert event["verdict"] == expected["verdict"]
    assert event["triggered_rules"] == expected["triggered_rules"]
    assert event["missing_requirements"] == expected["missing_requirements"]
    assert event["risk_factors"] == expected["risk_factors"]
    assert event["risk_score"] == expected["risk_score"]
    assert event["suggested_repairs"] == expected["suggested_repairs"]
    assert event["proof_status"] == scenario["proof_status"]
    assert event["proof_checks"] == scenario["proof_checks"]
    assert event["execution_status"] == ("EXECUTED" if expected["executed"] else "NOT_EXECUTED")
    assert event["tool_name"] == scenario["tool"]
    assert event["workflow_budget_before"]["rows_mutated"] == scenario["rows_before"]
    assert event["workflow_budget_after"]["rows_mutated"] == scenario["rows_after"]
    assert event["workflow_budget_before"]["max_rows"] == event["workflow_budget_after"]["max_rows"] == 100
