"""Slice 1 / P2: registry ownership is inverted.

proofgate.registry is generic and starts empty; the Operations binding
(proofgate/bindings/operations.py) registers the Operations tools, and
each runtime entrypoint calls it explicitly. Import-time behavior is
checked in fresh subprocesses, since this test process already ran the
module-level registration in tests/conftest.py.

In-process tests that need an empty or modified registry swap in a
private dict via monkeypatch (registry functions read the module-global
_REGISTRY at call time); monkeypatch restores the original at teardown
even on failure, so nothing leaks to other tests.
"""

import ast
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import proofgate.registry as registry
from operations.actions import count_rows
from operations.database import WORKING_DB_PATH, reset_working_db
from proofgate.bindings.operations import OPERATIONS_TOOL_SPECS, register_operations_tools
from proofgate.core import get_workflow_budget, guarded_delete_users, reset_workflow_state
from proofgate.models import ActionContext
from proofgate.policy import RULE_UNKNOWN_IMPACT
from proofgate.registry import get_tool_spec, registered_tool_names

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOLS = ("delete_users", "deactivate_users", "set_feature_flag")
OFFLINE_ENV = {
    "NEBIUS_LIVE_ENABLED": "false",
    "CRAFT_LIVE_ENABLED": "false",
    "PROOFGATE_RUNTIME_MODE": "fallback",
    "PATH": "/usr/bin:/bin",
}


def _run_python(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=OFFLINE_ENV,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return result.stdout.strip().splitlines()[-1]


@pytest.fixture
def empty_registry(monkeypatch):
    original = registry._REGISTRY
    snapshot = dict(original)
    monkeypatch.setattr(registry, "_REGISTRY", {})
    yield
    assert original == snapshot


# ---------------------------------------------------------------------------
# Fresh-process import behavior
# ---------------------------------------------------------------------------


def test_fresh_import_of_registry_alone_is_empty():
    out = _run_python(
        "import proofgate.registry as r; print(r.registered_tool_names())"
    )
    assert out == "()"


def test_fresh_import_of_core_does_not_register_anything():
    out = _run_python(
        "import proofgate.core; import proofgate.registry as r; print(r.registered_tool_names())"
    )
    assert out == "()"


def test_importing_the_binding_alone_registers_nothing():
    out = _run_python(
        "import proofgate.bindings.operations; import proofgate.registry as r; "
        "print(r.registered_tool_names())"
    )
    assert out == "()"


def test_fresh_explicit_registration_gives_exactly_the_three_operations_tools():
    out = _run_python(
        "from proofgate.bindings.operations import register_operations_tools; "
        "register_operations_tools(); import proofgate.registry as r; "
        "print(r.registered_tool_names())"
    )
    assert out == str(EXPECTED_TOOLS)


def test_mcp_server_startup_registers_exactly_the_three_operations_tools():
    out = _run_python(
        "import proofgate.mcp_server; import proofgate.registry as r; print(r.registered_tool_names())"
    )
    assert out == str(EXPECTED_TOOLS)


def test_app_startup_registers_exactly_the_three_operations_tools():
    out = _run_python(
        "import app; import proofgate.registry as r; print(r.registered_tool_names())"
    )
    assert out == str(EXPECTED_TOOLS)


# ---------------------------------------------------------------------------
# In-process registration behavior
# ---------------------------------------------------------------------------


def test_register_operations_tools_produces_the_same_three_specs(empty_registry):
    assert registered_tool_names() == ()
    register_operations_tools()
    assert registered_tool_names() == EXPECTED_TOOLS
    for spec in OPERATIONS_TOOL_SPECS:
        assert get_tool_spec(spec.tool_name) is spec


def test_register_operations_tools_twice_is_safe(empty_registry):
    register_operations_tools()
    first = {name: get_tool_spec(name) for name in EXPECTED_TOOLS}
    register_operations_tools()
    assert registered_tool_names() == EXPECTED_TOOLS
    for name in EXPECTED_TOOLS:
        assert get_tool_spec(name) is first[name]


@pytest.mark.parametrize(
    ("tool_name", "attr", "patch_target", "arguments"),
    [
        ("delete_users", "preview_fn", "operations.actions.preview_delete_users", {"inactive_days": 90, "environment": "test"}),
        ("delete_users", "mutation_fn", "operations.actions.delete_users", {"inactive_days": 90, "environment": "test"}),
        ("deactivate_users", "preview_fn", "operations.actions.preview_deactivate_users", {"inactive_days": 90, "environment": "test"}),
        ("deactivate_users", "mutation_fn", "operations.actions.deactivate_users", {"inactive_days": 90, "environment": "test"}),
        (
            "set_feature_flag",
            "preview_fn",
            "operations.feature_flags.preview_set_feature_flag",
            {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100},
        ),
        (
            "set_feature_flag",
            "mutation_fn",
            "operations.feature_flags.set_feature_flag",
            {"flag_name": "new_checkout", "enabled": True, "environment": "test", "rollout_percentage": 100},
        ),
    ],
)
def test_registered_wrappers_resolve_operations_functions_at_call_time(tool_name, attr, patch_target, arguments):
    fn = getattr(get_tool_spec(tool_name), attr)
    sentinel = object()
    with mock.patch(patch_target, return_value=sentinel) as patched:
        assert fn(**arguments) is sentinel
    patched.assert_called_once_with(**arguments)


def test_unregistered_tool_fails_closed_and_mutates_nothing(empty_registry):
    workflow_id = "wf-p2-empty-registry"
    reset_working_db()
    reset_workflow_state(workflow_id)
    rows_before = count_rows(WORKING_DB_PATH)

    with mock.patch("operations.actions.delete_users") as mutation_spy, mock.patch(
        "operations.actions.preview_delete_users"
    ) as preview_spy:
        result = guarded_delete_users(
            action_context=ActionContext(
                workflow_id=workflow_id,
                requesting_user="test-user",
                agent_id="test-agent",
                original_instruction="Clean up inactive test accounts that have not logged in for 90 days.",
            ),
            inactive_days=90,
            environment="test",
            rollback_proof=None,
        )

    assert result.verdict == "BLOCK"
    assert [rule.rule_id for rule in result.triggered_rules] == [RULE_UNKNOWN_IMPACT]
    assert result.executed is False
    preview_spy.assert_not_called()
    mutation_spy.assert_not_called()
    assert count_rows(WORKING_DB_PATH) == rows_before
    assert get_workflow_budget(workflow_id).rows_mutated == 0


def test_registry_is_back_to_exactly_the_three_tools_after_isolated_tests():
    assert registered_tool_names() == EXPECTED_TOOLS


# ---------------------------------------------------------------------------
# Structural: the generic registry knows no domain
# ---------------------------------------------------------------------------


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append("." * node.level + (node.module or ""))
    return modules


def test_registry_module_imports_no_domain_binding_or_core():
    imported = _imported_modules(Path(registry.__file__))
    forbidden_prefixes = ("operations", "craft", "agent", "legal", "proofgate.bindings", "proofgate.core")
    offenders = [
        name
        for name in imported
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden_prefixes)
    ]
    assert offenders == []
    assert set(imported) == {"dataclasses", "typing", "proofgate.models"}


def test_registry_module_defines_no_operations_wrappers_or_entries():
    source = Path(registry.__file__).read_text()
    for token in ("operations_actions", "operations_feature_flags", "_delete_users_preview", "delete_users"):
        assert token not in source
