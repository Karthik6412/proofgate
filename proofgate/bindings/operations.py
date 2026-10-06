"""Trusted Operations binding: registers the Operations tools into the
generic proofgate.registry.

operations/ is the domain; this module is the adapter between it and the
generic ProofGate machinery. The registry never imports this module --
each runtime entrypoint (tests/conftest.py, proofgate/mcp_server.py,
app.py) calls register_operations_tools() explicitly. Importing this
module registers nothing by itself.
"""

import operations.actions as operations_actions
import operations.feature_flags as operations_feature_flags
from proofgate.models import ImpactEnvelope, MutationResult
from proofgate.registry import GuardedToolSpec, register_tool


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


OPERATIONS_TOOL_SPECS: tuple[GuardedToolSpec, ...] = (
    GuardedToolSpec(
        tool_name="delete_users",
        preview_fn=_delete_users_preview,
        mutation_fn=_delete_users_mutation,
        selector_argument_names=("inactive_days", "environment"),
        resource="users",
        hard_delete=True,
        reversibility="irreversible_without_snapshot",
    ),
    GuardedToolSpec(
        tool_name="deactivate_users",
        preview_fn=_deactivate_users_preview,
        mutation_fn=_deactivate_users_mutation,
        selector_argument_names=("inactive_days", "environment"),
        resource="users",
        hard_delete=False,
        reversibility="reversible",
    ),
    GuardedToolSpec(
        tool_name="set_feature_flag",
        preview_fn=_set_feature_flag_preview,
        mutation_fn=_set_feature_flag_mutation,
        selector_argument_names=("flag_name", "enabled", "environment", "rollout_percentage"),
        resource="feature_flags",
        hard_delete=False,
        reversibility="reversible",
    ),
)


def register_operations_tools() -> None:
    """Register the three Operations tools. Idempotent: the specs above are
    built once per process from module-level functions, so a repeat call
    (Streamlit rerun, a second entrypoint in the same process) re-registers
    equal specs and is a no-op."""
    for spec in OPERATIONS_TOOL_SPECS:
        register_tool(spec)
