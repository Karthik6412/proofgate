"""Slice 1 / P4: general canonical JSON and digest primitives in
proofgate/selector.py.

canonical_json / sha256_digest are the one canonical encoding a second
domain reuses for its digests. The selector path now delegates to
canonical_json, so the pinned Operations selector bytes and hashes below
(captured before the change, and matching the golden-parity selector
hashes) must stay byte-identical.
"""

import ast
import datetime
import hashlib
from pathlib import Path

import pytest

import proofgate.selector as selector_module
from proofgate.selector import (
    canonical_json,
    canonical_selector_json,
    compute_selector_hash,
    sha256_digest,
)

PINNED_SELECTORS = [
    (
        {"inactive_days": 90, "environment": None},
        b'{"environment":null,"inactive_days":90}',
        "89f999b2a057f9c8e2438db60693d03fa6f55e489f7137d58d15ee86e4ab67df",
    ),
    (
        {"inactive_days": 90, "environment": "test"},
        b'{"environment":"test","inactive_days":90}',
        "77f57571c266d67fb40a157718edc8e07557677483ad23921e6f71f9f6e72f11",
    ),
    (
        {"flag_name": "new_checkout", "enabled": True, "environment": None, "rollout_percentage": 100},
        b'{"enabled":true,"environment":null,"flag_name":"new_checkout","rollout_percentage":100}',
        "2f0707170ae8a9bc63ab73e88a4559c4f9d1c04fb36f61de8cbc1d5a1ba7efdf",
    ),
    (
        {"flag_name": "new_checkout", "enabled": True, "environment": "production", "rollout_percentage": 1},
        b'{"enabled":true,"environment":"production","flag_name":"new_checkout","rollout_percentage":1}',
        "4cf14f6b8a241ed1a3c564132a8c5cea8cddfc5dd0f8b3482bb074cd856d99d2",
    ),
    (
        # Non-selector keys are still excluded from the selector hash.
        {"environment": "test", "inactive_days": 90, "rollback_proof": "x", "workflow_id": "wf"},
        b'{"environment":"test","inactive_days":90}',
        "77f57571c266d67fb40a157718edc8e07557677483ad23921e6f71f9f6e72f11",
    ),
]


@pytest.mark.parametrize("arguments,expected_bytes,expected_hash", PINNED_SELECTORS)
def test_operations_selector_encoding_is_byte_identical(arguments, expected_bytes, expected_hash):
    assert canonical_selector_json(arguments) == expected_bytes
    assert compute_selector_hash(arguments) == expected_hash


def test_canonical_json_encodes_the_whole_value_without_filtering():
    value = {"grant_id": "g-1", "environment": "test", "nested": {"b": [3, 1], "a": None}}

    assert canonical_json(value) == b'{"environment":"test","grant_id":"g-1","nested":{"a":null,"b":[3,1]}}'


def test_canonical_json_is_key_order_independent_but_list_order_sensitive():
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})
    assert canonical_json({"ids": ["d1", "d2"]}) != canonical_json({"ids": ["d2", "d1"]})


def test_canonical_json_is_ascii_safe():
    assert canonical_json({"name": "Zoë"}) == b'{"name":"Zo\\u00eb"}'


def test_sha256_digest_hashes_canonical_json():
    value = {"z": [1, 2], "a": "x"}

    assert sha256_digest(value) == hashlib.sha256(canonical_json(value)).hexdigest()


def test_non_selector_keys_change_the_general_digest_but_not_the_selector_hash():
    base = {"inactive_days": 90, "environment": "test"}
    extended = {**base, "workflow_id": "wf-1"}

    assert compute_selector_hash(base) == compute_selector_hash(extended)
    assert sha256_digest(base) != sha256_digest(extended)


def test_selector_hash_equals_general_digest_of_the_filtered_selector():
    arguments = {"inactive_days": 90, "environment": "test", "rollback_proof": "x"}

    assert compute_selector_hash(arguments) == sha256_digest({"inactive_days": 90, "environment": "test"})


@pytest.mark.parametrize(
    "value",
    [
        {"ids": {"d1", "d2"}},
        {"at": datetime.datetime(2026, 10, 9, tzinfo=datetime.timezone.utc)},
        {"obj": object()},
    ],
    ids=["set", "datetime", "object"],
)
def test_unnormalized_values_are_rejected_not_converted(value):
    with pytest.raises(TypeError):
        canonical_json(value)
    with pytest.raises(TypeError):
        sha256_digest(value)


def test_selector_module_has_exactly_one_json_encoding_call():
    tree = ast.parse(Path(selector_module.__file__).read_text())
    dumps_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "dumps"
    ]
    assert len(dumps_calls) == 1
