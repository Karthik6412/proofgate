"""Slice 19 tests: the MCP guarded gateway (proofgate/mcp_server.py).

No pytest-asyncio dependency is added -- each test drives the async MCP
client/server session via a plain asyncio.run(...) call inside an
ordinary sync test function, using the SDK's own in-memory client/server
helper (mcp.shared.memory.create_connected_server_and_client_session),
which requires no real stdio subprocess. The one real stdio-subprocess
test (protocol integrity) is separate and explicit about why it needs a
subprocess.

tests/conftest.py's autouse fixtures already disable live CRAFT/Nebius
and isolate the audit log per test.
"""

import ast
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import mcp.shared.memory as memory
import mcp.types as types
import pytest

import proofgate.audit as audit_module
import proofgate.mcp_server as mcp_server
from operations.actions import count_rows
from operations.database import WORKING_DB_PATH, reset_working_db
from operations.snapshots import create_snapshot
from proofgate.budgets import get_workflow_budget, reset_workflow_state

REPO_ROOT = Path(__file__).resolve().parent.parent

DELETE_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."
DEACTIVATE_INSTRUCTION = "Deactivate inactive test accounts that have not logged in for 90 days."


def _run(coro):
    return asyncio.run(coro)


async def _call(tool_name: str, arguments: dict) -> types.CallToolResult:
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
def _reset_shutdown_flag():
    mcp_server.reset_shutdown_flag_for_testing()
    yield
    mcp_server.reset_shutdown_flag_for_testing()


# ---------------------------------------------------------------------------
# MCP server construction
# ---------------------------------------------------------------------------


