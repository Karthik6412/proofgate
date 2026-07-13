"""ProofGate MCP guarded gateway (Slice 19).

Exposes exactly two consequential tools -- the same two, and only the two,
already registered in proofgate/registry.py -- to an external MCP client:

    delete_users
    deactivate_users

Every call that reaches guarded execution routes through the existing,
unchanged proofgate.core.guarded_execute(...) boundary. This module
contains no policy, proof, selector-hash, workflow-budget, postcondition,
or audit-writing logic of its own -- it only validates/normalizes MCP
input, builds the existing ActionContext/RollbackProof models, calls
guarded_execute, and reads back the one AuditEvent guarded_execute already
wrote (via _find_audit_event, a small read-only JSONL scan local to this
module) to build an honest structured response. Operations functions,
preview functions, snapshot creation, database reset, and audit reset are
never exposed here.

Package / version used for this slice: `mcp` 1.28.1 (already an approved
project dependency; installed for CRAFT's client-side MCP usage, now also
used here to build a server). Transport: stdio, via the SDK's low-level
mcp.server.lowlevel.Server (not FastMCP's @tool() sugar) -- FastMCP's
auto-generated per-parameter argument model was verified empirically to
silently ignore unknown/misspelled fields with no configuration knob to
change that, which would violate this slice's input-normalization policy
(reject unknown fields).

Validation layering: the low-level Server's call_tool() decorator can
optionally run `jsonschema.validate(arguments, tool.inputSchema)` before
the handler runs (validate_input=True, its default). This was tried
first and rejected: raw JSON Schema's `type: integer` check rejects a
numeric *string* like "90" outright (a JSON string is never a JSON
Schema "integer"), which conflicts with this slice's explicitly allowed
"90" -> 90 normalization (Part D) -- verified empirically. This module
therefore registers with `@server.call_tool(validate_input=False)` and
uses exactly one validation layer: GuardedToolRequest, a Pydantic model
with `model_config = ConfigDict(extra="forbid")`. Pydantic's own
`extra="forbid"` rejects unknown/misspelled fields (e.g. "enviroment")
just as strictly as the jsonschema approach would have, while its native
lax-mode int handling still safely coerces "90" -> 90 and still rejects
non-numeric strings, floats-with-a-fractional-part, and negative/zero
values (via Field(gt=0)) -- all verified empirically before writing this
module. inputSchema is still published (from the same Pydantic model) for
honest MCP discovery/introspection; it is simply not used to gate calls
a second time in a way that would conflict with the chosen normalization
rules.

Concurrency contract: the tool handler below is `async def` (required by
the installed SDK's lowlevel Server API), but the guarded pipeline itself
(guarded_execute) is a plain synchronous function called with no
surrounding `await`. Verified by reading mcp.server.lowlevel.server.Server
.run(): each incoming message is dispatched via `anyio.create_task_group().
start_soon(...)` onto a single-threaded asyncio event loop. A synchronous
call has no internal await/yield point, so once one guarded_execute(...)
call begins, the event loop cannot schedule or advance any other task
(including a second, already-queued tool call) until it returns. This
gives true, provable serialization of every consequential call in this
one server process -- with no explicit lock, semaphore, or queue. See
test_mcp_server.py's concurrency test, which proves this with a
deterministic in-flight counter (not a timing-based/flaky test), rather
than merely asserting it. Because no explicit lock exists, Part I's
bounded-queue-wait requirements are not applicable to this
implementation; this is stated explicitly rather than silently skipped.

This guarantee is scoped to *this one server process*. It says nothing
about a second, independent process (e.g. the Streamlit demo, or a second
MCP server instance) mutating the same operations/working.db
concurrently -- that cross-process scenario is out of scope for this
slice and is not claimed to be safe.

Logging: configured to stderr only (see _configure_logging below).
Stdio's stdout is reserved exclusively for MCP JSON-RPC protocol frames;
this module never calls print() and never lets an ordinary log record
reach stdout. FastMCP's own configure_logging (unused here, since this
module uses the lowlevel Server, not FastMCP) also defaults to stderr in
this environment (the optional `rich` dependency isn't installed, so it
falls back to logging.StreamHandler(), which itself defaults to
sys.stderr) -- confirmed empirically before writing this module.

Graceful shutdown: SIGTERM (and SIGINT, in addition to Python's default
KeyboardInterrupt handling) sets a module-level shutdown flag. Once set,
new consequential tool calls are rejected with a safe structured error
before any guarded/Operations work begins; an already-in-flight
synchronous guarded_execute(...) call is not interrupted (it cannot be,
short of killing the process -- consequential execution is not claimed to
be cancellation-safe once started). The raw signal handler itself does
only two things: set the flag and log one line to stderr; it performs no
filesystem or database work. This is a local-development gateway: it does
not claim protection from SIGKILL, power loss, or abrupt process
termination.

Run it with:

    python -m proofgate.mcp_server

Importing this module (e.g. from tests) never starts a server -- the
Server is constructed and its handlers are registered at import time
(cheap, side-effect-free beyond one startup registry-consistency
assertion), but stdio transport only begins inside main(), guarded by
`if __name__ == "__main__":`.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
from pathlib import Path
from typing import Annotated, Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

import proofgate.audit as audit_module
from proofgate.core import guarded_execute
from proofgate.models import ActionContext, RollbackProof
from proofgate.registry import registered_tool_names
from proofgate.runtime_mode import apply_runtime_mode_to_environment, mode_label

logger = logging.getLogger("proofgate.mcp_server")


def _configure_logging() -> None:
    """Ensure this module's own log records go to stderr, never stdout.

    Idempotent: safe to call more than once (won't duplicate handlers).
    Does not touch the root logger's other handlers or reconfigure
    third-party loggers beyond attaching this one stream handler if this
    logger doesn't already have one.
    """
    if logger.handlers:
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_configure_logging()

# ---------------------------------------------------------------------------
# Explicit public MCP surface (Part B). Deliberately hardcoded -- never
# derived by iterating the registry, so adding a third registry entry in a
# future slice does not silently expose it through MCP without review.
# ---------------------------------------------------------------------------

_PUBLIC_MCP_TOOLS: tuple[str, ...] = ("delete_users", "deactivate_users")

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "delete_users": (
        "Guarded hard delete of inactive user accounts, routed through "
        "ProofGate's deterministic policy engine. Irreversible: BLOCKed "
        "without a valid rollback proof when production rows are included "
        "or the workflow budget would be exceeded."
    ),
    "deactivate_users": (
        "Guarded reversible deactivation of inactive user accounts, routed "
        "through the same deterministic policy engine as delete_users. "
        "Reversible: no rollback proof is required or checked."
    ),
}


def _assert_registry_matches_public_surface() -> None:
    """Startup consistency check only -- not a dynamic-publication
    mechanism. Fails loudly at import time if this module's hardcoded
    public tool list and the real registry (proofgate/registry.py) have
    drifted apart, rather than silently exposing or silently omitting a
    tool.
    """
    registered = set(registered_tool_names())
    missing = [name for name in _PUBLIC_MCP_TOOLS if name not in registered]
    if missing:
        raise RuntimeError(
            "ProofGate MCP gateway declares public tools that are not "
            f"present in proofgate.registry: {missing}. Registered tools: "
            f"{sorted(registered)}."
        )


_assert_registry_matches_public_surface()

# Slice 20: resolve the centralized runtime mode once at import/startup and,
# only for FALLBACK/RELIABLE_DEMO, force the legacy NEBIUS_LIVE_ENABLED /
# CRAFT_LIVE_ENABLED flags off (mirrors app.py's identical call). For LIVE,
# this deliberately leaves both flags untouched: guarded_execute's own
# Nebius extraction call already attempts live and degrades gracefully
# exactly as it does for Streamlit; this gateway never calls CRAFT at all
# (CRAFT integration is out of scope for MCP tool handlers). No network
# call happens here -- only an environment-variable resolution.
_RESOLVED_RUNTIME_MODE = apply_runtime_mode_to_environment()

# ---------------------------------------------------------------------------
# Part D: explicit, narrow input-normalization policy.
# ---------------------------------------------------------------------------

_SUPPORTED_ENVIRONMENTS = ("test", "production")


def _normalize_environment(value: Any) -> str | None:
    """Trim and lowercase a supplied environment string; reject anything
    that isn't None or one of the two supported values once normalized.
    Case is the only normalization applied here -- "testing" and other
    aliases are deliberately rejected, not guessed at."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("environment must be a string or null.")
    normalized = value.strip().lower()
    if normalized not in _SUPPORTED_ENVIRONMENTS:
        raise ValueError(
            f"Unsupported environment {value!r}. Must be one of "
            f"{_SUPPORTED_ENVIRONMENTS} (case-insensitive), or null."
        )
    return normalized


def _trim_required_text(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


class RollbackProofInput(BaseModel):
    """Mirrors proofgate.models.RollbackProof exactly. No filesystem paths
    are accepted -- validate_rollback_proof (unchanged, called only inside
    the existing guarded pipeline) already cross-checks these fields
    against authoritative server-side snapshot metadata; it never trusts
    the caller alone."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    selector_hash: str = Field(min_length=1)
    max_affected_rows: int = Field(gt=0)


_TrimmedText = Annotated[str, BeforeValidator(_trim_required_text), Field(min_length=1)]
_NormalizedEnvironment = Annotated[str | None, BeforeValidator(_normalize_environment)]


class GuardedToolRequest(BaseModel):
    """Shared input shape for both public MCP tools (Part C). This is the
    sole validation layer (see module docstring): extra="forbid" rejects
    unknown/misspelled fields (e.g. "enviroment"), inactive_days safely
    accepts a numeric string ("90") via Pydantic's own lax int handling
    while rejecting non-numeric strings/floats, and environment is
    normalized/validated by _normalize_environment below."""

    model_config = ConfigDict(extra="forbid")

    instruction: _TrimmedText
    workflow_id: _TrimmedText
    inactive_days: int = Field(gt=0)
    environment: _NormalizedEnvironment = None
    rollback_proof: RollbackProofInput | None = None
    requesting_user: str = Field(default="mcp-client", min_length=1)
    agent_id: str = Field(default="mcp-gateway", min_length=1)


# ---------------------------------------------------------------------------
# Graceful shutdown (Part M). The raw signal handler only sets a flag and
# logs one line -- no filesystem/database work happens inside it.
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _handle_termination_signal(signum: int, frame: Any) -> None:  # pragma: no cover - exercised via direct call in tests
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Received signal %s; no new consequential MCP calls will be accepted.", signum)


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_termination_signal)
    signal.signal(signal.SIGINT, _handle_termination_signal)


def reset_shutdown_flag_for_testing() -> None:
    """Test-only helper: restores the module to its pre-shutdown state."""
    global _shutdown_requested
    _shutdown_requested = False


# ---------------------------------------------------------------------------
# The MCP server itself.
# ---------------------------------------------------------------------------

server: Server = Server("proofgate-mcp-gateway")


def _tool_definitions() -> list[types.Tool]:
    schema = GuardedToolRequest.model_json_schema()
    return [
        types.Tool(name=name, description=_TOOL_DESCRIPTIONS[name], inputSchema=schema)
        for name in _PUBLIC_MCP_TOOLS
    ]


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return _tool_definitions()


def _find_audit_event(event_id: str) -> dict | None:
    """Read-only scan of the existing shared JSONL audit log for the one
    event guarded_execute already wrote for this call. Deliberately a
    small, independent utility local to this module (not imported from
    app_logic.py, which is Streamlit-specific and must not be a dependency
    of the MCP gateway) -- it duplicates a ~10-line read pattern, never any
    audit-writing or schema logic."""
    audit_path = audit_module.DEFAULT_AUDIT_PATH
    if not audit_path.exists():
        return None
    for line in audit_path.read_text().strip().splitlines():
        if not line:
            continue
        raw = json.loads(line)
        if raw.get("event_id") == event_id:
            return raw
    return None


def _serialize_result(tool_name: str, result) -> dict[str, Any]:
    """Build the honest structured MCP response. Uses the existing
    EnforcementResult's own model_dump(mode="json") for its fields, and
    merges in the richer fields (impact, proof status/checks, mutation
    result, postcondition, workflow budget) from the one AuditEvent
    guarded_execute already wrote -- every one of those values is already
    a JSON-safe primitive (the audit event was itself read back from a
    JSONL line originally written via AuditEvent.model_dump_json()), so
    nothing further needs converting. BLOCK and ALLOW share this exact
    same shape; fields that don't apply (e.g. mutation_result on BLOCK)
    are simply None, not omitted.
    """
    payload = result.model_dump(mode="json")
    audit_event = _find_audit_event(result.audit_event_id) or {}
    payload["tool_name"] = tool_name
    payload["impact_envelope"] = audit_event.get("impact_envelope")
    payload["proof_status"] = audit_event.get("proof_status")
    payload["proof_checks"] = audit_event.get("proof_checks")
    payload["mutation_result"] = audit_event.get("mutation_result")
    payload["postcondition_result"] = audit_event.get("postcondition_result")
    payload["workflow_budget"] = audit_event.get("workflow_budget_after")
    # Slice 20: additive, backward-compatible integration-source metadata --
    # both fields already existed on the AuditEvent this call already
    # wrote; BLOCK and ALLOW responses still share one identical key set.
    payload["extraction_mode"] = audit_event.get("extraction_mode")
    payload["nebius_model"] = audit_event.get("nebius_model")
    return payload


def _invoke_guarded_tool(tool_name: str, request: GuardedToolRequest) -> dict[str, Any]:
    """The entire adapter: build the existing ActionContext/RollbackProof,
    call the existing shared guarded_execute(...) boundary once, and
    serialize its result. No preview, intent, policy, proof, selector,
    budget, mutation, postcondition, or audit logic lives here -- all of
    it is the same shared pipeline every other caller (Streamlit, direct
    Python, prior slices' tests) already goes through unchanged.
    """
    action_context = ActionContext(
        workflow_id=request.workflow_id,
        requesting_user=request.requesting_user,
        agent_id=request.agent_id,
        original_instruction=request.instruction,
    )
    rollback_proof = (
        RollbackProof(**request.rollback_proof.model_dump()) if request.rollback_proof else None
    )
    result = guarded_execute(
        tool_name,
        action_context,
        {"inactive_days": request.inactive_days, "environment": request.environment},
        rollback_proof,
    )
    return _serialize_result(tool_name, result)


@server.call_tool(validate_input=False)
async def _call_tool(name: str, arguments: dict) -> dict[str, Any]:
    if name not in _PUBLIC_MCP_TOOLS:
        raise ValueError(
            f"Unknown tool {name!r}. This gateway exposes only {_PUBLIC_MCP_TOOLS}."
        )

    if _shutdown_requested:
        raise ValueError(
            "ProofGate MCP gateway is shutting down and is not accepting new "
            "consequential requests. Retry against a new server instance."
        )

    try:
        request = GuardedToolRequest.model_validate(arguments)
    except ValidationError as exc:
        raise ValueError(f"Invalid arguments for {name}: {exc}") from exc

    try:
        return _invoke_guarded_tool(name, request)
    except Exception:
        logger.exception("Unexpected internal error while executing MCP tool %r.", name)
        raise ValueError(f"Internal error while executing {name!r}. See server logs.") from None


async def _run_stdio() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """Local stdio entrypoint: `python -m proofgate.mcp_server`."""
    _install_signal_handlers()
    logger.info(
        "Starting ProofGate MCP gateway (stdio transport, tools=%s, runtime_mode=%s)...",
        _PUBLIC_MCP_TOOLS,
        mode_label(_RESOLVED_RUNTIME_MODE),
    )
    try:
        anyio.run(_run_stdio)
    except KeyboardInterrupt:  # pragma: no cover - real process signal path
        logger.info("Shutdown requested (SIGINT) -- stopping gateway.")
    logger.info("ProofGate MCP gateway stopped cleanly.")


if __name__ == "__main__":
    main()
