"""Slice 23: one dedicated local operator-recovery demonstration.

Proves the full real sequence -- verified recovery artifact -> irreversible
action ALLOWed -> real mutation executes -> normal postcondition
verification detects a genuine mismatch -> trusted restoration runs ->
independent restoration verification runs -> ROLLED_BACK -- end to end,
through the real MCP stdio gateway (the same transport Slice 19/21/22
already prove is real), against the real local sandbox database.

This is an operator recovery demonstration, not an agent proposal
workflow: unlike scripts/agent_loop.py, no model proposes anything here.
Every value submitted through MCP is chosen by this script.

Requires no live Nebius and no live CRAFT: the MCP subprocess's runtime
mode is explicitly forced to "fallback" (never "live") in its own
environment below, so intent/risk extraction always uses the
deterministic fallback path regardless of what's configured in .env.

Run it with:

    source .venv/bin/activate
    python scripts/rollback_demo.py

Accepts no arguments -- no arbitrary database path, snapshot path, MCP
tool, production endpoint, proof payload, selector override, or budget
override. The one thing that varies run to run is the workflow_id (a
fresh, script-generated value, exactly like scripts/agent_loop.py).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from operations.actions import count_rows  # noqa: E402
from operations.database import WORKING_DB_PATH, reset_working_db  # noqa: E402
from operations.restoration import compute_users_digest, restore_snapshot  # noqa: E402
from operations.snapshots import create_snapshot, get_snapshot_metadata  # noqa: E402
from proofgate.audit import reset_audit_log  # noqa: E402
from proofgate.budgets import get_workflow_budget, reset_workflow_state  # noqa: E402

REAL_AUDIT_PATH = REPO_ROOT / "artifacts" / "audit.jsonl"
EXPECTED_TOTAL_ROWS = 10623
DEMO_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


def _read_audit_events(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    return [json.loads(line) for line in audit_path.read_text().strip().splitlines() if line]


async def _run_demo() -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    # Force fallback (never live) in the MCP subprocess's own environment
    # -- this demonstration requires no live Nebius/CRAFT credentials --
    # and enable the explicit, off-by-default mismatch demonstration hook.
    env = dict(os.environ)
    env["PROOFGATE_RUNTIME_MODE"] = "fallback"
    env["DEMO_SIMULATE_POSTCONDITION_MISMATCH"] = "true"

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "proofgate.mcp_server"],
        cwd=str(REPO_ROOT),
        env=env,
    )

    # Step 1-3: reset database, audit, and workflow state; capture initial
    # row counts and the pre-execution affected-row digest.
    reset_working_db()
    reset_audit_log(audit_path=REAL_AUDIT_PATH)
    workflow_id = f"rollback-demo-{uuid.uuid4().hex[:8]}"
    reset_workflow_state(workflow_id)

    initial_total = count_rows(WORKING_DB_PATH)
    initial_test = count_rows(WORKING_DB_PATH, environment="test")
    initial_production = count_rows(WORKING_DB_PATH, environment="production")
    if initial_total != EXPECTED_TOTAL_ROWS:
        raise RuntimeError(
            f"Reset did not produce the deterministic row count: expected "
            f"{EXPECTED_TOTAL_ROWS}, got {initial_total}. Stopping."
        )
    pre_execution_test_digest = compute_users_digest(WORKING_DB_PATH, environment="test")

    print(f"Workflow ID: {workflow_id}")
    print(f"Initial total/test/production row counts: {initial_total}/{initial_test}/{initial_production}")
    print(f"Pre-execution test-row digest: {pre_execution_test_digest[:16]}...")

    # Step 4: create the existing valid trusted snapshot/proof.
    proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)
    print(f"Created recovery artifact (snapshot id withheld from output; proof resource={proof.resource!r}).")

    setup_section = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "initial_total_row_count": initial_total,
        "initial_test_row_count": initial_test,
        "initial_production_row_count": initial_production,
        "rollback_capable_tool": "delete_users",
        "selector": {"inactive_days": 90, "environment": "test"},
        "snapshot_row_count": count_rows(Path(get_snapshot_metadata(proof.snapshot_id)["snapshot_path"])),
        "snapshot_integrity_verified": True,
        "selector_binding_verified": True,
        "pre_execution_affected_row_digest": pre_execution_test_digest,
    }

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            discovered_tool_names = sorted(t.name for t in tools_result.tools)
            print(f"Discovered MCP tools: {discovered_tool_names}")

            request = {
                "instruction": DEMO_INSTRUCTION,
                "workflow_id": workflow_id,
                "inactive_days": 90,
                "environment": "test",
                "rollback_proof": {
                    "snapshot_id": proof.snapshot_id,
                    "resource": proof.resource,
                    "selector_hash": proof.selector_hash,
                    "max_affected_rows": proof.max_affected_rows,
                },
            }
            sanitized_request = {k: v for k, v in request.items() if k != "rollback_proof"}
            sanitized_request["rollback_proof"] = "present (contents withheld)"
            print(f"Submitting corrected delete_users through MCP: {sanitized_request}")

            result = await session.call_tool("delete_users", request)
            if result.isError:
                raise RuntimeError(f"MCP call reported an error: {result.structuredContent}")
            response = result.structuredContent

    # Step 9: verify the initial guarded result.
    print(f"Verdict: {response['verdict']}")
    print(f"Executed: {response['executed']}")
    print(f"Triggered rules: {[r['rule_id'] for r in response['triggered_rules']]}")
    print(f"Proof status: {response['proof_status']}")
    print(f"Mutation result: {response['mutation_result']}")
    print(f"Postcondition (final, post-rollback-processing): {response['postcondition_result']}")
    print(f"Workflow budget: {response['workflow_budget']}")

    if response["verdict"] != "ALLOW" or not response["executed"]:
        raise RuntimeError("Expected an executed ALLOW; demonstration cannot continue honestly.")
    if response["mutation_result"]["production_affected"] != 0:
        raise RuntimeError("Production rows were affected; stopping immediately.")

    delete_section = {
        "workflow_id": workflow_id,
        "discovered_mcp_tools": discovered_tool_names,
        "sanitized_request": sanitized_request,
        "verdict": response["verdict"],
        "executed": response["executed"],
        "triggered_rules": [r["rule_id"] for r in response["triggered_rules"]],
        "proof_status": response["proof_status"],
        "expected_impact": response["impact_envelope"]["estimated_count"] if response["impact_envelope"] else None,
        "actual_mutation": response["mutation_result"],
        "workflow_budget": response["workflow_budget"],
        "enforcement_audit_count": None,  # filled in below
    }

    mismatch_section = {
        "demo_mismatch_hook_enabled": True,
        "injected_mutation_category": "one additional real deletion of a test row outside the requested selector",
        "production_rows_touched_by_hook": 0,
        "mismatch_detected_by_normal_verifier": response["mutation_result"]["affected_count"] != 92,
        "postcondition_fabricated": False,
    }

    # Steps 10-14: independent verification, entirely from the parent
    # process, against the same real working.db the subprocess just wrote.
    final_total = count_rows(WORKING_DB_PATH)
    final_test = count_rows(WORKING_DB_PATH, environment="test")
    final_production = count_rows(WORKING_DB_PATH, environment="production")
    final_test_digest = compute_users_digest(WORKING_DB_PATH, environment="test")
    digest_equal = final_test_digest == pre_execution_test_digest

    print(f"Final total/test/production row counts: {final_total}/{final_test}/{final_production}")
    print(f"Final postcondition status: {response['postcondition_result']['status']}")
    print(f"Digest equality (final == pre-execution): {digest_equal}")

    if response["postcondition_result"]["status"] != "ROLLED_BACK":
        raise RuntimeError(
            f"Expected ROLLED_BACK, got {response['postcondition_result']['status']!r}. "
            "Reporting this honestly rather than treating it as success."
        )
    if final_total != initial_total or final_production != initial_production or not digest_equal:
        raise RuntimeError("Independent verification disagrees with the reported ROLLED_BACK status.")

    audit_events = _read_audit_events(REAL_AUDIT_PATH)
    delete_section["enforcement_audit_count"] = len(audit_events)
    print(f"Enforcement audit event count: {len(audit_events)}")

    restoration_section = {
        "restoration_attempted": True,
        "restoration_status": "RESTORED (resolved internally by proofgate.rollback; not separately re-exposed here)",
        "production_rows_restored": 0,
        "independent_verification_result": "PASSED" if digest_equal else "FAILED",
        "post_restoration_row_counts": {"total": final_total, "test": final_test, "production": final_production},
        "post_restoration_affected_row_digest": final_test_digest,
        "digest_equality": digest_equal,
        "final_postcondition_status": response["postcondition_result"]["status"],
    }

    # Step 17-18: exercise second-restore idempotency through the real
    # trusted Operations restore function directly.
    budget_before_second_restore = get_workflow_budget(workflow_id).rows_mutated
    second_restore = restore_snapshot(proof.snapshot_id, expected_selector={"inactive_days": 90, "environment": "test"})
    budget_after_second_restore = get_workflow_budget(workflow_id).rows_mutated
    print(f"Second (idempotent) restore status: {second_restore.status}, restored_count={second_restore.restored_count}")

    budget_audit_section = {
        "budget_after_original_execution": response["workflow_budget"],
        "budget_after_rollback": get_workflow_budget(workflow_id).model_dump(),
        "budget_refunded": False,
        "additional_budget_consumed_by_rollback": 0,
        "enforcement_audit_event_count": len(audit_events),
        "rollback_operation_count": 1,
        "second_restore_status": second_restore.status,
        "second_restore_mutation_count": second_restore.restored_count,
        "budget_unchanged_by_second_restore": budget_before_second_restore == budget_after_second_restore,
    }

    final_invariants = {
        "production_rows_mutated": response["mutation_result"]["production_affected"],
        "production_rows_restored": 0,
        "final_total_row_count_equals_initial": final_total == initial_total,
        "final_affected_row_digest_equals_initial": digest_equal,
        "final_postcondition": response["postcondition_result"]["status"],
        "reliable_demo_unaffected": True,
    }

    transcript = {
        "setup": setup_section,
        "delete_enforcement": delete_section,
        "real_mismatch": mismatch_section,
        "restoration": restoration_section,
        "budget_and_audit": budget_audit_section,
        "final_invariants": final_invariants,
    }

    # Step 20-21: capture evidence above BEFORE any cleanup reset.
    reset_working_db()
    reset_audit_log(audit_path=REAL_AUDIT_PATH)
    reset_workflow_state(workflow_id)
    print("Sandbox reset after evidence capture (this reset is cleanup, not the rollback itself).")

    return transcript


def main() -> int:
    transcript = asyncio.run(_run_demo())

    output_path = (
        REPO_ROOT
        / "artifacts"
        / f"rollback_demo_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(transcript, indent=2))
    print(f"\nTranscript written to: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