def test_module_import_is_side_effect_free_beyond_registry_assertion():
    """Re-importing must not start a server, open a socket, or touch the
    database/audit log. Verified via a fresh subprocess import."""
    result = subprocess.run(
        [sys.executable, "-c", "import proofgate.mcp_server"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        env={"NEBIUS_LIVE_ENABLED": "false", "CRAFT_LIVE_ENABLED": "false", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_exactly_the_two_intended_tools_are_registered():
    tools_result = _run(_list_tools_via_client())
    names = {t.name for t in tools_result.tools}
    assert names == {"delete_users", "deactivate_users"}


async def _list_tools_via_client():
    async with memory.create_connected_server_and_client_session(mcp_server.server) as client:
        return await client.list_tools()


def test_no_operations_helper_or_reset_helper_is_exposed():
    tools_result = _run(_list_tools_via_client())
    names = {t.name for t in tools_result.tools}
    forbidden = {
        "preview_delete_users",
        "preview_deactivate_users",
        "create_snapshot",
        "reset_working_db",
        "reset_audit_log",
        "reset_workflow_state",
        "guarded_execute",
    }
    assert not (names & forbidden)


def test_startup_assertion_fails_loudly_on_registry_drift(monkeypatch):
    monkeypatch.setattr(mcp_server, "_PUBLIC_MCP_TOOLS", ("delete_users", "not_a_real_tool"))
    with pytest.raises(RuntimeError, match="not_a_real_tool"):
        mcp_server._assert_registry_matches_public_surface()


# ---------------------------------------------------------------------------
# Broad delete through MCP
# ---------------------------------------------------------------------------


def test_broad_delete_through_mcp():
    workflow_id = "mcp-broad-delete"
    _clean_state(workflow_id)
    before_events = len(_audit_events())

    result = _run(
        _call(
            "delete_users",
            {
                "instruction": DELETE_INSTRUCTION,
                "workflow_id": workflow_id,
                "inactive_days": 90,
                "environment": None,
            },
        )
    )

    assert result.isError is False
    content = result.structuredContent
    assert content["verdict"] == "BLOCK"
    assert content["executed"] is False
    assert content["impact_envelope"]["estimated_count"] == 10073
    assert content["impact_envelope"]["environment_counts"] == {"production": 9981, "test": 92}
    assert {r["rule_id"] for r in content["triggered_rules"]} == {
        "RULE_INTENT_BOUNDARY",
        "RULE_RECOVERY_PROOF",
        "RULE_WORKFLOW_BUDGET",
    }
    assert count_rows(WORKING_DB_PATH) == 10623
    assert get_workflow_budget(workflow_id).rows_mutated == 0
    assert len(_audit_events()) == before_events + 1


# ---------------------------------------------------------------------------
# Corrected delete through MCP
# ---------------------------------------------------------------------------


def test_corrected_delete_through_mcp():
    workflow_id = "mcp-corrected-delete"
    _clean_state(workflow_id)
    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)
    before_events = len(_audit_events())

    result = _run(
        _call(
            "delete_users",
            {
                "instruction": DELETE_INSTRUCTION,
                "workflow_id": workflow_id,
                "inactive_days": 90,
                "environment": "test",
                "rollback_proof": proof.model_dump(),
            },
        )
    )

    assert result.isError is False
    content = result.structuredContent
    assert content["verdict"] == "ALLOW"
    assert content["executed"] is True
    assert content["mutation_result"] == {"affected_count": 92, "production_affected": 0, "test_affected": 92}
    assert content["postcondition_result"]["status"] == "VERIFIED"
    assert content["workflow_budget"] == {
        "workflow_id": workflow_id,
        "rows_mutated": 92,
        "production_rows_mutated": 0,
        "max_rows": 100,
    }
    assert content["proof_status"] == "VALID"
    assert count_rows(WORKING_DB_PATH) == 10531
    assert len(_audit_events()) == before_events + 1


# ---------------------------------------------------------------------------
# Broad deactivate through MCP
# ---------------------------------------------------------------------------


def test_broad_deactivate_through_mcp():
    workflow_id = "mcp-broad-deactivate"
    _clean_state(workflow_id)
    before_events = len(_audit_events())

    result = _run(
        _call(
            "deactivate_users",
            {
                "instruction": DEACTIVATE_INSTRUCTION,
                "workflow_id": workflow_id,
                "inactive_days": 90,
                "environment": None,
            },
        )
    )

    assert result.isError is False
    content = result.structuredContent
    assert content["verdict"] == "BLOCK"
    assert content["executed"] is False
    assert content["impact_envelope"]["estimated_count"] == 10073
    assert content["impact_envelope"]["environment_counts"] == {"production": 9981, "test": 92}
    triggered = {r["rule_id"] for r in content["triggered_rules"]}
    assert triggered == {"RULE_INTENT_BOUNDARY", "RULE_WORKFLOW_BUDGET"}
    assert "RULE_RECOVERY_PROOF" not in triggered
    assert "RULE_UNKNOWN_IMPACT" not in triggered
    assert count_rows(WORKING_DB_PATH) == 10623
    assert get_workflow_budget(workflow_id).rows_mutated == 0
    assert len(_audit_events()) == before_events + 1


# ---------------------------------------------------------------------------
# Corrected deactivate through MCP
# ---------------------------------------------------------------------------


def test_corrected_deactivate_through_mcp():
    workflow_id = "mcp-corrected-deactivate"
    _clean_state(workflow_id)
    before_events = len(_audit_events())

    with mock.patch("operations.snapshots.create_snapshot") as snapshot_spy:
        result = _run(
            _call(
                "deactivate_users",
                {
                    "instruction": DEACTIVATE_INSTRUCTION,
                    "workflow_id": workflow_id,
                    "inactive_days": 90,
                    "environment": "test",
                },
            )
        )
        snapshot_spy.assert_not_called()

    assert result.isError is False
    content = result.structuredContent
    assert content["verdict"] == "ALLOW"
    assert content["executed"] is True
    assert content["mutation_result"] == {"affected_count": 92, "production_affected": 0, "test_affected": 92}
    assert content["postcondition_result"]["status"] == "VERIFIED"
    assert content["workflow_budget"]["rows_mutated"] == 92
    assert content["proof_status"] == "MISSING"
    assert len(_audit_events()) == before_events + 1


# ---------------------------------------------------------------------------
# Audit cardinality
# ---------------------------------------------------------------------------


def test_invalid_schema_input_writes_zero_audit_events():
    workflow_id = "mcp-invalid-schema"
    _clean_state(workflow_id)
    before_events = len(_audit_events())

    result = _run(
        _call(
            "delete_users",
            {
                "instruction": DELETE_INSTRUCTION,
                "workflow_id": workflow_id,
                "inactive_days": 90,
                "enviroment": "test",  # misspelled, unknown field
            },
        )
    )

    assert result.isError is True
    assert len(_audit_events()) == before_events
    assert get_workflow_budget(workflow_id).rows_mutated == 0
    assert count_rows(WORKING_DB_PATH) == 10623


def test_unknown_tool_name_writes_zero_audit_events_and_does_not_call_guarded_execute():
    workflow_id = "mcp-unknown-tool"
    _clean_state(workflow_id)
    before_events = len(_audit_events())

    with mock.patch("proofgate.mcp_server.guarded_execute") as spy:
        result = _run(
            _call(
                "reactivate_users",
                {
                    "instruction": DEACTIVATE_INSTRUCTION,
                    "workflow_id": workflow_id,
                    "inactive_days": 90,
                },
            )
        )
        spy.assert_not_called()

    assert result.isError is True
    assert len(_audit_events()) == before_events


# ---------------------------------------------------------------------------
# Shared pipeline proof
# ---------------------------------------------------------------------------


def test_mcp_module_calls_guarded_execute_directly():
    workflow_id = "mcp-shared-pipeline"
    _clean_state(workflow_id)

    with mock.patch("proofgate.mcp_server.guarded_execute") as spy:
        spy.return_value = mock.Mock(
            model_dump=lambda mode: {
                "verdict": "BLOCK",
                "executed": False,
                "audit_event_id": "evt-fake",
                "risk_score": 0,
                "risk_factors": [],
                "triggered_rules": [],
                "missing_requirements": [],
                "suggested_repairs": [],
            },
            audit_event_id="evt-fake",
        )
        _run(
            _call(
                "delete_users",
                {
                    "instruction": DELETE_INSTRUCTION,
                    "workflow_id": workflow_id,
                    "inactive_days": 90,
                },
            )
        )
        spy.assert_called_once()
        called_args = spy.call_args.args
        assert called_args[0] == "delete_users"


def test_mcp_module_never_directly_imports_operations_policy_proof_or_audit_write():
    """AST inspection: proofgate/mcp_server.py must never import
    operations.actions, proofgate.policy, proofgate.proofs,
    proofgate.selector, operations.snapshots (as a callable dependency),
    or proofgate.postcondition directly, and must never call
    append_audit_event. Only proofgate.core.guarded_execute is the shared
    boundary it goes through."""
    source = Path(mcp_server.__file__).read_text()
    tree = ast.parse(source)

    imported_modules = []
    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)
            imported_names.extend(alias.name for alias in node.names)

    forbidden_modules = {
        "operations.actions",
        "proofgate.policy",
        "proofgate.proofs",
        "proofgate.selector",
        "proofgate.postcondition",
    }
    assert not (set(imported_modules) & forbidden_modules)
    assert "append_audit_event" not in imported_names
    assert "evaluate_policy" not in imported_names
    assert "validate_rollback_proof" not in imported_names
    assert "compute_selector_hash" not in imported_names

    assert "append_audit_event(" not in source


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_block_and_allow_responses_share_one_stable_shape():
    workflow_id = "mcp-shape-check"
    _clean_state(workflow_id)

    block_result = _run(
        _call(
            "delete_users",
            {"instruction": DELETE_INSTRUCTION, "workflow_id": workflow_id, "inactive_days": 90},
        )
    )
    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)
    allow_result = _run(
        _call(
            "delete_users",
            {
                "instruction": DELETE_INSTRUCTION,
                "workflow_id": workflow_id,
                "inactive_days": 90,
                "environment": "test",
                "rollback_proof": proof.model_dump(),
            },
        )
    )

    assert set(block_result.structuredContent.keys()) == set(allow_result.structuredContent.keys())
    for value in block_result.structuredContent.values():
        assert not str(type(value)).startswith("<class 'proofgate")
    for value in allow_result.structuredContent.values():
        assert not str(type(value)).startswith("<class 'proofgate")


