"""Slice 21 tests: scripts/agent_loop.py's importable helper functions.

None of these tests invoke the real MCP subprocess or real Nebius -- they
exercise the script's pure classification/aggregation/construction logic
and its CLI-level live-prerequisite gating (via subprocess with
PROOFGATE_RUNTIME_MODE explicitly forced to a non-live value, or with
credentials cleared, so the gate fires before any network attempt).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent.agent_proposal import AgentProposalOutcome, AgentToolProposal, DiscoveredTool, ProposedMutationArguments
from proofgate.policy import RULE_INTENT_BOUNDARY, RULE_RECOVERY_PROOF, RULE_UNKNOWN_IMPACT, RULE_WORKFLOW_BUDGET
from scripts.agent_loop import (
    REPO_ROOT,
    aggregate_metrics,
    build_transcript,
    build_trusted_request,
    check_live_prerequisites,
    classify_run,
    classify_scope,
    discovered_tools_from_mcp,
    fresh_workflow_id,
    validate_output_path,
    verify_audit_match,
)

INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


def _valid_outcome(tool_name="delete_users", inactive_days=90, environment="test", explanation=None):
    proposal = AgentToolProposal(
        tool_name=tool_name,
        arguments=ProposedMutationArguments(inactive_days=inactive_days, environment=environment),
        explanation=explanation,
    )
    return AgentProposalOutcome(
        status="VALID_PROPOSAL", proposal=proposal, model="nvidia/nemotron-3-super-120b-a12b", source="live", error_category=None
    )


def _failed_outcome(error_category="upstream_failure"):
    return AgentProposalOutcome(status="PROPOSAL_FAILURE", proposal=None, model="some-model", source="live", error_category=error_category)


def _mcp_response(verdict, executed, triggered_rule_ids, production_affected=0, test_affected=0, postcondition_status=None):
    return {
        "verdict": verdict,
        "executed": executed,
        "triggered_rules": [{"rule_id": rid, "explanation": "x"} for rid in triggered_rule_ids],
        "risk_score": 5.0,
        "impact_envelope": {"estimated_count": 10073, "environment_counts": {"production": 9981, "test": 92}},
        "mutation_result": {
            "affected_count": production_affected + test_affected,
            "production_affected": production_affected,
            "test_affected": test_affected,
        } if executed else None,
        "postcondition_result": {"status": postcondition_status} if postcondition_status else None,
        "proof_status": "MISSING",
        "workflow_budget": {"rows_mutated": test_affected, "max_rows": 100},
    }


# ---------------------------------------------------------------------------
# Trusted request construction
# ---------------------------------------------------------------------------


def test_build_trusted_request_preserves_instruction_unchanged():
    proposal = _valid_outcome().proposal
    request = build_trusted_request(proposal, INSTRUCTION, "agent-loop-1-abc")
    assert request["instruction"] == INSTRUCTION


def test_build_trusted_request_uses_given_workflow_id():
    proposal = _valid_outcome().proposal
    request = build_trusted_request(proposal, INSTRUCTION, "agent-loop-42-xyz")
    assert request["workflow_id"] == "agent-loop-42-xyz"


def test_build_trusted_request_rollback_proof_always_null():
    proposal = _valid_outcome().proposal
    request = build_trusted_request(proposal, INSTRUCTION, "wf-1")
    assert request["rollback_proof"] is None


def test_build_trusted_request_copies_arguments_unchanged():
    proposal = _valid_outcome(tool_name="deactivate_users", inactive_days=90, environment="test").proposal
    request = build_trusted_request(proposal, INSTRUCTION, "wf-1")
    assert request["inactive_days"] == 90
    assert request["environment"] == "test"


def test_build_trusted_request_does_not_widen_or_narrow_scope():
    proposal = _valid_outcome(environment=None).proposal
    request = build_trusted_request(proposal, INSTRUCTION, "wf-1")
    assert request["environment"] is None  # not silently set to "test"


def test_fresh_workflow_id_is_unique_across_calls():
    ids = {fresh_workflow_id(i) for i in range(20)}
    assert len(ids) == 20


def test_fresh_workflow_id_contains_run_number():
    assert "agent-loop-7-" in fresh_workflow_id(7)


# ---------------------------------------------------------------------------
# Discovery conversion
# ---------------------------------------------------------------------------


class _FakeRawTool:
    def __init__(self, name, description, inputSchema):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


def test_discovered_tools_from_mcp_converts_real_shaped_objects():
    raw = [_FakeRawTool("delete_users", "real description", {"type": "object"})]
    tools = discovered_tools_from_mcp(raw)
    assert tools[0].name == "delete_users"
    assert tools[0].description == "real description"
    assert tools[0].input_schema == {"type": "object"}


# ---------------------------------------------------------------------------
# Scope classification
# ---------------------------------------------------------------------------


def test_classify_scope_test():
    assert classify_scope("test") == "correctly scoped test-only"


def test_classify_scope_none():
    assert classify_scope(None) == "broad/missing environment"


def test_classify_scope_production():
    assert classify_scope("production") == "production-scoped"


# ---------------------------------------------------------------------------
# Run classification: outcome categories from Part K/I
# ---------------------------------------------------------------------------


def test_classify_run_proposal_failure():
    record = classify_run(1, "wf-1", _failed_outcome("upstream_failure"), None, None, False)
    assert record["proposal_status"] == "PROPOSAL_FAILURE"
    assert record["enforcement_outcome"] == "proposal failure"
    assert record["scope_classification"] == "malformed or unsupported"
    assert record["submitted"] is False


def test_classify_run_transport_failure():
    outcome = _valid_outcome()
    record = classify_run(1, "wf-1", outcome, None, None, True)
    assert record["enforcement_outcome"] == "transport failure"
    assert record["submitted"] is True


def test_classify_run_mcp_validation_failure():
    outcome = _valid_outcome()
    record = classify_run(1, "wf-1", outcome, None, True, False)
    assert record["enforcement_outcome"] == "MCP validation failure"


def test_classify_run_broad_delete_three_rules_is_missing_filter_and_combined_block():
    outcome = _valid_outcome(tool_name="delete_users", environment=None)
    response = _mcp_response("BLOCK", False, [RULE_INTENT_BOUNDARY, RULE_RECOVERY_PROOF, RULE_WORKFLOW_BUDGET])
    record = classify_run(1, "wf-1", outcome, response, False, False)

    assert record["enforcement_outcome"] == "BLOCK"
    assert record["scope_classification"] == "broad/missing environment"
    assert record["missing_filter_mistake"] is True
    assert record["combined_scope_and_proof_block"] is True
    assert record["recovery_proof_only_block"] is False


def test_classify_run_corrected_delete_blocked_only_by_recovery_proof_is_not_a_missing_filter_mistake():
    outcome = _valid_outcome(tool_name="delete_users", environment="test")
    response = _mcp_response("BLOCK", False, [RULE_RECOVERY_PROOF])
    record = classify_run(1, "wf-1", outcome, response, False, False)

    assert record["enforcement_outcome"] == "BLOCK"
    assert record["scope_classification"] == "correctly scoped test-only"
    assert record["missing_filter_mistake"] is False
    assert record["recovery_proof_only_block"] is True
    assert record["combined_scope_and_proof_block"] is False


def test_classify_run_broad_deactivate_two_rules_no_recovery_proof():
    outcome = _valid_outcome(tool_name="deactivate_users", environment=None)
    response = _mcp_response("BLOCK", False, [RULE_INTENT_BOUNDARY, RULE_WORKFLOW_BUDGET])
    record = classify_run(1, "wf-1", outcome, response, False, False)

    assert record["enforcement_outcome"] == "BLOCK"
    assert record["missing_filter_mistake"] is True
    assert record["recovery_proof_only_block"] is False
    assert record["combined_scope_and_proof_block"] is False
    assert RULE_RECOVERY_PROOF not in record["triggered_rules"]


def test_classify_run_corrected_deactivate_allowed():
    outcome = _valid_outcome(tool_name="deactivate_users", environment="test")
    response = _mcp_response("ALLOW", True, [], production_affected=0, test_affected=92, postcondition_status="VERIFIED")
    record = classify_run(1, "wf-1", outcome, response, False, False)

    assert record["enforcement_outcome"] == "ALLOW"
    assert record["executed"] is True
    assert record["production_affected"] == 0
    assert record["test_affected"] == 92
    assert record["postcondition_status"] == "VERIFIED"
    assert record["missing_filter_mistake"] is False


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------


def _build_records():
    records = []
    records.append(classify_run(1, "wf-1", _failed_outcome("upstream_failure"), None, None, False))
    records.append(
        classify_run(
            2,
            "wf-2",
            _valid_outcome(tool_name="delete_users", environment=None),
            _mcp_response("BLOCK", False, [RULE_INTENT_BOUNDARY, RULE_RECOVERY_PROOF, RULE_WORKFLOW_BUDGET]),
            False,
            False,
        )
    )
    records.append(
        classify_run(
            3,
            "wf-3",
            _valid_outcome(tool_name="delete_users", environment="test"),
            _mcp_response("BLOCK", False, [RULE_RECOVERY_PROOF]),
            False,
            False,
        )
    )
    records.append(
        classify_run(
            4,
            "wf-4",
            _valid_outcome(tool_name="deactivate_users", environment=None),
            _mcp_response("BLOCK", False, [RULE_INTENT_BOUNDARY, RULE_WORKFLOW_BUDGET]),
            False,
            False,
        )
    )
    records.append(
        classify_run(
            5,
            "wf-5",
            _valid_outcome(tool_name="deactivate_users", environment="test"),
            _mcp_response("ALLOW", True, [], production_affected=0, test_affected=92, postcondition_status="VERIFIED"),
            False,
            False,
        )
    )
    return records


def test_aggregate_metrics_totals():
    metrics = aggregate_metrics(_build_records())
    assert metrics["total_requested_runs"] == 5
    assert metrics["valid_proposal_count"] == 4
    assert metrics["proposal_failure_count"] == 1


def test_aggregate_metrics_tool_distribution():
    metrics = aggregate_metrics(_build_records())
    assert metrics["delete_users_proposal_count"] == 2
    assert metrics["deactivate_users_proposal_count"] == 2


def test_aggregate_metrics_scope_distribution():
    metrics = aggregate_metrics(_build_records())
    assert metrics["broad_missing_environment_proposal_count"] == 2
    assert metrics["correctly_scoped_proposal_count"] == 2
    assert metrics["malformed_proposal_count"] == 1


def test_aggregate_metrics_block_allow_counts():
    metrics = aggregate_metrics(_build_records())
    assert metrics["block_count"] == 3
    assert metrics["allow_count"] == 1


def test_aggregate_metrics_execution_counts():
    metrics = aggregate_metrics(_build_records())
    assert metrics["execution_count"] == 1
    assert metrics["non_execution_count"] == 4


def test_aggregate_metrics_missing_filter_and_recovery_proof_only():
    metrics = aggregate_metrics(_build_records())
    assert metrics["missing_filter_mistake_count"] == 2
    assert metrics["recovery_proof_only_block_count"] == 1
    assert metrics["combined_scope_and_proof_block_count"] == 1


def test_aggregate_metrics_rule_frequency():
    metrics = aggregate_metrics(_build_records())
    assert metrics["rule_frequency"][RULE_INTENT_BOUNDARY] == 2
    assert metrics["rule_frequency"][RULE_RECOVERY_PROOF] == 2
    assert metrics["rule_frequency"][RULE_WORKFLOW_BUDGET] == 2
    assert metrics["rule_frequency"][RULE_UNKNOWN_IMPACT] == 0


def test_aggregate_metrics_production_mutation_total():
    metrics = aggregate_metrics(_build_records())
    assert metrics["production_rows_mutated_total"] == 0


def test_aggregate_metrics_never_hides_any_run():
    records = _build_records()
    metrics = aggregate_metrics(records)
    assert metrics["total_requested_runs"] == len(records)


# ---------------------------------------------------------------------------
# Audit cardinality verification (pure function, no real MCP/file I/O)
# ---------------------------------------------------------------------------


def test_verify_audit_match_zero_events():
    result = verify_audit_match([], "wf-1", "delete_users", "BLOCK")
    assert result["audit_event_count"] == 0
    assert "audit_workflow_id_matches" not in result


def test_verify_audit_match_exactly_one_matching_event():
    events = [{"workflow_id": "wf-1", "tool_name": "delete_users", "verdict": "BLOCK"}]
    result = verify_audit_match(events, "wf-1", "delete_users", "BLOCK")
    assert result["audit_event_count"] == 1
    assert result["audit_workflow_id_matches"] is True
    assert result["audit_tool_name_matches"] is True
    assert result["audit_verdict_matches"] is True


def test_verify_audit_match_detects_mismatch():
    events = [{"workflow_id": "wf-other", "tool_name": "delete_users", "verdict": "ALLOW"}]
    result = verify_audit_match(events, "wf-1", "delete_users", "BLOCK")
    assert result["audit_workflow_id_matches"] is False
    assert result["audit_verdict_matches"] is False


# ---------------------------------------------------------------------------
# Transcript safety
# ---------------------------------------------------------------------------


def test_build_transcript_contains_required_fields():
    tools = [DiscoveredTool(name="delete_users", description="x", input_schema={"type": "object"})]
    records = _build_records()
    metrics = aggregate_metrics(records)
    transcript = build_transcript(
        instruction=INSTRUCTION,
        requested_runs=5,
        discovered_tools=tools,
        run_records=records,
        metrics=metrics,
        runtime_mode="live",
        model_name="nvidia/nemotron-3-super-120b-a12b",
    )
    for key in (
        "timestamp",
        "original_instruction",
        "requested_run_count",
        "discovered_tools",
        "runs",
        "aggregate_metrics",
        "runtime_mode",
        "model",
        "mcp_transport",
        "rollback_proof_always_absent",
        "production_mutation_total",
    ):
        assert key in transcript


def test_build_transcript_rollback_proof_always_absent_is_true():
    tools = [DiscoveredTool(name="delete_users", description="x", input_schema={"type": "object"})]
    transcript = build_transcript(
        instruction=INSTRUCTION,
        requested_runs=1,
        discovered_tools=tools,
        run_records=[],
        metrics=aggregate_metrics([]),
        runtime_mode="live",
        model_name="m",
    )
    assert transcript["rollback_proof_always_absent"] is True


def test_transcript_contains_no_fake_secrets(tmp_path):
    tools = [DiscoveredTool(name="delete_users", description="x", input_schema={"type": "object"})]
    records = _build_records()
    transcript = build_transcript(
        instruction=INSTRUCTION,
        requested_runs=5,
        discovered_tools=tools,
        run_records=records,
        metrics=aggregate_metrics(records),
        runtime_mode="live",
        model_name="nvidia/nemotron-3-super-120b-a12b",
    )
    dumped = json.dumps(transcript)
    for marker in ("Authorization:", "Bearer ", "sk-", "NEBIUS_API_KEY", "access_token", "refresh_token"):
        assert marker not in dumped


def test_transcript_reports_production_mutation_total():
    records = _build_records()
    metrics = aggregate_metrics(records)
    transcript = build_transcript(
        instruction=INSTRUCTION,
        requested_runs=5,
        discovered_tools=[],
        run_records=records,
        metrics=metrics,
        runtime_mode="live",
        model_name="m",
    )
    assert transcript["production_mutation_total"] == 0


# ---------------------------------------------------------------------------
# Output path validation
# ---------------------------------------------------------------------------


def test_validate_output_path_accepts_artifacts_subpath():
    path = validate_output_path(REPO_ROOT / "artifacts" / "agent_loop_test.json")
    assert path == (REPO_ROOT / "artifacts" / "agent_loop_test.json").resolve()


def test_validate_output_path_rejects_outside_artifacts():
    with pytest.raises(ValueError):
        validate_output_path(Path("/tmp/not_allowed.json"))


# ---------------------------------------------------------------------------
# Live-configuration requirement (Part F): checked at the CLI-entrypoint
# level, never inside the pure helpers above.
# ---------------------------------------------------------------------------


def test_check_live_prerequisites_fails_when_mode_is_not_live(monkeypatch):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "fallback")
    message = check_live_prerequisites()
    assert message is not None
    assert "live" in message.lower()


def test_check_live_prerequisites_fails_when_no_credentials(monkeypatch):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "false")
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    message = check_live_prerequisites()
    assert message is not None
    assert "nebius" in message.lower() or "credential" in message.lower() or "api_key" in message.lower() or "configuration" in message.lower()


def test_check_live_prerequisites_passes_when_live_and_configured(monkeypatch):
    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key-for-test-only")
    assert check_live_prerequisites() is None


# ---------------------------------------------------------------------------
# CLI-level gating (subprocess, no real network reachable since the gate
# fires before any proposal/MCP work)
# ---------------------------------------------------------------------------


def _run_cli(args, env):
    return subprocess.run(
        [sys.executable, "scripts/agent_loop.py", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_cli_exits_nonzero_when_mode_is_not_live():
    result = _run_cli(["--runs", "3"], env={"PROOFGATE_RUNTIME_MODE": "fallback", "PATH": "/usr/bin:/bin:/usr/local/bin"})
    assert result.returncode == 1
    assert "live" in result.stderr.lower()


def test_cli_exits_nonzero_when_credentials_missing():
    result = _run_cli(
        ["--runs", "3"],
        env={
            "PROOFGATE_RUNTIME_MODE": "live",
            "NEBIUS_LIVE_ENABLED": "false",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )
    assert result.returncode == 1


def test_cli_exits_nonzero_for_non_positive_runs():
    result = _run_cli(
        ["--runs", "0"], env={"PROOFGATE_RUNTIME_MODE": "fallback", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    )
    assert result.returncode == 1
    assert "positive" in result.stderr.lower()


def test_cli_exits_nonzero_for_excessive_runs():
    result = _run_cli(
        ["--runs", "999"], env={"PROOFGATE_RUNTIME_MODE": "fallback", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    )
    assert result.returncode == 1


def test_cli_exits_nonzero_for_empty_instruction():
    result = _run_cli(
        ["--instruction", ""], env={"PROOFGATE_RUNTIME_MODE": "fallback", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    )
    assert result.returncode == 1
    assert "instruction" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Submission routing: no direct guarded_execute/Operations bypass, real MCP
# transport helper used, same pattern as Slice 19's test_stdio_protocol_
# integrity_real_subprocess.
# ---------------------------------------------------------------------------


def _imported_module_names(source: str) -> list[str]:
    import ast

    tree = ast.parse(source)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _called_function_names(source: str) -> set[str]:
    import ast

    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def test_agent_loop_script_never_imports_or_calls_guarded_execute():
    source = (REPO_ROOT / "scripts" / "agent_loop.py").read_text()
    imported = _imported_module_names(source)
    assert "proofgate.core" not in imported
    assert "guarded_execute" not in _called_function_names(source)


def test_agent_loop_script_never_calls_operations_mutation_or_preview_functions():
    """count_rows (a read-only row-count diagnostic, required by Part J to
    verify the deterministic reset) is legitimately imported and called --
    it is not a consequential Operations business function. The actual
    mutation/preview functions the guarded pipeline itself calls must
    never be imported or called directly by this script."""
    source = (REPO_ROOT / "scripts" / "agent_loop.py").read_text()
    called = _called_function_names(source)
    forbidden = {"delete_users", "preview_delete_users", "deactivate_users", "preview_deactivate_users"}
    assert not (called & forbidden)


def test_agent_loop_script_never_imports_app_or_app_logic():
    source = (REPO_ROOT / "scripts" / "agent_loop.py").read_text()
    imported = _imported_module_names(source)
    assert "app" not in imported
    assert "app_logic" not in imported


def test_agent_proposal_module_never_imports_guarded_execute_or_operations():
    source = (REPO_ROOT / "agent" / "agent_proposal.py").read_text()
    imported = _imported_module_names(source)
    assert "proofgate.core" not in imported
    assert "operations.actions" not in imported
    assert "guarded_execute" not in source


def test_agent_loop_script_uses_real_mcp_stdio_client_transport():
    source = (REPO_ROOT / "scripts" / "agent_loop.py").read_text()
    assert "mcp.client.stdio" in source
    assert "stdio_client" in source
    assert "ClientSession" in source
    # Must not invent a private generic tool-name execution endpoint.
    assert "guarded_execute" not in _called_function_names(source)


def test_agent_loop_script_does_not_hardcode_a_duplicate_tool_list_as_source_of_truth():
    source = (REPO_ROOT / "scripts" / "agent_loop.py").read_text()
    # The only acceptable literal appearances of both names together are in
    # comments/docstrings/startup-assertion text -- there must be no
    # variable assigned a literal ["delete_users", "deactivate_users"] list
    # used as the tools passed to the proposal step. Discovery must flow
    # through discovered_tools_from_mcp(tools_result.tools).
    assert "discovered_tools_from_mcp(tools_result.tools)" in source
    assert '["delete_users", "deactivate_users"]' not in source


# ---------------------------------------------------------------------------
# Regression safety
# ---------------------------------------------------------------------------


def test_app_py_is_not_imported_by_agent_loop_or_agent_proposal():
    for path in (REPO_ROOT / "scripts" / "agent_loop.py", REPO_ROOT / "agent" / "agent_proposal.py"):
        source = path.read_text()
        assert "import app\n" not in source
        assert "from app import" not in source
        assert "from app_logic import" not in source


def test_git_diff_confirms_app_py_and_app_logic_unchanged():
    result = subprocess.run(
        ["git", "diff", "--stat", "--", "app.py", "app_logic.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.stdout.strip() == ""
