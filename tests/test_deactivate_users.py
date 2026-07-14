"""Slice 17 tests: deactivate_users as the second registered guarded tool.

Proves genericity: the exact same shared guarded_execute pipeline produces
a different, correct outcome for deactivate_users than for delete_users --
driven entirely by the registered tool's reversibility facts
(ImpactEnvelope.hard_delete, set by the Operations preview function), not
by any tool-name branch in proofgate/core.py or proofgate/policy.py.

tests/conftest.py's autouse fixtures already disable live CRAFT/Nebius and
isolate the audit log per test.
"""

import ast
import inspect
import uuid
from pathlib import Path
from unittest import mock

import operations.actions as operations_actions
import proofgate.audit as audit_module
import proofgate.policy as policy_module
from operations.actions import (
    count_rows,
    deactivate_users,
    delete_users,
    preview_deactivate_users,
    preview_delete_users,
)
from operations.database import PRISTINE_DB_PATH, WORKING_DB_PATH, reset_working_db
from proofgate.budgets import get_workflow_budget, reset_workflow_state
from proofgate.core import guarded_execute
from proofgate.models import ActionContext
from proofgate.policy import RULE_INTENT_BOUNDARY, RULE_RECOVERY_PROOF, RULE_UNKNOWN_IMPACT, RULE_WORKFLOW_BUDGET
from proofgate.registry import GuardedToolSpec, _REGISTRY, get_tool_spec
from proofgate.selector import compute_selector_hash

DELETE_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."
DEACTIVATE_INSTRUCTION = "Deactivate inactive test accounts that have not logged in for 90 days."


def _fresh_workflow_id() -> str:
    return f"wf-{uuid.uuid4().hex[:12]}"


def _action_context(workflow_id: str, instruction: str) -> ActionContext:
    return ActionContext(
        workflow_id=workflow_id,
        requesting_user="demo-user",
        agent_id="demo-agent",
        original_instruction=instruction,
    )


def _fresh_clean_state(workflow_id: str) -> None:
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
# Registry correctness
# ---------------------------------------------------------------------------


def test_registry_contains_delete_users_and_deactivate_users():
    """Slice 25 adds a third registered tool (set_feature_flag, see
    tests/test_feature_flag.py for its own registry assertions); this
    test only confirms the two users-table tools from this slice remain
    present and unaffected."""
    assert {"delete_users", "deactivate_users"}.issubset(set(_REGISTRY.keys()))


def test_registry_deactivate_users_metadata_is_correct():
    spec = get_tool_spec("deactivate_users")
    assert isinstance(spec, GuardedToolSpec)
    assert spec.tool_name == "deactivate_users"
    assert spec.resource == "users"
    assert spec.hard_delete is False
    assert spec.reversibility == "reversible"
    assert spec.selector_argument_names == ("inactive_days", "environment")


def test_registry_deactivate_users_preview_and_mutation_fns_delegate_to_operations():
    reset_working_db()
    spec = get_tool_spec("deactivate_users")

    preview = spec.preview_fn(inactive_days=90, environment="test")
    assert preview.tool_name == "deactivate_users"
    assert preview.estimated_count == 92

    mutation = spec.mutation_fn(inactive_days=90, environment="test")
    assert mutation.affected_count == 92


# ---------------------------------------------------------------------------
# Preview correctness
# ---------------------------------------------------------------------------


def test_deactivate_broad_preview_matches_exact_demo_counts():
    reset_working_db()
    impact = preview_deactivate_users(inactive_days=90, environment=None)

    assert impact.estimated_count == 10073
    assert impact.environment_counts == {"production": 9981, "test": 92}
    assert impact.hard_delete is False
    assert impact.reversibility == "reversible"
    assert impact.tool_name == "deactivate_users"


def test_deactivate_corrected_preview_matches_exact_demo_counts():
    reset_working_db()
    impact = preview_deactivate_users(inactive_days=90, environment="test")

    assert impact.estimated_count == 92
    assert impact.environment_counts == {"production": 0, "test": 92}


def test_deactivate_preview_selector_hash_uses_shared_canonicalization():
    reset_working_db()
    impact = preview_deactivate_users(inactive_days=90, environment="test")

    assert impact.selector_hash == compute_selector_hash(
        {"inactive_days": 90, "environment": "test"}
    )


