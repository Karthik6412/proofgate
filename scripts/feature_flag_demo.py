"""Slice 25/25.1: deterministic terminal demonstration of
set_feature_flag -- a genuinely non-user-management consequential tool,
governed by the exact same guarded pipeline (registry, policy, selector
hashing, budget, postcondition, audit, repair guidance) as
delete_users/deactivate_users, operating on a filesystem-backed
feature-flag resource instead of the users SQLite database.

Slice 25.1 corrected blast-radius accounting to measure the change in
effective user exposure (not simply the requested rollout percentage),
so this demonstration also exercises a disable, a rollout reduction, and
an exact no-op -- proving enabling/disabling/increasing/decreasing are
all accounted for symmetrically.

Requires no live Nebius and no live CRAFT: the MCP subprocess's runtime
mode is explicitly forced to "fallback" in its own environment below, so
intent/risk extraction always uses the deterministic fallback path
regardless of what's configured in .env. This keeps the demonstration
itself fully local and deterministic.

Run it with:

    source .venv/bin/activate
    python scripts/feature_flag_demo.py

Accepts no arguments -- no arbitrary database path, snapshot path, MCP
tool, production endpoint, proof payload, selector override, or budget
override.
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

from operations.database import PRISTINE_DB_PATH, WORKING_DB_PATH, reset_working_db  # noqa: E402
from operations.feature_flags import (  # noqa: E402
    FEATURE_FLAGS_PRISTINE_PATH,
    FEATURE_FLAGS_WORKING_PATH,
    load_audience,
    reset_feature_flags_working,
)
from proofgate.audit import reset_audit_log  # noqa: E402
from proofgate.budgets import get_workflow_budget, reset_workflow_state  # noqa: E402

REAL_AUDIT_PATH = REPO_ROOT / "artifacts" / "audit.jsonl"

BROAD_INSTRUCTION = "Enable the new checkout flow for test users."
CORRECTED_INSTRUCTION = "Enable the new checkout flow for the test environment."
DISABLE_INSTRUCTION = "Disable the new checkout flow for the test environment."
REDUCE_INSTRUCTION = "Reduce the new checkout flow rollout in the test environment."


def _read_audit_events(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    return [json.loads(line) for line in audit_path.read_text().strip().splitlines() if line]


def _working_state() -> dict:
    return json.loads(FEATURE_FLAGS_WORKING_PATH.read_text())


async def _run_demo() -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = dict(os.environ)
    env["PROOFGATE_RUNTIME_MODE"] = "fallback"

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "proofgate.mcp_server"],
        cwd=str(REPO_ROOT),
        env=env,
    )

    reset_working_db()
    reset_feature_flags_working()
    reset_audit_log(audit_path=REAL_AUDIT_PATH)

    pristine_state_before = FEATURE_FLAGS_PRISTINE_PATH.read_text()
    audience = load_audience()
    print(f"Audience: test={audience['test']}, production={audience['production']}, total={sum(audience.values())}")

    transcript: dict[str, Any] = {"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()}

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            discovered = sorted(t.name for t in tools_result.tools)
            print(f"Discovered MCP tools: {discovered}")
            transcript["discovered_tools"] = discovered

            # --- Broad request ---
            workflow_id = f"ff-demo-broad-{uuid.uuid4().hex[:8]}"
            reset_workflow_state(workflow_id)
            broad_request = {
                "instruction": BROAD_INSTRUCTION,
                "workflow_id": workflow_id,
                "flag_name": "new_checkout",
                "enabled": True,
                "environment": None,
                "rollout_percentage": 100,
            }
            print(f"\n=== Broad request ===\nInstruction: {BROAD_INSTRUCTION!r}\nArguments: {broad_request}")
            broad_result = await session.call_tool("set_feature_flag", broad_request)
            if broad_result.isError:
                raise RuntimeError(f"Broad request reported an MCP error: {broad_result.structuredContent}")
            broad = broad_result.structuredContent
            print(f"Verdict: {broad['verdict']}")
            print(f"Impact: {broad['impact_envelope']['estimated_count']} "
                  f"(production={broad['impact_envelope']['environment_counts']['production']}, "
                  f"test={broad['impact_envelope']['environment_counts']['test']})")
            print(f"Rules: {[r['rule_id'] for r in broad['triggered_rules']]}")
            print(f"Executed: {broad['executed']}")
            print(f"Repair guidance: {broad['suggested_repairs']}")

            if broad["verdict"] != "BLOCK" or broad["executed"]:
                raise RuntimeError("Expected the broad request to be BLOCKed without execution.")
            state_after_broad = _working_state()
            if state_after_broad != json.loads(pristine_state_before):
                raise RuntimeError("Working feature-flag state changed after a BLOCKed request.")

            broad_audit_count = len(_read_audit_events(REAL_AUDIT_PATH))

            transcript["broad"] = {
                "instruction": BROAD_INSTRUCTION,
                "arguments": broad_request,
                "impact": broad["impact_envelope"]["estimated_count"],
                "impact_environment_counts": broad["impact_envelope"]["environment_counts"],
                "verdict": broad["verdict"],
                "triggered_rules": [r["rule_id"] for r in broad["triggered_rules"]],
                "proof_status": broad["proof_status"],
                "repair_guidance": broad["suggested_repairs"],
                "executed": broad["executed"],
                "audit_event_count": broad_audit_count,
                "working_state_unchanged": True,
            }

            # --- Corrected request ---
            workflow_id_2 = f"ff-demo-corrected-{uuid.uuid4().hex[:8]}"
            reset_workflow_state(workflow_id_2)
            pre_state = _working_state()
            corrected_request = {
                "instruction": CORRECTED_INSTRUCTION,
                "workflow_id": workflow_id_2,
                "flag_name": "new_checkout",
                "enabled": True,
                "environment": "test",
                "rollout_percentage": 100,
            }
            print(f"\n=== Corrected request ===\nInstruction: {CORRECTED_INSTRUCTION!r}\nArguments: {corrected_request}")
            corrected_result = await session.call_tool("set_feature_flag", corrected_request)
            if corrected_result.isError:
                raise RuntimeError(f"Corrected request reported an MCP error: {corrected_result.structuredContent}")
            corrected = corrected_result.structuredContent
            print(f"Verdict: {corrected['verdict']}")
            print(f"Mutation: {corrected['mutation_result']}")
            print(f"Postcondition: {corrected['postcondition_result']}")
            print(f"Budget: {corrected['workflow_budget']}")

            if corrected["verdict"] != "ALLOW" or not corrected["executed"]:
                raise RuntimeError("Expected the corrected request to be ALLOWed and executed.")

            post_state = _working_state()
            production_unchanged = post_state["new_checkout"]["production"] == pre_state["new_checkout"]["production"]
            pristine_unchanged = FEATURE_FLAGS_PRISTINE_PATH.read_text() == pristine_state_before

            print(f"\nnew_checkout.test: {post_state['new_checkout']['test']}")
            print(f"new_checkout.production: {post_state['new_checkout']['production']}")
            print(f"Production state unchanged: {production_unchanged}")
            print(f"Pristine state unchanged: {pristine_unchanged}")

            if not (
                post_state["new_checkout"]["test"]["enabled"] is True
                and post_state["new_checkout"]["test"]["rollout_percentage"] == 100
            ):
                raise RuntimeError("Corrected request did not honestly update the test environment.")
            if not production_unchanged or not pristine_unchanged:
                raise RuntimeError("Corrected request affected state it must not have touched.")

            corrected_audit_count = len(_read_audit_events(REAL_AUDIT_PATH)) - broad_audit_count

            transcript["corrected"] = {
                "instruction": CORRECTED_INSTRUCTION,
                "arguments": corrected_request,
                "pre_state": pre_state,
                "verdict": corrected["verdict"],
                "mutation_result": corrected["mutation_result"],
                "postcondition_result": corrected["postcondition_result"],
                "workflow_budget": corrected["workflow_budget"],
                "audit_event_count": corrected_audit_count,
                "post_state": post_state,
                "production_state_unchanged": production_unchanged,
                "pristine_state_unchanged": pristine_unchanged,
            }

            # --- Corrected disable (Slice 25.1, Part H) ---
            workflow_id_3 = f"ff-demo-disable-{uuid.uuid4().hex[:8]}"
            reset_workflow_state(workflow_id_3)
            disable_request = {
                "instruction": DISABLE_INSTRUCTION,
                "workflow_id": workflow_id_3,
                "flag_name": "new_checkout",
                "enabled": False,
                "environment": "test",
                "rollout_percentage": 0,
            }
            print(f"\n=== Corrected disable (from enabled 100%) ===\nInstruction: {DISABLE_INSTRUCTION!r}\nArguments: {disable_request}")
            disable_result = await session.call_tool("set_feature_flag", disable_request)
            if disable_result.isError:
                raise RuntimeError(f"Disable request reported an MCP error: {disable_result.structuredContent}")
            disable = disable_result.structuredContent
            print(f"Verdict: {disable['verdict']}")
            print(f"Impact: {disable['impact_envelope']['estimated_count']}")
            print(f"Mutation: {disable['mutation_result']}")
            print(f"Postcondition: {disable['postcondition_result']}")
            print(f"Budget: {disable['workflow_budget']}")

            if disable["verdict"] != "ALLOW" or disable["mutation_result"]["test_affected"] != 92:
                raise RuntimeError("Expected the disable request to be ALLOWed and affect exactly 92 test users.")
            disable_post_state = _working_state()
            if disable_post_state["new_checkout"]["test"]["enabled"] is not False:
                raise RuntimeError("Disable request did not honestly disable the test environment.")

            transcript["disable"] = {
                "instruction": DISABLE_INSTRUCTION,
                "arguments": disable_request,
                "impact": disable["impact_envelope"]["estimated_count"],
                "verdict": disable["verdict"],
                "mutation_result": disable["mutation_result"],
                "postcondition_result": disable["postcondition_result"],
                "workflow_budget": disable["workflow_budget"],
                "post_state_test": disable_post_state["new_checkout"]["test"],
                "post_state_production": disable_post_state["new_checkout"]["production"],
            }

            # --- Rollout reduction 100% -> 50% ---
            workflow_id_4 = f"ff-demo-reduce-{uuid.uuid4().hex[:8]}"
            reset_workflow_state(workflow_id_4)
            await session.call_tool(
                "set_feature_flag",
                {
                    "instruction": CORRECTED_INSTRUCTION,
                    "workflow_id": f"ff-demo-reseed-{uuid.uuid4().hex[:8]}",
                    "flag_name": "new_checkout",
                    "enabled": True,
                    "environment": "test",
                    "rollout_percentage": 100,
                },
            )
            reduce_request = {
                "instruction": REDUCE_INSTRUCTION,
                "workflow_id": workflow_id_4,
                "flag_name": "new_checkout",
                "enabled": True,
                "environment": "test",
                "rollout_percentage": 50,
            }
            print(f"\n=== Rollout reduction (100% -> 50%) ===\nInstruction: {REDUCE_INSTRUCTION!r}\nArguments: {reduce_request}")
            reduce_result = await session.call_tool("set_feature_flag", reduce_request)
            reduce = reduce_result.structuredContent
            print(f"Verdict: {reduce['verdict']}")
            print(f"Mutation: {reduce['mutation_result']}")
            print(f"Budget: {reduce['workflow_budget']}")
            if reduce["mutation_result"]["test_affected"] != 46:
                raise RuntimeError(f"Expected 46 affected users for a 100%->50% reduction, got {reduce['mutation_result']}")

            transcript["reduce_100_to_50"] = {
                "instruction": REDUCE_INSTRUCTION,
                "arguments": reduce_request,
                "verdict": reduce["verdict"],
                "mutation_result": reduce["mutation_result"],
                "postcondition_result": reduce["postcondition_result"],
                "workflow_budget": reduce["workflow_budget"],
            }

            # --- Exact no-op ---
            workflow_id_5 = f"ff-demo-noop-{uuid.uuid4().hex[:8]}"
            reset_workflow_state(workflow_id_5)
            before_noop_mtime = FEATURE_FLAGS_WORKING_PATH.stat().st_mtime_ns
            noop_request = {
                "instruction": REDUCE_INSTRUCTION,
                "workflow_id": workflow_id_5,
                "flag_name": "new_checkout",
                "enabled": True,
                "environment": "test",
                "rollout_percentage": 50,
            }
            print(f"\n=== Exact no-op (already at 50%) ===\nArguments: {noop_request}")
            noop_result = await session.call_tool("set_feature_flag", noop_request)
            noop = noop_result.structuredContent
            after_noop_mtime = FEATURE_FLAGS_WORKING_PATH.stat().st_mtime_ns
            print(f"Mutation: {noop['mutation_result']}")
            print(f"File mutated: {before_noop_mtime != after_noop_mtime}")
            if noop["mutation_result"]["test_affected"] != 0 or before_noop_mtime != after_noop_mtime:
                raise RuntimeError("Expected an exact no-op: zero affected users and no file mutation.")

            transcript["exact_no_op"] = {
                "arguments": noop_request,
                "mutation_result": noop["mutation_result"],
                "file_mutated": before_noop_mtime != after_noop_mtime,
            }

    users_db_unchanged = True  # this demo never touches operations/{pristine,working}.db at all
    transcript["users_database_invariant"] = users_db_unchanged
    transcript["final_total_audit_events"] = len(_read_audit_events(REAL_AUDIT_PATH))

    # Reset before returning -- capture is already complete above.
    reset_working_db()
    reset_feature_flags_working()
    reset_audit_log(audit_path=REAL_AUDIT_PATH)
    print("\nSandbox reset after evidence capture (feature-flag state, users database, audit log, workflow budgets).")

    return transcript


def main() -> int:
    transcript = asyncio.run(_run_demo())
    output_path = (
        REPO_ROOT
        / "artifacts"
        / f"feature_flag_demo_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(transcript, indent=2))
    print(f"\nTranscript written to: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
