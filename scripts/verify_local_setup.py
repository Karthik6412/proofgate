"""Slice 26: verify the current checkout is set up correctly for local,
deterministic, fallback-mode development.

This checks the *current* checkout, not a genuinely isolated fresh
operating-system environment -- it does not clone into a temp directory,
does not create a new virtualenv, and does not test OS/package-index
availability. It confirms: the expected Python version, expected
fixture/config files exist, the app imports, the real MCP stdio gateway
starts and discovers the registered tools, one deterministic BLOCK and
one deterministic ALLOW call succeed through the real guarded pipeline,
and (by default) a small, fast smoke-test subset passes.

Never installs dependencies, never requires credentials, never calls a
live provider, never modifies pristine fixtures, and resets all generated
state before exiting.

Run it with:

    source .venv/bin/activate
    python scripts/verify_local_setup.py            # fast checks only
    python scripts/verify_local_setup.py --full      # also runs the full pytest suite
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

MIN_PYTHON = (3, 11)

REQUIRED_FILES = [
    "pyproject.toml",
    "operations/pristine.db",
    "operations/feature_flags_pristine.json",
    "operations/feature_flag_audience.json",
    "proofgate/mcp_server.py",
    "app.py",
]

_checks_passed: list[str] = []
_checks_failed: list[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    (_checks_passed if condition else _checks_failed).append(label)


def _check_python_version() -> None:
    actual = sys.version_info[:2]
    _check(
        f"Python version >= {'.'.join(map(str, MIN_PYTHON))} (found {'.'.join(map(str, actual))})",
        actual >= MIN_PYTHON,
    )


def _check_required_files() -> None:
    for rel_path in REQUIRED_FILES:
        path = REPO_ROOT / rel_path
        _check(f"Required file exists: {rel_path}", path.exists())


def _check_fallback_requires_no_env() -> None:
    """Fallback mode must not require a .env file at all -- confirmed by
    resolving runtime mode with PROOFGATE_RUNTIME_MODE explicitly set and
    NEBIUS_API_KEY absent, exactly as tests/conftest.py already does for
    the whole offline test suite."""
    env = dict(os.environ)
    env.pop("NEBIUS_API_KEY", None)
    env["PROOFGATE_RUNTIME_MODE"] = "fallback"
    result = subprocess.run(
        [sys.executable, "-c", "from proofgate.runtime_mode import resolve_mode; print(resolve_mode().value)"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    _check(
        "Fallback mode resolves without NEBIUS_API_KEY or .env",
        result.returncode == 0 and result.stdout.strip() == "fallback",
        result.stderr.strip()[:200],
    )


def _check_app_import() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _check("app.py imports successfully", result.returncode == 0, result.stderr.strip()[-300:])


async def _mcp_discovery_and_governed_calls() -> tuple[bool, bool, bool, str]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from operations.database import reset_working_db
    from operations.snapshots import create_snapshot
    from proofgate.audit import reset_audit_log
    from proofgate.budgets import reset_workflow_state

    env = dict(os.environ)
    env["PROOFGATE_RUNTIME_MODE"] = "fallback"
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "proofgate.mcp_server"], cwd=str(REPO_ROOT), env=env
    )

    reset_working_db()
    audit_path = REPO_ROOT / "artifacts" / "audit.jsonl"
    reset_audit_log(audit_path=audit_path)

    discovery_ok = False
    block_ok = False
    allow_ok = False
    detail = ""

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = sorted(t.name for t in (await session.list_tools()).tools)
                discovery_ok = tools == ["deactivate_users", "delete_users", "set_feature_flag"]
                if not discovery_ok:
                    detail = f"discovered {tools}"

                reset_workflow_state("verify-local-setup-block")
                block_result = await session.call_tool(
                    "delete_users",
                    {
                        "instruction": "Clean up inactive test accounts that have not logged in for 90 days.",
                        "workflow_id": "verify-local-setup-block",
                        "inactive_days": 90,
                        "environment": None,
                    },
                )
                block_payload = block_result.structuredContent
                block_ok = (not block_result.isError) and block_payload["verdict"] == "BLOCK"

                reset_workflow_state("verify-local-setup-allow")
                proof = create_snapshot(resource="users", inactive_days=90, environment="test", max_affected_rows=92)
                allow_result = await session.call_tool(
                    "delete_users",
                    {
                        "instruction": "Clean up inactive test accounts that have not logged in for 90 days.",
                        "workflow_id": "verify-local-setup-allow",
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
                allow_payload = allow_result.structuredContent
                allow_ok = (not allow_result.isError) and allow_payload["verdict"] == "ALLOW"
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)[:200]
    finally:
        reset_working_db()
        reset_audit_log(audit_path=audit_path)
        reset_workflow_state("verify-local-setup-block")
        reset_workflow_state("verify-local-setup-allow")

    return discovery_ok, block_ok, allow_ok, detail


def _check_mcp_and_governed_calls() -> None:
    discovery_ok, block_ok, allow_ok, detail = asyncio.run(_mcp_discovery_and_governed_calls())
    _check("MCP discovery returns exactly the three registered tools", discovery_ok, detail)
    _check("Deterministic governed BLOCK call succeeds", block_ok, detail)
    _check("Deterministic governed ALLOW call succeeds", allow_ok, detail)


def _check_bounded_smoke_tests() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_policy_block.py", "tests/test_mcp_server.py", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    _check("Bounded smoke-test subset passes (policy + MCP)", result.returncode == 0, result.stdout[-500:])


def _run_full_suite() -> None:
    print("\nRunning full pytest suite (--full)...")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=REPO_ROOT, timeout=600)
    _check("Full pytest suite passes", result.returncode == 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the current checkout for local deterministic development.")
    parser.add_argument("--full", action="store_true", help="Also run the entire pytest suite (slower).")
    args = parser.parse_args()

    print("Verifying local setup (current checkout, fallback mode, no credentials, no network calls)...\n")

    _check_python_version()
    _check_required_files()
    _check_fallback_requires_no_env()
    _check_app_import()
    _check_mcp_and_governed_calls()
    _check_bounded_smoke_tests()

    if args.full:
        _run_full_suite()

    print(f"\n{len(_checks_passed)} passed, {len(_checks_failed)} failed.")
    if _checks_failed:
        print("Failed checks:")
        for label in _checks_failed:
            print(f"  - {label}")
        return 1

    print("Local setup verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
