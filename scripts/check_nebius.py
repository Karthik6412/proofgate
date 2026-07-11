#!/usr/bin/env python
"""Manual Nebius Token Factory integration check.

This is NOT part of the automated unit-test suite -- unit tests must
never depend on live network access. Run this manually, after unit
tests, to confirm the real live integration actually works:

    .venv/bin/python scripts/check_nebius.py
    .venv/bin/python scripts/check_nebius.py --strict

--strict exits non-zero if either live extraction call fails and falls
back to the deterministic extractor. NEBIUS_API_KEY is never printed.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.nebius_client import (
    extract_intent_live_or_fallback,
    extract_risk_features_live_or_fallback,
    has_api_key,
    live_enabled,
    model,
)
from operations.actions import preview_delete_users
from operations.database import reset_working_db

DEMO_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manual Nebius Token Factory integration check."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if either live extraction call falls back.",
    )
    args = parser.parse_args()

    print(f"NEBIUS_LIVE_ENABLED: {live_enabled()}")
    print(f"NEBIUS_API_KEY configured: {has_api_key()}")
    print(f"Configured model: {model()}")
    print()

    reset_working_db()
    impact = preview_delete_users(inactive_days=90, environment=None)

    intent_outcome = extract_intent_live_or_fallback(DEMO_INSTRUCTION)
    intent_live_success = intent_outcome.mode == "nebius_live"
    print(f"Intent extraction live success: {intent_live_success}")
    print(f"Intent extraction mode: {intent_outcome.mode}")
    print(f"Validated intent: {intent_outcome.value.model_dump_json()}")
    print()

    risk_outcome = extract_risk_features_live_or_fallback(
        DEMO_INSTRUCTION,
        intent_outcome.value,
        {"inactive_days": 90, "environment": None},
        impact,
    )
    risk_live_success = risk_outcome.mode == "nebius_live"
    print(f"Risk extraction live success: {risk_live_success}")
    print(f"Risk extraction mode: {risk_outcome.mode}")
    print(f"Validated risk features: {risk_outcome.value.model_dump_json()}")
    print()

    used_fallback = not intent_live_success or not risk_live_success
    print(f"Fallback used: {used_fallback}")

    if args.strict and used_fallback:
        print("STRICT MODE: live integration did not fully succeed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
