#!/usr/bin/env python
"""Manual CRAFT MCP integration check.

This is NOT part of the automated unit-test suite -- unit tests must
never depend on live network access or interactive OAuth consent. Run
this manually, after unit tests, to confirm the real live integration
actually works:

    .venv/bin/python scripts/check_craft.py
    .venv/bin/python scripts/check_craft.py --strict --save-cache

--strict exits non-zero unless the live CRAFT workflow succeeds
end-to-end (cached fallback does not count as strict success). OAuth
tokens, authorization headers, client secrets, and complete environment
variable values are never printed.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from craft import config
from craft.evidence import DEMO_COHORT_QUESTION, prepare_craft_evidence

DEMO_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


def _print_raw_summary(label: str, summary: dict | None) -> None:
    if summary is None:
        return
    print(f"{label} (sanitized, bounded):")
    for key, value in summary.items():
        print(f"  {key}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual CRAFT MCP integration check.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero unless the live CRAFT workflow succeeds (cache doesn't count).",
    )
    parser.add_argument(
        "--save-cache",
        action="store_true",
        help="Explicitly confirm intent to persist sanitized evidence to the cache file.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Print discovered CRAFT tool names, the list_databases/get_schema/"
            "generate_sql input schemas, sanitized arguments and response-shape "
            "summaries for each discovery step, and the sanitized nested "
            "exception (never raw tokens/headers)."
        ),
    )
    args = parser.parse_args()

    print(f"CRAFT_LIVE_ENABLED: {config.live_enabled()}")
    print(f"CRAFT_PROJECT_ID configured: {config.has_project_id()}")
    print(f"CRAFT_MCP_URL: {config.mcp_url()}")
    print(f"CRAFT_OAUTH_CLIENT_ID: {config.oauth_client_id()}")
    print(f"CRAFT_DATABASE: {config.database()}")
    print(f"CRAFT_CACHE_PATH: {config.cache_path()}")
    print(f"CRAFT_OAUTH_TIMEOUT_SECONDS: {config.oauth_timeout_seconds()}")
    print()

    diagnostics: dict = {}
    outcome = prepare_craft_evidence(
        "check-craft-script", DEMO_INSTRUCTION, question=DEMO_COHORT_QUESTION, diagnostics=diagnostics
    )

    print(f"Authentication succeeded: {diagnostics.get('authenticated', False)}")
    print(f"Discovery tool used: {diagnostics.get('discovery_tool')}")
    print(f"Schema discovery succeeded: {diagnostics.get('schema_discovery_succeeded', False)}")
    print(f"generate_sql succeeded: {diagnostics.get('generate_sql_succeeded', False)}")
    print(f"execute_query succeeded: {diagnostics.get('execute_query_succeeded', False)}")
    print(f"Selected database/connection: {diagnostics.get('selected_database')}")
    print()

    print(f"Evidence mode: {outcome.mode}")
    if outcome.evidence is not None:
        print(f"Evidence label: {outcome.evidence.label}")
        print(f"Tool trace: {outcome.evidence.tool_trace}")
        print(f"Generated SQL: {outcome.evidence.generated_sql}")
        print(f"Result summary: {outcome.evidence.result_summary}")
        print(f"Result preview (bounded): {outcome.evidence.result_preview}")
    if outcome.error_summary:
        print(f"Error summary (sanitized): {outcome.error_summary}")

    cache_used = outcome.mode == "cached"
    saved_cache = outcome.mode == "live"
    print(f"Cache fallback used: {cache_used}")
    print(f"Sanitized evidence saved to cache: {saved_cache}")
    if args.save_cache and not saved_cache:
        print("--save-cache requested but no live evidence was produced to save.")

    if args.debug:
        print()
        print("--- DEBUG ---")
        print(f"Discovered CRAFT tool names: {diagnostics.get('discovered_tool_names')}")

        print()
        print(f"list_databases input schema: {diagnostics.get('list_databases_input_schema')}")
        print(f"list_databases arguments sent: {diagnostics.get('list_databases_arguments_sent')}")
        _print_raw_summary("list_databases raw result", diagnostics.get("list_databases_response_summary"))
        print(f"Selected database name: {diagnostics.get('selected_database_name')}")
        print(f"Selected database FQN: {diagnostics.get('selected_database_fqn')}")

        print()
        print(f"get_schema input schema: {diagnostics.get('get_schema_input_schema')}")
        print(f"get_schema arguments sent: {diagnostics.get('get_schema_arguments_sent')}")
        _print_raw_summary("get_schema raw result", diagnostics.get("get_schema_response_summary"))
        print(f"Selected schema name: {diagnostics.get('discovered_schema_name')}")
        print(f"Selected schema FQN: {diagnostics.get('discovered_schema_fqn')}")

        print()
        print(f"generate_sql input schema: {diagnostics.get('generate_sql_input_schema')}")
        print(f"generate_sql arguments sent: {diagnostics.get('generate_sql_arguments_sent')}")
        _print_raw_summary("generate_sql raw result", diagnostics.get("generate_sql_raw_result_summary"))

        print()
        print(f"Sanitized nested exception: {outcome.error_summary}")

    if args.strict and outcome.mode != "live":
        print("STRICT MODE: live CRAFT integration did not succeed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
