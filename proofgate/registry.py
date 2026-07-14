"""Minimal in-code guarded-tool registry.

Explicit and local by design: no plugin loading, no config-driven
registration, no dynamic imports, no filesystem discovery, no policy DSL,
no generic schema generation. Adding a tool means adding one more
GuardedToolSpec entry to _REGISTRY below -- nothing else.

The registry only describes a tool; it never evaluates policy, risk, or
proof, and it never decides ALLOW/BLOCK. selector_argument_names is
descriptive metadata used by proofgate.core to extract a selector dict
from the generic arguments dict -- the actual selector canonicalization
and hashing still flows entirely through the single shared implementation
in proofgate/selector.py (via the registered preview/mutation functions,
which already use it), never duplicated here.
"""

from dataclasses import dataclass
from typing import Callable

import operations.actions as operations_actions
import operations.feature_flags as operations_feature_flags
from proofgate.models import ImpactEnvelope, MutationResult


@dataclass(frozen=True)
class GuardedToolSpec:
    tool_name: str
    preview_fn: Callable[..., ImpactEnvelope]
    mutation_fn: Callable[..., MutationResult]
    selector_argument_names: tuple[str, ...]
    resource: str
    hard_delete: bool
    reversibility: str


def _delete_users_preview(**kwargs) -> ImpactEnvelope:
    """Thin indirection that resolves operations.actions.preview_delete_users
    by module-attribute lookup at call time (not a frozen reference bound
    at registry-construction time), so tests can still patch
    operations.actions.preview_delete_users directly -- the real, dumb
    Operations function this ultimately calls."""
    return operations_actions.preview_delete_users(**kwargs)


def _delete_users_mutation(**kwargs) -> MutationResult:
    """Same module-attribute indirection as _delete_users_preview, for
    operations.actions.delete_users."""
    return operations_actions.delete_users(**kwargs)


def _deactivate_users_preview(**kwargs) -> ImpactEnvelope:
    """Call-time module-attribute lookup, same pattern as
    _delete_users_preview, so operations.actions.preview_deactivate_users
    stays patchable in tests."""
    return operations_actions.preview_deactivate_users(**kwargs)


def _deactivate_users_mutation(**kwargs) -> MutationResult:
    """Call-time module-attribute lookup, same pattern as
    _delete_users_mutation, for operations.actions.deactivate_users."""
    return operations_actions.deactivate_users(**kwargs)


def _set_feature_flag_preview(**kwargs) -> ImpactEnvelope:
    """Call-time module-attribute lookup, same pattern as
    _delete_users_preview, so operations.feature_flags.
    preview_set_feature_flag stays patchable in tests. A genuinely
    different resource (filesystem-backed feature-flag JSON, not the
    users database) reached through the exact same registry-dispatch
    mechanism as the two users-table tools."""
    return operations_feature_flags.preview_set_feature_flag(**kwargs)


def _set_feature_flag_mutation(**kwargs) -> MutationResult:
    """Call-time module-attribute lookup, same pattern as
    _delete_users_mutation, for operations.feature_flags.set_feature_flag."""
    return operations_feature_flags.set_feature_flag(**kwargs)


_REGISTRY: dict[str, GuardedToolSpec] = {
    "delete_users": GuardedToolSpec(
        tool_name="delete_users",
        preview_fn=_delete_users_preview,
        mutation_fn=_delete_users_mutation,
        selector_argument_names=("inactive_days", "environment"),
        resource="users",
        hard_delete=True,
        reversibility="irreversible_without_snapshot",
    ),
    "deactivate_users": GuardedToolSpec(
        tool_name="deactivate_users",
        preview_fn=_deactivate_users_preview,
        mutation_fn=_deactivate_users_mutation,
        selector_argument_names=("inactive_days", "environment"),
        resource="users",
        hard_delete=False,
        reversibility="reversible",
    ),
    "set_feature_flag": GuardedToolSpec(
        tool_name="set_feature_flag",
        preview_fn=_set_feature_flag_preview,
        mutation_fn=_set_feature_flag_mutation,
        selector_argument_names=("flag_name", "enabled", "environment", "rollout_percentage"),
        resource="feature_flags",
        hard_delete=False,
        reversibility="reversible",
    ),
}


def get_tool_spec(tool_name: str) -> GuardedToolSpec | None:
    """Look up a registered tool, or None if it isn't registered.

    The generic guarded entrypoint fails closed (RULE_UNKNOWN_IMPACT) on
    None -- it never guesses at unregistered tool behavior.
    """
    return _REGISTRY.get(tool_name)


def registered_tool_names() -> tuple[str, ...]:
    """Read-only enumeration of every registered tool name.

    Used by proofgate/mcp_server.py to assert its explicit, hardcoded
    public MCP tool list actually matches the registry at startup -- never
    used to dynamically publish tools through any transport.
    """
    return tuple(_REGISTRY.keys())
