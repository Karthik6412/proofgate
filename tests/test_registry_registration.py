"""Slice 1 / P1: explicit registration seam in proofgate.registry.

Every test that registers anything runs against a private copy of
_REGISTRY installed via monkeypatch. get_tool_spec, registered_tool_names
and register_tool all read the module-global _REGISTRY at call time, so
they see the copy; monkeypatch restores the original dict object at
teardown even when the test fails, and the original is never mutated
(asserted below), so no registration can leak into other tests.
"""

import dataclasses

import pytest

import proofgate.registry as registry
from proofgate.registry import GuardedToolSpec, get_tool_spec, register_tool, registered_tool_names

EXPECTED_TOOLS = ("delete_users", "deactivate_users", "set_feature_flag")


@pytest.fixture
def isolated_registry(monkeypatch):
    original = registry._REGISTRY
    original_snapshot = dict(original)
    monkeypatch.setattr(registry, "_REGISTRY", dict(original))
    yield
    assert original == original_snapshot


def _preview(**kwargs):
    raise AssertionError("never called by registration tests")


def _mutation(**kwargs):
    raise AssertionError("never called by registration tests")


def _other_mutation(**kwargs):
    raise AssertionError("never called by registration tests")


def _policy(**kwargs):
    raise AssertionError("never called by registration tests")


def _other_policy(**kwargs):
    raise AssertionError("never called by registration tests")


def _fake_spec(**overrides) -> GuardedToolSpec:
    fields = dict(
        tool_name="fake_tool",
        preview_fn=_preview,
        mutation_fn=_mutation,
        selector_argument_names=("selector",),
        resource="fake_resource",
        hard_delete=False,
        reversibility="reversible",
        policy_fn=_policy,
    )
    fields.update(overrides)
    return GuardedToolSpec(**fields)


def test_new_unique_spec_is_registered(isolated_registry):
    spec = _fake_spec()
    register_tool(spec)
    assert get_tool_spec("fake_tool") is spec
    assert registered_tool_names() == EXPECTED_TOOLS + ("fake_tool",)


def test_reregistering_an_equal_spec_is_a_noop(isolated_registry):
    first = _fake_spec()
    second = _fake_spec()
    assert first is not second and first == second

    register_tool(first)
    register_tool(second)

    assert get_tool_spec("fake_tool") is first
    assert registered_tool_names().count("fake_tool") == 1


def test_reregistering_each_existing_operations_spec_is_a_noop(isolated_registry):
    for name in EXPECTED_TOOLS:
        spec = get_tool_spec(name)
        register_tool(spec)
        register_tool(dataclasses.replace(spec))
        assert get_tool_spec(name) is spec
    assert registered_tool_names() == EXPECTED_TOOLS


@pytest.mark.parametrize(
    "override",
    [
        {"mutation_fn": _other_mutation},
        {"preview_fn": lambda **kwargs: None},
        {"selector_argument_names": ("selector", "extra")},
        {"resource": "other_resource"},
        {"hard_delete": True},
        {"reversibility": "irreversible_without_snapshot"},
        {"policy_fn": _other_policy},
    ],
    ids=["mutation_fn", "preview_fn", "selectors", "resource", "hard_delete", "reversibility", "policy_fn"],
)
def test_conflicting_spec_raises_and_keeps_existing(isolated_registry, override):
    original = _fake_spec()
    register_tool(original)

    with pytest.raises(ValueError, match="fake_tool"):
        register_tool(_fake_spec(**override))

    assert get_tool_spec("fake_tool") is original


def test_operations_specs_share_one_stable_policy_fn_across_registrations(isolated_registry):
    from proofgate.bindings.operations import evaluate_operations_policy, register_operations_tools

    before = {name: get_tool_spec(name) for name in EXPECTED_TOOLS}
    register_operations_tools()
    register_operations_tools()

    for name in EXPECTED_TOOLS:
        spec = get_tool_spec(name)
        assert spec is before[name]
        assert spec.policy_fn is evaluate_operations_policy
    assert registered_tool_names() == EXPECTED_TOOLS


def test_conflicting_spec_cannot_replace_an_operations_tool(isolated_registry):
    real = get_tool_spec("delete_users")
    with pytest.raises(ValueError, match="delete_users"):
        register_tool(dataclasses.replace(real, hard_delete=False))
    assert get_tool_spec("delete_users") is real


def test_normal_import_still_registers_exactly_the_three_operations_tools():
    assert registered_tool_names() == EXPECTED_TOOLS

    delete_spec = get_tool_spec("delete_users")
    assert delete_spec.selector_argument_names == ("inactive_days", "environment")
    assert delete_spec.resource == "users"
    assert delete_spec.hard_delete is True
    assert delete_spec.reversibility == "irreversible_without_snapshot"

    deactivate_spec = get_tool_spec("deactivate_users")
    assert deactivate_spec.selector_argument_names == ("inactive_days", "environment")
    assert deactivate_spec.resource == "users"
    assert deactivate_spec.hard_delete is False
    assert deactivate_spec.reversibility == "reversible"

    flag_spec = get_tool_spec("set_feature_flag")
    assert flag_spec.selector_argument_names == (
        "flag_name",
        "enabled",
        "environment",
        "rollout_percentage",
    )
    assert flag_spec.resource == "feature_flags"
    assert flag_spec.hard_delete is False
    assert flag_spec.reversibility == "reversible"