def test_deactivate_preview_excludes_already_deactivated_rows():
    reset_working_db()
    deactivate_users(inactive_days=90, environment="test")

    impact = preview_deactivate_users(inactive_days=90, environment="test")
    assert impact.estimated_count == 0
    assert impact.environment_counts == {"production": 0, "test": 0}


# ---------------------------------------------------------------------------
# Direct mutation correctness
# ---------------------------------------------------------------------------


def test_deactivate_direct_mutation_affects_exactly_92_test_rows():
    reset_working_db()
    result = deactivate_users(inactive_days=90, environment="test")

    assert result.affected_count == 92
    assert result.production_affected == 0
    assert result.test_affected == 92


def test_deactivate_direct_mutation_preserves_total_row_count():
    reset_working_db()
    before = count_rows(WORKING_DB_PATH)
    deactivate_users(inactive_days=90, environment="test")
    after = count_rows(WORKING_DB_PATH)

    assert before == after == 10623


def test_deactivate_direct_mutation_marks_matching_rows_and_leaves_others_unchanged():
    import sqlite3

    reset_working_db()
    conn = sqlite3.connect(str(WORKING_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        before_statuses = {
            row["id"]: row["status"] for row in conn.execute("SELECT id, status FROM users")
        }
    finally:
        conn.close()

    deactivate_users(inactive_days=90, environment="test")

    conn = sqlite3.connect(str(WORKING_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        after_rows = {
            row["id"]: (row["status"], row["environment"], row["last_login"])
            for row in conn.execute("SELECT id, status, environment, last_login FROM users")
        }
    finally:
        conn.close()

    deactivated_ids = {id_ for id_, (status, _, _) in after_rows.items() if status == "deactivated"}
    assert len(deactivated_ids) == 92
    for deactivated_id in deactivated_ids:
        _, environment, _ = after_rows[deactivated_id]
        assert environment == "test"

    # Every row still exists (same id set as before), and every row that
    # wasn't selected for deactivation kept its original status.
    assert set(after_rows.keys()) == set(before_statuses.keys())
    for id_, (status, _, _) in after_rows.items():
        if id_ not in deactivated_ids:
            assert status == before_statuses[id_]


def test_deactivate_repeated_identical_direct_call_is_idempotent():
    reset_working_db()
    first = deactivate_users(inactive_days=90, environment="test")
    second = deactivate_users(inactive_days=90, environment="test")

    assert first.affected_count == 92
    assert second.affected_count == 0
    assert second.production_affected == 0
    assert second.test_affected == 0


def test_deactivate_rejects_pristine_database_as_mutation_target():
    try:
        deactivate_users(inactive_days=90, environment="test", db_path=PRISTINE_DB_PATH)
        assert False, "expected ValueError refusing to mutate pristine.db"
    except ValueError as exc:
        assert "pristine" in str(exc).lower()


# ---------------------------------------------------------------------------
# Broad genericity comparison: same pipeline, different registered
# reversibility facts -> different triggered rules. Not tool-name special
# casing -- see test_no_policy_special_casing_for_tool_name below.
# ---------------------------------------------------------------------------


def test_broad_delete_users_triggers_all_three_rules():
    workflow_id = _fresh_workflow_id()
    _fresh_clean_state(workflow_id)

    result = guarded_execute(
        tool_name="delete_users",
        action_context=_action_context(workflow_id, DELETE_INSTRUCTION),
        arguments={"inactive_days": 90, "environment": None},
        rollback_proof=None,
    )

    assert result.verdict == "BLOCK"
    assert result.executed is False
    assert {r.rule_id for r in result.triggered_rules} == {
        RULE_INTENT_BOUNDARY,
        RULE_RECOVERY_PROOF,
        RULE_WORKFLOW_BUDGET,
    }
    assert RULE_UNKNOWN_IMPACT not in {r.rule_id for r in result.triggered_rules}
    assert get_workflow_budget(workflow_id).rows_mutated == 0
    assert count_rows(WORKING_DB_PATH) == 10623


def test_broad_deactivate_users_triggers_exactly_two_rules_because_it_is_reversible():
    """deactivate_users omits RULE_RECOVERY_PROOF here purely because its
    registered preview reports hard_delete=False -- proofgate/policy.py's
    RULE_RECOVERY_PROOF check reads only ImpactEnvelope.hard_delete, with
    no tool_name branch (see test_no_policy_special_casing_for_tool_name).
    """
    workflow_id = _fresh_workflow_id()
    _fresh_clean_state(workflow_id)

    result = guarded_execute(
        tool_name="deactivate_users",
        action_context=_action_context(workflow_id, DEACTIVATE_INSTRUCTION),
        arguments={"inactive_days": 90, "environment": None},
        rollback_proof=None,
    )

    assert result.verdict == "BLOCK"
    assert result.executed is False
    assert {r.rule_id for r in result.triggered_rules} == {
        RULE_INTENT_BOUNDARY,
        RULE_WORKFLOW_BUDGET,
    }
    triggered_ids = {r.rule_id for r in result.triggered_rules}
    assert RULE_RECOVERY_PROOF not in triggered_ids
    assert RULE_UNKNOWN_IMPACT not in triggered_ids
    assert get_workflow_budget(workflow_id).rows_mutated == 0
    assert count_rows(WORKING_DB_PATH) == 10623


def test_broad_deactivate_users_suggested_repair_names_deactivate_users_not_delete_users():
    """Slice 24 regression guard: build_suggested_repairs must name the
    real tool being repaired. Before that fix, every suggestion
    hardcoded "tool": "delete_users", which would have misattributed
    this deactivate_users repair suggestion to the wrong tool.

    Slice 25 additionally generalized next_step to depend on the
    generic hard_delete registry metadata rather than always saying
    "create_snapshot" -- deactivate_users is reversible and never needs
    one, so suggesting it here would be actively misleading (the same
    reasoning Slice 25's set_feature_flag repair guidance requires)."""
    workflow_id = _fresh_workflow_id()
    _fresh_clean_state(workflow_id)

    result = guarded_execute(
        tool_name="deactivate_users",
        action_context=_action_context(workflow_id, DEACTIVATE_INSTRUCTION),
        arguments={"inactive_days": 90, "environment": None},
        rollback_proof=None,
    )

    assert result.suggested_repairs == [
        {
            "tool": "deactivate_users",
            "arguments": {"inactive_days": 90, "environment": "test"},
            "next_step": "retry_with_corrected_environment",
        }
    ]


# ---------------------------------------------------------------------------
# Corrected generic execution
# ---------------------------------------------------------------------------


def test_corrected_deactivate_users_allows_without_any_rollback_proof():
    workflow_id = _fresh_workflow_id()
    _fresh_clean_state(workflow_id)

    with mock.patch("operations.snapshots.create_snapshot") as snapshot_spy:
        result = guarded_execute(
            tool_name="deactivate_users",
            action_context=_action_context(workflow_id, DEACTIVATE_INSTRUCTION),
            arguments={"inactive_days": 90, "environment": "test"},
            rollback_proof=None,
        )
        snapshot_spy.assert_not_called()

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
    # No proof was supplied and none was required; "MISSING" here means
    # "no proof object present" -- it is not misrepresented as VALID, and
    # the rest of the audit record (hard_delete would be False on the
    # impact envelope, no RULE_RECOVERY_PROOF triggered, verdict ALLOW)
    # honestly makes "not required" independently derivable.
    assert audit_event["proof_status"] == "MISSING"
    assert audit_event["impact_envelope"]["hard_delete"] is False


def test_corrected_deactivate_users_audit_event_is_honest():
    workflow_id = _fresh_workflow_id()
    _fresh_clean_state(workflow_id)

    result = guarded_execute(
        tool_name="deactivate_users",
        action_context=_action_context(workflow_id, DEACTIVATE_INSTRUCTION),
        arguments={"inactive_days": 90, "environment": "test"},
        rollback_proof=None,
    )

    audit_event = _find_audit_event(result.audit_event_id)
    assert audit_event["tool_name"] == "deactivate_users"
    assert audit_event["tool_arguments"] == {"inactive_days": 90, "environment": "test"}
    assert audit_event["intent_constraints"]["action_type"] == "deactivate_users"
    assert audit_event["verdict"] == "ALLOW"
    assert audit_event["execution_status"] == "EXECUTED"


# ---------------------------------------------------------------------------
# Intent extraction
# ---------------------------------------------------------------------------


def test_deterministic_fallback_recognizes_deactivate_verb():
    from agent.nebius_client import extract_intent

    intent = extract_intent("Deactivate inactive test accounts that have not logged in for 90 days.")
    assert intent.action_type == "deactivate_users"
    assert intent.target_resource == "users"
    assert intent.environment == "test"
    assert intent.inactivity_days == 90


def test_deterministic_fallback_recognizes_deactivation_noun_form():
    from agent.nebius_client import extract_intent

    intent = extract_intent("Please run deactivation for test user accounts, 90 days inactive.")
    assert intent.action_type == "deactivate_users"


def test_deterministic_fallback_recognizes_deactivate_users_literal():
    from agent.nebius_client import extract_intent

    intent = extract_intent("deactivate_users for test accounts, 90 days")
    assert intent.action_type == "deactivate_users"


def test_deterministic_fallback_delete_extraction_is_unchanged():
    from agent.nebius_client import extract_intent

    intent = extract_intent(DELETE_INSTRUCTION.replace("Clean up", "Delete"))
    assert intent.action_type == "delete_users"
    intent2 = extract_intent(DELETE_INSTRUCTION)
    assert intent2.action_type == "delete_users"


# ---------------------------------------------------------------------------
# Selector-hash correctness
# ---------------------------------------------------------------------------


def test_both_tools_share_the_same_selector_hash_for_equivalent_arguments():
    reset_working_db()
    delete_impact = preview_delete_users(inactive_days=90, environment="test")
    deactivate_impact = preview_deactivate_users(inactive_days=90, environment="test")

    assert delete_impact.selector_hash == deactivate_impact.selector_hash
    assert delete_impact.selector_hash == compute_selector_hash(
        {"inactive_days": 90, "environment": "test"}
    )


def test_registry_module_still_defines_no_second_canonicalization_function():
    import proofgate.registry as registry_module

    source = Path(registry_module.__file__).read_text()
    tree = ast.parse(source)
    defined_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert "compute_selector_hash" not in defined_names
    assert "canonical_selector_json" not in defined_names


# ---------------------------------------------------------------------------
# No policy special casing
# ---------------------------------------------------------------------------


def test_no_policy_special_casing_for_tool_name():
    """proofgate/policy.py must contain no tool-identity branch at all.

    build_suggested_repairs (Slice 24) accepts the real tool identity as
    a plain parameter and places it directly into the returned
    suggestion -- it is never compared or branched on, so this test's
    "no tool-identity literal comparison" invariant still holds even
    though build_suggested_repairs' output now correctly varies by tool.
    """
    source = Path(policy_module.__file__).read_text()
    assert "tool_name" not in source
    assert "deactivate_users" not in source

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and comparator.value in (
                    "delete_users",
                    "deactivate_users",
                ):
                    raise AssertionError("policy.py contains a tool-name literal comparison")


# ---------------------------------------------------------------------------
# Operations boundary correctness
# ---------------------------------------------------------------------------


def test_deactivate_operations_functions_carry_no_policy_or_proof_metadata():
    preview_params = set(inspect.signature(preview_deactivate_users).parameters)
    mutation_params = set(inspect.signature(deactivate_users).parameters)

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


def test_operations_actions_module_does_not_import_proofgate_core_or_registry():
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


# ---------------------------------------------------------------------------
# Regression safety
# ---------------------------------------------------------------------------


def test_existing_delete_users_broad_counts_are_unchanged():
    reset_working_db()
    impact = preview_delete_users(inactive_days=90, environment=None)
    assert impact.estimated_count == 10073
    assert impact.environment_counts == {"production": 9981, "test": 92}


def test_existing_delete_users_corrected_mutation_is_unchanged():
    reset_working_db()
    result = delete_users(inactive_days=90, environment="test")
    assert result.affected_count == 92
    assert result.production_affected == 0
    assert result.test_affected == 92
