"""Slice 26: one polished, deterministic portfolio-demo runner.

A thin orchestrator, not a reimplementation: the rollback and feature-flag
sections import and call the existing scripts.rollback_demo._run_demo()
and scripts.feature_flag_demo._run_demo() coroutines directly, reusing
their real MCP-gateway orchestration rather than duplicating it. Only the
delete/deactivate/repair section is new orchestration code (no existing
single script already covers that exact combination).

Runs entirely in deterministic fallback mode. Makes zero external calls:
no live Nebius, no live CRAFT, no real feature-flag provider. The one-shot
repair section uses a fake OpenAI-compatible client (the same technique
tests/test_agent_repair.py already uses) as an explicitly labeled,
deterministic stand-in for the live repair-proposal model call --
Slice 22's DEMO.md section documents a real, live 5-run experiment of
that exact call separately; this script proves the trusted-code path
(validation, trusted request construction, real MCP submission) without
requiring credentials.

Run it with:

    source .venv/bin/activate
    python scripts/run_portfolio_demo.py

Accepts no arguments. Resets all generated state before and after.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sys
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent.agent_proposal import AgentToolProposal, DiscoveredTool, ProposedMutationArguments  # noqa: E402
from agent.agent_repair import SanitizedRepairFeedback, propose_repair  # noqa: E402
from operations.database import reset_working_db  # noqa: E402
from operations.feature_flags import reset_feature_flags_working  # noqa: E402
from operations.snapshots import create_snapshot  # noqa: E402
from proofgate.audit import reset_audit_log  # noqa: E402
from proofgate.budgets import reset_workflow_state  # noqa: E402

REAL_AUDIT_PATH = REPO_ROOT / "artifacts" / "audit.jsonl"

DELETE_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."
DEACTIVATE_INSTRUCTION = "Deactivate inactive test accounts that have not logged in for 90 days."


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _DeterministicRepairStandIn:
    """Explicitly labeled, deterministic stand-in for the live
    repair-proposal model call -- never a real network request. Returns
    a fixed proposal that switches to the reversible tool, matching what
    Slice 22's real 5-run live experiment actually observed (see DEMO.md's
    "One bounded repair attempt" section for the live-verified version of
    this exact call)."""

    def __init__(self) -> None:
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        payload = {
            "tool_name": "deactivate_users",
            "arguments": {"inactive_days": 90, "environment": "test"},
            "explanation": "Deactivation is reversible and does not require recovery proof.",
        }
        return _FakeResponse(json.dumps(payload))


def _summary(label: str, response: dict) -> dict:
    return {
        "label": label,
        "verdict": response["verdict"],
        "triggered_rules": [r["rule_id"] for r in response["triggered_rules"]],
        "executed": response["executed"],
        "impact": (response.get("impact_envelope") or {}).get("estimated_count"),
    }


async def _run_delete_deactivate_and_repair(session) -> list[dict]:
    results = []

    # 1. Broad delete -> BLOCK
    wf1 = f"portfolio-{uuid.uuid4().hex[:8]}"
    reset_workflow_state(wf1)
    reset_working_db()
    r1 = await session.call_tool(
        "delete_users",
        {"instruction": DELETE_INSTRUCTION, "workflow_id": wf1, "inactive_days": 90, "environment": None},
    )
    results.append(_summary("Broad delete", r1.structuredContent))

    # 2. Corrected delete without proof -> BLOCK
    wf2 = f"portfolio-{uuid.uuid4().hex[:8]}"
    reset_workflow_state(wf2)
    reset_working_db()
    r2 = await session.call_tool(
        "delete_users",
        {"instruction": DELETE_INSTRUCTION, "workflow_id": wf2, "inactive_days": 90, "environment": "test"},
    )
    results.append(_summary("Corrected delete without proof", r2.structuredContent))

    # 3. Corrected delete with valid proof -> ALLOW
    wf3 = f"portfolio-{uuid.uuid4().hex[:8]}"
    reset_workflow_state(wf3)
    reset_working_db()
    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)
    r3 = await session.call_tool(
        "delete_users",
        {
            "instruction": DELETE_INSTRUCTION,
            "workflow_id": wf3,
            "inactive_days": 90,
            "environment": "test",
            "rollback_proof": {
                "snapshot_id": proof.snapshot_id,
                "resource": proof.resource,
                "selector_hash": proof.selector_hash,
                "max_affected_rows": proof.max_affected_rows,
            },
        },
    )
    results.append(_summary("Corrected delete with valid proof", r3.structuredContent))

    # 4. Broad deactivate -> BLOCK
    wf4 = f"portfolio-{uuid.uuid4().hex[:8]}"
    reset_workflow_state(wf4)
    reset_working_db()
    r4 = await session.call_tool(
        "deactivate_users",
        {"instruction": DEACTIVATE_INSTRUCTION, "workflow_id": wf4, "inactive_days": 90, "environment": None},
    )
    results.append(_summary("Broad deactivate", r4.structuredContent))

    # 5. Corrected deactivate -> ALLOW (no proof required, reversible)
    wf5 = f"portfolio-{uuid.uuid4().hex[:8]}"
    reset_workflow_state(wf5)
    reset_working_db()
    r5 = await session.call_tool(
        "deactivate_users",
        {"instruction": DEACTIVATE_INSTRUCTION, "workflow_id": wf5, "inactive_days": 90, "environment": "test"},
    )
    results.append(_summary("Corrected deactivate", r5.structuredContent))

    # 6. One-shot repair: initial broad delete BLOCK -> repaired deactivate ALLOW
    wf6 = f"portfolio-{uuid.uuid4().hex[:8]}"
    reset_workflow_state(wf6)
    reset_working_db()
    initial = await session.call_tool(
        "delete_users",
        {"instruction": DELETE_INSTRUCTION, "workflow_id": wf6, "inactive_days": 90, "environment": None},
    )
    initial_payload = initial.structuredContent
    results.append(_summary("One-shot repair: initial delete", initial_payload))

    original_proposal = AgentToolProposal(
        tool_name="delete_users",
        arguments=ProposedMutationArguments(inactive_days=90, environment=None),
        explanation="Broad cleanup as literally requested.",
    )
    feedback = SanitizedRepairFeedback(
        verdict="BLOCK",
        triggered_rules=[r["rule_id"] for r in initial_payload["triggered_rules"]],
        suggested_repairs=initial_payload["suggested_repairs"],
    )
    discovered_tools = [
        DiscoveredTool(name=t.name, description=t.description, input_schema=t.inputSchema)
        for t in (await session.list_tools()).tools
    ]
    repair_outcome = propose_repair(
        DELETE_INSTRUCTION, original_proposal, feedback, discovered_tools, client=_DeterministicRepairStandIn()
    )
    assert repair_outcome.status == "VALID_REPAIR", "deterministic repair stand-in failed to validate"
    repaired = repair_outcome.proposal

    repaired_result = await session.call_tool(
        repaired.tool_name,
        {
            "instruction": DELETE_INSTRUCTION,  # unchanged original instruction, per Slice 22's trusted-construction rule
            "workflow_id": wf6,  # same workflow ID
            "inactive_days": repaired.arguments.inactive_days,
            "environment": repaired.arguments.environment,
            # rollback_proof intentionally omitted (None) -- no proof fabricated
        },
    )
    repaired_summary = _summary("One-shot repair: repaired call", repaired_result.structuredContent)
    repaired_summary["repaired_tool"] = repaired.tool_name
    results.append(repaired_summary)

    return results


async def _run_portfolio_demo() -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    import os

    env = dict(os.environ)
    env["PROOFGATE_RUNTIME_MODE"] = "fallback"

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "proofgate.mcp_server"], cwd=str(REPO_ROOT), env=env
    )

    reset_working_db()
    reset_audit_log(audit_path=REAL_AUDIT_PATH)

    transcript: dict[str, Any] = {"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()}

    print("=== Delete / deactivate / one-shot repair ===")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            discovered = sorted(t.name for t in (await session.list_tools()).tools)
            print(f"Discovered MCP tools: {discovered}")
            transcript["discovered_tools"] = discovered

            core_results = await _run_delete_deactivate_and_repair(session)
            for r in core_results:
                print(f"  {r['label']}: {r['verdict']} rules={r['triggered_rules']} executed={r['executed']}")
            transcript["delete_deactivate_repair"] = core_results

    reset_working_db()
    reset_audit_log(audit_path=REAL_AUDIT_PATH)
    reset_workflow_state("__portfolio_cleanup__")

    print("\n=== Automatic rollback (real MCP gateway, own subprocess) ===")
    import scripts.rollback_demo as rollback_demo

    rollback_transcript = await rollback_demo._run_demo()
    final_status = rollback_transcript["restoration"]["final_postcondition_status"]
    budget = rollback_transcript["budget_and_audit"]["budget_after_rollback"]
    print(f"  Final postcondition: {final_status}")
    print(f"  Workflow budget: rows_mutated={budget['rows_mutated']}, max_rows={budget['max_rows']}")
    transcript["rollback"] = {
        "final_postcondition_status": final_status,
        "workflow_budget": budget,
        "mutation_result": rollback_transcript["delete_enforcement"]["actual_mutation"],
    }

    print("\n=== Feature flags (real MCP gateway, own subprocess) ===")
    import scripts.feature_flag_demo as feature_flag_demo

    ff_transcript = await feature_flag_demo._run_demo()
    for key in ("broad", "corrected", "disable", "exact_no_op"):
        section = ff_transcript[key]
        verdict = section.get("verdict", "n/a")
        mutation = section.get("mutation_result")
        print(f"  {key}: verdict={verdict} mutation={mutation}")
    transcript["feature_flags"] = {
        "broad": {"verdict": ff_transcript["broad"]["verdict"], "impact": ff_transcript["broad"]["impact"]},
        "corrected_enable": {
            "verdict": ff_transcript["corrected"]["verdict"],
            "mutation_result": ff_transcript["corrected"]["mutation_result"],
        },
        "corrected_disable": {
            "verdict": ff_transcript["disable"]["verdict"],
            "mutation_result": ff_transcript["disable"]["mutation_result"],
        },
        "exact_no_op": {
            "mutation_result": ff_transcript["exact_no_op"]["mutation_result"],
            "file_mutated": ff_transcript["exact_no_op"]["file_mutated"],
        },
    }

    # Final reset -- capture is already complete above.
    reset_working_db()
    reset_feature_flags_working()
    reset_audit_log(audit_path=REAL_AUDIT_PATH)
    print("\nSandbox reset after evidence capture (users database, feature-flag state, audit log, workflow budgets).")

    return transcript


def main() -> int:
    transcript = asyncio.run(_run_portfolio_demo())
    output_path = (
        REPO_ROOT
        / "artifacts"
        / f"portfolio_demo_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(transcript, indent=2))
    print(f"\nTranscript written to: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