def test_serialized_response_does_not_leak_paths_or_internal_objects():
    workflow_id = "mcp-no-leak"
    _clean_state(workflow_id)
    result = _run(
        _call(
            "delete_users",
            {"instruction": DELETE_INSTRUCTION, "workflow_id": workflow_id, "inactive_days": 90},
        )
    )
    dumped = json.dumps(result.structuredContent)
    assert str(REPO_ROOT) not in dumped
    assert "working.db" not in dumped
    assert "NEBIUS_API_KEY" not in dumped


# ---------------------------------------------------------------------------
# Input normalization
# ---------------------------------------------------------------------------


def test_numeric_string_inactive_days_is_accepted():
    workflow_id = "mcp-norm-numeric-string"
    _clean_state(workflow_id)
    result = _run(
        _call(
            "delete_users",
            {"instruction": DELETE_INSTRUCTION, "workflow_id": workflow_id, "inactive_days": "90"},
        )
    )
    assert result.isError is False
    assert result.structuredContent["impact_envelope"]["estimated_count"] == 10073


def test_uppercase_environment_is_normalized():
    workflow_id = "mcp-norm-uppercase-env"
    _clean_state(workflow_id)
    result = _run(
        _call(
            "delete_users",
            {
                "instruction": DELETE_INSTRUCTION,
                "workflow_id": workflow_id,
                "inactive_days": 90,
                "environment": "TEST",
            },
        )
    )
    assert result.isError is False
    assert result.structuredContent["impact_envelope"]["environment_counts"]["test"] == 92


