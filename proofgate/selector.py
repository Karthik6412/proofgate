"""Shared selector canonicalization and hashing.

This is the single canonicalization function required by AGENTS.md's
Selector Hash Contract. Every module that needs a selector hash (impact
preview, snapshot creation, proof validation, guarded execution) must
import from here rather than reimplementing canonical JSON encoding.
"""

import hashlib
import json

# The complete set of mutation-selecting argument names recognized across
# every registered tool -- never rollback_proof, action_context, workflow
# metadata, policy metadata, risk metadata, timestamps, or audit fields.
# This is a plain data allowlist, not per-tool branching logic: adding a
# new registered tool's own selector field names here (Slice 25 added
# flag_name/enabled/rollout_percentage for set_feature_flag) never changes
# the canonicalization algorithm, its ordering, or its serialization --
# every tool's selector is still sorted, separated, and hashed identically.
SELECTOR_FIELDS = ("inactive_days", "environment", "flag_name", "enabled", "rollout_percentage")


def canonical_selector_json(arguments: dict) -> bytes:
    """Canonical JSON encoding of selector arguments as UTF-8 bytes."""
    selector = {key: arguments[key] for key in SELECTOR_FIELDS if key in arguments}
    return json.dumps(
        selector,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def compute_selector_hash(arguments: dict) -> str:
    """SHA-256 hex digest of the canonical selector JSON."""
    return hashlib.sha256(canonical_selector_json(arguments)).hexdigest()
