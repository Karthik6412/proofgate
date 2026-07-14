"""Slice 24 Part C: one real enforcement result, checked identically
across the three layers that describe it -- the raw AuditEvent, the real
MCP response, and what app_logic.py's own presentation helpers would
render for it. This exists specifically to catch the kind of drift Slice
22 found (build_suggested_repairs' hardcoded "delete_users", silently
wrong for every deactivate_users call), by comparing layers directly
against each other rather than checking each in isolation.

Uses the SDK's in-memory client/server session (no stdio subprocess),
same convention as tests/test_mcp_server.py.
"""

import asyncio
import json

import mcp.shared.memory as memory
import pytest

import proofgate.audit as audit_module
import proofgate.mcp_server as mcp_server
from app_logic import mutation_verb_label, recovery_proof_requirement_label
from operations.database import reset_working_db
from proofgate.budgets import reset_workflow_state

DELETE_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."
DEACTIVATE_INSTRUCTION = "Deactivate inactive test accounts that have not logged in for 90 days."


def _run(coro):
    return asyncio.run(coro)


async def _call(tool_name: str, arguments: dict):
    async with memory.create_connected_server_and_client_session(mcp_server.server) as client:
        return await client.call_tool(tool_name, arguments)


def _clean_state(workflow_id: str) -> None:
    reset_working_db()
    reset_workflow_state(workflow_id)


def _audit_events() -> list[dict]:
    path = audit_module.DEFAULT_AUDIT_PATH
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().splitlines() if line]


@pytest.fixture(autouse=True)
def _reset_real_working_db():
    reset_working_db()
    yield
    reset_working_db()


def _broad_block_case(tool_name: str, instruction: str, workflow_id: str):
    """Runs one real broad (missing environment) BLOCK call through the
    real MCP gateway and returns (mcp_response, audit_event) for the
    SAME single enforcement result -- not two separate calls."""
    _clean_state(workflow_id)
    result = _run(_call(tool_name, {"instruction": instruction, "workflow_id": workflow_id, "inactive_days": 90}))
    mcp_response = result.structuredContent

    events = _audit_events()
    assert len(events) == 1  # exactly this one call's event, nothing stale
    audit_event = events[0]

    return mcp_response, audit_event


def _assert_cross_layer_agreement(tool_name: str, mcp_response: dict, audit_event: dict):
    # --- verdict ---
    assert mcp_response["verdict"] == audit_event["verdict"] == "BLOCK"

    # --- triggered rules ---
    mcp_rule_ids = {r["rule_id"] for r in mcp_response["triggered_rules"]}
    audit_rule_ids = {r["rule_id"] for r in audit_event["triggered_rules"]}
    assert mcp_rule_ids == audit_rule_ids
    assert mcp_rule_ids  # a broad call always triggers at least one rule

    # --- suggested_repairs tool name: the exact Slice 22 regression guard ---
    assert mcp_response["suggested_repairs"], "expected a non-empty repair suggestion for this broad call"
    assert audit_event["suggested_repairs"]
    mcp_tool = mcp_response["suggested_repairs"][0]["tool"]
    audit_tool = audit_event["suggested_repairs"][0]["tool"]
    assert mcp_tool == audit_tool == tool_name

    # --- proof status: raw agreement between MCP and audit (the wire
    # format), plus a presentation-layer check that Streamlit's helper
    # produces an honest, non-contradictory interpretation of that same
    # raw value -- never a literal-string match (that's not the point;
    # the whole reason a presentation layer exists is to say something
    # different, but never something contradictory).
    assert mcp_response["proof_status"] == audit_event["proof_status"] == "MISSING"
    hard_delete = audit_event["impact_envelope"]["hard_delete"]
    assert mcp_response["impact_envelope"]["hard_delete"] == hard_delete

    presented_label = recovery_proof_requirement_label(hard_delete=hard_delete, proof_status=audit_event["proof_status"])
    if hard_delete:
        # Irreversible and no proof supplied: the raw wire value IS the
        # honest, complete story here -- presentation must not soften it.
        assert presented_label == "MISSING"
    else:
        # Reversible: the raw "MISSING" alone would misleadingly read as
        # an error. Presentation must say so was never required, while
        # the raw MCP/audit layers still honestly show "MISSING" --
        # that is the intentional, documented difference, not a bug.
        assert presented_label == "Not required for this reversible action"
        assert presented_label != mcp_response["proof_status"]

    # --- mutation-verb presentation sanity (same hard_delete input,
    # must not silently disagree with what the tool actually is) ---
    verb = mutation_verb_label(hard_delete)
    assert verb == ("deleted" if tool_name == "delete_users" else "deactivated")


def test_cross_layer_consistency_for_broad_deactivate_users_block():
    mcp_response, audit_event = _broad_block_case(
        "deactivate_users", DEACTIVATE_INSTRUCTION, "wf-cross-layer-deactivate"
    )
    assert audit_event["tool_name"] == "deactivate_users"
    _assert_cross_layer_agreement("deactivate_users", mcp_response, audit_event)


def test_cross_layer_consistency_for_broad_delete_users_block():
    mcp_response, audit_event = _broad_block_case("delete_users", DELETE_INSTRUCTION, "wf-cross-layer-delete")
    assert audit_event["tool_name"] == "delete_users"
    _assert_cross_layer_agreement("delete_users", mcp_response, audit_event)