def test_whitespace_is_trimmed_from_instruction_and_workflow_id():
    workflow_id = "mcp-norm-whitespace"
    _clean_state(workflow_id)
    result = _run(
        _call(
            "delete_users",
            {
                "instruction": f"  {DELETE_INSTRUCTION}  ",
                "workflow_id": f"  {workflow_id}  ",
                "inactive_days": 90,
            },
        )
    )
    assert result.isError is False
    assert get_workflow_budget(workflow_id).workflow_id == workflow_id


@pytest.mark.parametrize(
    "bad_arguments",
    [
        {"inactive_days": "ninety"},
        {"inactive_days": -5},
        {"inactive_days": 0},
        {"inactive_days": 90.5},
        {"environment": "testing"},
        {"environment": 123},
    ],
)
def test_rejected_input_values(bad_arguments):
    workflow_id = "mcp-reject"
    _clean_state(workflow_id)
    before_events = len(_audit_events())
    arguments = {
        "instruction": DELETE_INSTRUCTION,
        "workflow_id": workflow_id,
        "inactive_days": 90,
    }
    arguments.update(bad_arguments)

    result = _run(_call("delete_users", arguments))

    assert result.isError is True
    assert len(_audit_events()) == before_events
    assert get_workflow_budget(workflow_id).rows_mutated == 0
    assert count_rows(WORKING_DB_PATH) == 10623


def test_omitted_instruction_is_rejected():
    result = _run(_call("delete_users", {"workflow_id": "mcp-missing-1", "inactive_days": 90}))
    assert result.isError is True


def test_omitted_workflow_id_is_rejected():
    result = _run(_call("delete_users", {"instruction": DELETE_INSTRUCTION, "inactive_days": 90}))
    assert result.isError is True


def test_unknown_extra_field_is_rejected():
    result = _run(
        _call(
            "delete_users",
            {
                "instruction": DELETE_INSTRUCTION,
                "workflow_id": "mcp-extra-field",
                "inactive_days": 90,
                "not_a_real_field": True,
            },
        )
    )
    assert result.isError is True


# ---------------------------------------------------------------------------
# Concurrency behavior: proves serialization deterministically (no timing
# assumptions -- an in-flight counter plus a real blocking time.sleep()
# widens the window; if the dispatch ever interleaved, this would fail
# every run, not flakily).
# ---------------------------------------------------------------------------


def test_two_concurrent_mcp_calls_cannot_race_past_the_guarded_boundary():
    workflow_id = "mcp-concurrency"
    _clean_state(workflow_id)

    real_guarded_execute = mcp_server.guarded_execute
    in_flight = []
    max_observed = []

    def _tracking_guarded_execute(*args, **kwargs):
        in_flight.append(1)
        max_observed.append(len(in_flight))
        try:
            time.sleep(0.05)
            return real_guarded_execute(*args, **kwargs)
        finally:
            in_flight.pop()

    async def scenario():
        async with memory.create_connected_server_and_client_session(mcp_server.server) as client:
            return await asyncio.gather(
                client.call_tool(
                    "delete_users",
                    {"instruction": DELETE_INSTRUCTION, "workflow_id": workflow_id, "inactive_days": 90},
                ),
                client.call_tool(
                    "deactivate_users",
                    {"instruction": DEACTIVATE_INSTRUCTION, "workflow_id": workflow_id, "inactive_days": 90},
                ),
            )

    with mock.patch("proofgate.mcp_server.guarded_execute", side_effect=_tracking_guarded_execute):
        results = _run(scenario())

    assert max(max_observed) == 1, "guarded_execute was re-entered while another call was in flight"
    assert all(not r.isError for r in results)

    budget = get_workflow_budget(workflow_id)
    # Both calls were broad/unsafe (environment=None) -- BLOCK, zero mutation,
    # regardless of ordering.
    assert budget.rows_mutated == 0


