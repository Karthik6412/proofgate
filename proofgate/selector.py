"""Shared canonical JSON encoding and hashing.

canonical_json is the single canonical encoding in ProofGate, and
sha256_digest hashes it. canonical_selector_json is the selector-specific
use required by AGENTS.md's Selector Hash Contract: it filters to the
mutation-selecting fields and then uses canonical_json, so every selector
hash is byte-identical to the encoding it has always had. Every module
that needs a selector hash (impact preview, snapshot creation, proof
validation, guarded execution) or any other canonical digest must import
from here rather than reimplementing canonical JSON encoding.
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


def canonical_json(value: object) -> bytes:
    """Canonical JSON encoding of value as UTF-8 bytes.

    Encodes the whole value -- nothing is filtered. Values JSON cannot
    represent (sets, datetimes, arbitrary objects) raise TypeError rather
    than being converted, so callers must normalize them explicitly
    (e.g. sorted lists, ISO-8601 strings) before hashing.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sha256_digest(value: object) -> str:
    """SHA-256 hex digest of canonical_json(value)."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def canonical_selector_json(arguments: dict) -> bytes:
    """Canonical JSON encoding of selector arguments as UTF-8 bytes."""
    selector = {key: arguments[key] for key in SELECTOR_FIELDS if key in arguments}
    return canonical_json(selector)


def compute_selector_hash(arguments: dict) -> str:
    """SHA-256 hex digest of the canonical selector JSON."""
    return hashlib.sha256(canonical_selector_json(arguments)).hexdigest()
