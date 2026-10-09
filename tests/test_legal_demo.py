"""Friday v1 packaging: the deterministic beats 1-7 terminal demo.

Structured results are checked against the production flow directly;
the rendered output is checked for beat order, the decisive lines, and
the absence of restricted text, tracebacks and nondeterminism.
"""

import ast
import socket
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import scripts.legal_demo as demo
from legal.corpus import build_corpus
from legal.models import AgentCapabilities
from proofgate.bindings.legal.authorization import AuthorizationDigestMismatch, action_digest
from proofgate.bindings.legal.authorize import LegalAuthorizationStatus
from proofgate.bindings.legal.plan import CANONICAL_PLAN
from proofgate.bindings.legal.resolver import resolve_plan

REPO_ROOT = Path(demo.__file__).resolve().parent.parent
CLEAN_DIGEST = "cd4ff5e2544c75e2ee62a0323384f28c83fc603158287654c5e596637b050b47"
CHECK_ORDER = ["user_matter_access", "agent_may_search", "agent_may_package", "recipient_permitted"]


@pytest.fixture
def result(tmp_path):
    return demo.run_demo(tmp_path / "decisions.jsonl")


def _run_script() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/legal_demo.py"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
    )


# ---------------------------------------------------------------------------
# Structured results come from the production flow
# ---------------------------------------------------------------------------


def test_canonical_facts_come_from_the_production_resolver(result):
    assert result.canonical.effect == resolve_plan(CANONICAL_PLAN)
    dims = result.canonical.effect.dimensions
    assert result.canonical.effect.total_affected == 143
    assert dict(dims.matters_touched) == {"M-102": 126, "M-118": 17}
    assert (dims.privileged_count, dims.restricted_count) == (9, 4)


def test_beat_four_is_exactly_four_real_passing_checks(result):
    checks = result.canonical.permissions.checks

    assert [check.check_id for check in checks] == CHECK_ORDER
    assert [check.status for check in checks] == ["PASS"] * 4


def test_canonical_verdict_is_block_without_authorization(result):
    assert result.canonical.status is LegalAuthorizationStatus.EVALUATED
    assert result.canonical.verdict == "BLOCK"
    assert result.canonical_authorization is None
    assert result.canonical_record.decision == "BLOCK"
    assert [rule.rule_id for rule in result.canonical.decision.triggered_rules] == [
        "RULE_MATTER_BOUNDARY",
        "RULE_PRIVILEGE_BOUNDARY",
        "RULE_WORKFLOW_BUDGET",
    ]


def test_clean_path_is_authorized_and_the_untouched_action_verifies(result):
    assert result.clean.verdict == "ALLOW"
    assert result.tamper.authorization.action_digest == CLEAN_DIGEST
    assert result.tamper.untouched_verified is True


def test_the_altered_resource_set_is_rejected_by_real_verification(result):
    mismatch = result.tamper.mismatch

    assert isinstance(mismatch, AuthorizationDigestMismatch)
    assert mismatch.expected == CLEAN_DIGEST
    assert mismatch.actual == action_digest(result.tamper.tampered_action) != CLEAN_DIGEST
    assert demo.SLIPPED_IN_DOC in result.tamper.tampered_action.effect.consequential_resource_ids
    assert result.tamper.tampered_action.effect.total_affected == 81


# ---------------------------------------------------------------------------
# Rendered output
# ---------------------------------------------------------------------------


def test_all_seven_beats_render_in_order(result):
    output = "\n".join(demo.render(result))
    titles = [
        "BEAT 1 -- Human request",
        "BEAT 2 -- Authority grant",
        "BEAT 3 -- Agent plan",
        "BEAT 4 -- Permission checks",
        "BEAT 5 -- Resolved effect",
        "BEAT 6 -- Verdict",
        "BEAT 7 -- Tampered execution",
    ]
    positions = [output.index(title) for title in titles]

    assert positions == sorted(positions)
    assert "BEAT 8" not in output


def test_decisive_lines_are_present(result):
    output = "\n".join(demo.render(result))

    beat_four = output[output.index("BEAT 4") : output.index("BEAT 5")].splitlines()
    assert [line.split()[0] for line in beat_four if line.startswith("  PASS")] == ["PASS"] * 4
    assert "TOOL AND MATTER PERMISSIONS PASS" in output
    assert "M-102: 126, M-118: 17" in output
    assert "FAIL  matter_boundary" in output and "PASS  recipient_boundary" in output
    assert "DECISION: BLOCK" in output
    assert "DIGEST MISMATCH -- AUTHORIZATION FAILS" in output
    assert "The blocked plan never received an authorization." in output
    assert CLEAN_DIGEST[:16] in output


def test_a_failing_permission_check_is_exposed_not_hidden(tmp_path):
    no_search = AgentCapabilities(agent_id="agent-01", allowed_tools=frozenset({"compile_package", "share_package"}))
    result = demo.run_demo(tmp_path / "decisions.jsonl", agent_capabilities=no_search)
    output = "\n".join(demo.render(result))

    assert "FAIL  Agent may search" in output
    assert "TOOL AND MATTER PERMISSIONS PASS" not in output
    assert "PERMISSION CHECK FAILED: Agent may search" in output
    assert "DECISION: BLOCK" in output
    assert "nothing to tamper with" in output


def test_output_contains_no_document_text_or_canary(result):
    output = "\n".join(demo.render(result))

    assert "Synthetic text" not in output and "Acquisition codename" not in output
    for document in build_corpus():
        assert document.text not in output
        if document.canary:
            assert document.canary not in output


# ---------------------------------------------------------------------------
# Fresh-checkout command, determinism, isolation
# ---------------------------------------------------------------------------


def test_script_runs_cleanly_and_identically_twice():
    first, second = _run_script(), _run_script()

    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == ""
    assert "Traceback" not in first.stdout
    assert first.stdout == second.stdout


def test_script_does_not_touch_the_default_decision_log():
    default_log = REPO_ROOT / "artifacts" / "legal_decisions.jsonl"
    before = default_log.read_bytes() if default_log.exists() else None

    assert _run_script().returncode == 0
    assert (default_log.read_bytes() if default_log.exists() else None) == before


def test_demo_needs_no_network(tmp_path):
    with mock.patch.object(socket, "socket", side_effect=AssertionError("network access attempted")):
        result = demo.run_demo(tmp_path / "decisions.jsonl")
    assert result.tamper.mismatch is not None


def test_demo_imports_no_model_or_network_client():
    tree = ast.parse(Path(demo.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = ("agent", "openai", "anthropic", "craft", "mcp", "requests", "httpx", "urllib", "socket")
    assert not [module for module in imported if module.split(".")[0] in forbidden]
