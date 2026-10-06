"""Minimal in-code guarded-tool registry.

Explicit and local by design: no plugin loading, no config-driven
registration, no dynamic imports, no filesystem discovery, no policy DSL,
no generic schema generation. This module is generic machinery only: it
imports no domain package and starts empty. A domain binding (for
Operations, proofgate/bindings/operations.py) adds its tools through
register_tool, and each runtime entrypoint calls that binding explicitly.

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


_REGISTRY: dict[str, GuardedToolSpec] = {}


def register_tool(spec: GuardedToolSpec) -> None:
    """Add spec to the registry under spec.tool_name.

    Re-registering an equal spec is a no-op. Equality is the frozen
    dataclass's field-by-field comparison, so preview_fn/mutation_fn
    compare by object identity: registering module-level functions again
    is idempotent, while a different callable or different metadata under
    an already-registered name raises ValueError and leaves the existing
    spec in place -- a registration is never silently replaced or dropped.
    """
    existing = _REGISTRY.get(spec.tool_name)
    if existing is None:
        _REGISTRY[spec.tool_name] = spec
        return
    if existing != spec:
        raise ValueError(
            f"Tool {spec.tool_name!r} is already registered with a different "
            "spec; refusing to replace it."
        )


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