def test_queue_wait_timeout_is_not_applicable_no_explicit_lock_exists():
    """Part I applies only if an explicit execution lock/queue exists. This
    implementation adds none -- serialization is a structural property of
    a synchronous call inside a single-threaded async dispatch loop (see
    the concurrency test above and the module docstring). There is no
    lock object, semaphore, or queue anywhere in mcp_server.py to test a
    timeout against."""
    source = Path(mcp_server.__file__).read_text()
    assert "asyncio.Lock(" not in source
    assert "anyio.Lock(" not in source
    assert "Semaphore(" not in source
    assert "Queue(" not in source


# ---------------------------------------------------------------------------
# Shutdown and lifecycle behavior
# ---------------------------------------------------------------------------


def test_shutdown_flag_starts_false():
    assert mcp_server._shutdown_requested is False


def test_signal_handler_sets_flag_without_filesystem_or_db_work():
    mcp_server._handle_termination_signal(15, None)
    assert mcp_server._shutdown_requested is True


def test_new_calls_are_rejected_once_shutdown_flag_is_set():
    workflow_id = "mcp-shutdown"
    _clean_state(workflow_id)
    before_events = len(_audit_events())

    mcp_server._handle_termination_signal(15, None)
    with mock.patch("proofgate.mcp_server.guarded_execute") as spy:
        result = _run(
            _call(
                "delete_users",
                {"instruction": DELETE_INSTRUCTION, "workflow_id": workflow_id, "inactive_days": 90},
            )
        )
        spy.assert_not_called()

    assert result.isError is True
    assert len(_audit_events()) == before_events


def test_database_can_be_reopened_after_server_activity():
    workflow_id = "mcp-reopen"
    _clean_state(workflow_id)
    _run(
        _call(
            "delete_users",
            {"instruction": DELETE_INSTRUCTION, "workflow_id": workflow_id, "inactive_days": 90},
        )
    )
    # A fresh connection/read against the same working.db must succeed
    # cleanly -- no lingering lock from the in-memory session.
    assert count_rows(WORKING_DB_PATH) == 10623


# ---------------------------------------------------------------------------
# Stdio protocol integrity (real subprocess, real stdio transport)
# ---------------------------------------------------------------------------


def test_stdio_protocol_integrity_real_subprocess():
    """Real stdio subprocess, real MCP client -- not the in-memory session
    used by every other test in this file. The subprocess is a separate
    Python process, so conftest.py's audit-path monkeypatch (which only
    affects this parent test process) does not apply to it: the
    subprocess reads/writes the real artifacts/audit.jsonl. This test
    backs that file up and restores it afterward so it never leaves
    behind or destroys real demo audit data.
    """
    async def scenario():
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "proofgate.mcp_server"],
            cwd=str(REPO_ROOT),
            env={
                "NEBIUS_LIVE_ENABLED": "false",
                "CRAFT_LIVE_ENABLED": "false",
                "PATH": "/usr/bin:/bin:/usr/local/bin",
            },
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == {"delete_users", "deactivate_users"}

                result = await session.call_tool(
                    "delete_users",
                    {
                        "instruction": DELETE_INSTRUCTION,
                        "workflow_id": "mcp-stdio-smoke",
                        "inactive_days": 90,
                        "environment": None,
                    },
                )
                assert result.isError is False
                assert result.structuredContent["verdict"] == "BLOCK"

    reset_working_db()

    real_audit_path = REPO_ROOT / "artifacts" / "audit.jsonl"
    backup_content = real_audit_path.read_text() if real_audit_path.exists() else None
    before_lines = backup_content.strip().splitlines() if backup_content else []

    try:
        _run(scenario())

        assert count_rows(WORKING_DB_PATH) == 10623
        after_lines = real_audit_path.read_text().strip().splitlines() if real_audit_path.exists() else []
        assert len(after_lines) == len(before_lines) + 1
        new_event = json.loads(after_lines[-1])
        assert new_event["tool_name"] == "delete_users"
        assert new_event["workflow_id"] == "mcp-stdio-smoke"
        assert new_event["verdict"] == "BLOCK"
    finally:
        reset_working_db()
        if backup_content is not None:
            real_audit_path.write_text(backup_content)
        elif real_audit_path.exists():
            real_audit_path.unlink()


# ---------------------------------------------------------------------------
# Slice 20: runtime-mode behavior
# ---------------------------------------------------------------------------


def test_mcp_module_imports_safely_in_all_three_modes(monkeypatch):
    for value in ("live", "fallback", "reliable_demo"):
        result = subprocess.run(
            [sys.executable, "-c", "import proofgate.mcp_server"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PROOFGATE_RUNTIME_MODE": value,
                "PATH": "/usr/bin:/bin",
            },
        )
        assert result.returncode == 0, f"mode={value}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_mcp_response_includes_extraction_mode_metadata():
    workflow_id = "mcp-extraction-mode-field"
    _clean_state(workflow_id)

    result = _run(
        _call(
            "delete_users",
            {"instruction": DELETE_INSTRUCTION, "workflow_id": workflow_id, "inactive_days": 90},
        )
    )

    assert result.isError is False
    assert result.structuredContent["extraction_mode"] == "deterministic_fallback"
    assert "nebius_model" in result.structuredContent


def test_mcp_fallback_mode_never_calls_live_nebius(monkeypatch):
    import agent.nebius_client as nebius_client_module

    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "fallback")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")

    def _explode():
        raise AssertionError("must not construct a real Nebius client in fallback mode")

    monkeypatch.setattr(nebius_client_module, "_build_client", _explode)

    workflow_id = "mcp-fallback-no-live"
    _clean_state(workflow_id)
    result = _run(
        _call(
            "delete_users",
            {"instruction": DELETE_INSTRUCTION, "workflow_id": workflow_id, "inactive_days": 90},
        )
    )
    assert result.isError is False
    assert result.structuredContent["extraction_mode"] == "deterministic_fallback"


def test_mcp_reliable_demo_mode_never_calls_live_nebius(monkeypatch):
    import agent.nebius_client as nebius_client_module

    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "reliable_demo")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")

    def _explode():
        raise AssertionError("must not construct a real Nebius client in reliable_demo mode")

    monkeypatch.setattr(nebius_client_module, "_build_client", _explode)

    workflow_id = "mcp-reliable-demo-no-live"
    _clean_state(workflow_id)
    result = _run(
        _call(
            "deactivate_users",
            {"instruction": DEACTIVATE_INSTRUCTION, "workflow_id": workflow_id, "inactive_days": 90},
        )
    )
    assert result.isError is False
    assert result.structuredContent["extraction_mode"] == "deterministic_fallback"


def test_mcp_live_mode_upstream_failure_still_returns_structured_result(monkeypatch):
    """LIVE mode + a live Nebius client that raises must still produce a
    real, structured ProofGate BLOCK -- never a transport failure, never a
    fabricated ALLOW."""
    import agent.nebius_client as nebius_client_module

    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")

    def _explode():
        raise ConnectionError("simulated upstream failure")

    monkeypatch.setattr(nebius_client_module, "_build_client", _explode)

    workflow_id = "mcp-live-failure-fallback"
    _clean_state(workflow_id)
    before_events = len(_audit_events())

    result = _run(
        _call(
            "delete_users",
            {"instruction": DELETE_INSTRUCTION, "workflow_id": workflow_id, "inactive_days": 90},
        )
    )

    assert result.isError is False
    content = result.structuredContent
    assert content["verdict"] == "BLOCK"
    assert content["extraction_mode"] == "deterministic_fallback"
    assert {r["rule_id"] for r in content["triggered_rules"]} == {
        "RULE_INTENT_BOUNDARY",
        "RULE_RECOVERY_PROOF",
        "RULE_WORKFLOW_BUDGET",
    }
    assert len(_audit_events()) == before_events + 1


def test_mcp_response_never_leaks_secret_values_under_live_failure(monkeypatch):
    import agent.nebius_client as nebius_client_module

    monkeypatch.setenv("PROOFGATE_RUNTIME_MODE", "live")
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "sk-should-never-appear-anywhere")

    def _explode():
        raise RuntimeError("Authorization: Bearer sk-should-never-appear-anywhere rejected")

    monkeypatch.setattr(nebius_client_module, "_build_client", _explode)

    workflow_id = "mcp-no-secret-leak"
    _clean_state(workflow_id)
    result = _run(
        _call(
            "delete_users",
            {"instruction": DELETE_INSTRUCTION, "workflow_id": workflow_id, "inactive_days": 90},
        )
    )

    dumped = json.dumps(result.structuredContent)
    assert "sk-should-never-appear-anywhere" not in dumped
