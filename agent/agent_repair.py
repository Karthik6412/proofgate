"""One bounded, live-only repair-proposal capability (Slice 22).

Mirrors agent/agent_proposal.py's discipline exactly, as a separate
module: a different prompt, a different (but structurally identical)
output schema, and a completely separate purpose -- responding to
ProofGate's own existing suggested_repairs after a real BLOCK, not the
original independent tool-selection choice. propose_repair never
executes anything, never evaluates policy, never creates or attaches
rollback proof, and is called at most once per workflow by the
orchestrating script (scripts/agent_loop.py), never in a loop.

Reuses agent.nebius_client's existing provider-client construction
(_build_client, model(), live_enabled(), has_api_key()) and
agent.agent_proposal's ProposedMutationArguments/DiscoveredTool models --
the actual mutation-argument shape the repair model may propose is
identical to the initial proposal's shape, so there is no reason to
duplicate that narrow, already-hardened model.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from agent.agent_proposal import (
    AgentToolProposal,
    DiscoveredTool,
    ProposedMutationArguments,
    _extract_json_object,
    _sanitize_exception_message,
)
from agent.nebius_client import _build_client, has_api_key, live_enabled, model

logger = logging.getLogger("agent.agent_repair")


def _configure_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_configure_logging()


class SanitizedRepairFeedback(BaseModel):
    """Only what the repair model is allowed to see about the initial
    call's enforcement outcome -- no full MCP response, no audit
    metadata, no workflow ID, no proof internals. suggested_repairs is
    the exact, unmodified list[dict] the real EnforcementResult/MCP
    response already returned (proofgate.models.EnforcementResult.
    suggested_repairs) -- never rewritten, summarized, or reinterpreted.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["BLOCK"]
    triggered_rules: list[str]
    suggested_repairs: list[dict]


class AgentRepairProposal(BaseModel):
    """The complete, validated repair-model output. Identical shape to
    AgentToolProposal (tool_name + narrow ProposedMutationArguments +
    optional explanation), kept as its own class for a clean, separate
    validation-error identity in this module. extra="forbid" rejects any
    other field -- instruction, workflow_id, rollback_proof, proof
    payload/path, snapshot id/hash, selector hash, database path,
    verdict, policy fields, audit metadata, budget values, requesting
    user, agent identity, retry count, or any other metadata -- whether
    at the top level or nested inside arguments."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: ProposedMutationArguments
    explanation: str | None = None


RepairErrorCategory = Literal[
    "missing_configuration",
    "upstream_failure",
    "invalid_response",
    "unknown_tool",
]


@dataclass
class AgentRepairOutcome:
    """Internal runtime metadata, not a wire contract. status is
    PROPOSAL_FAILURE for any timeout/error/malformed/unknown-tool/invalid-
    argument outcome -- never silently replaced with a deterministic
    guess, never retried."""

    status: Literal["VALID_REPAIR", "PROPOSAL_FAILURE"]
    proposal: AgentRepairProposal | None
    model: str | None
    source: Literal["live"]
    error_category: RepairErrorCategory | None


_REPAIR_SYSTEM_PROMPT = (
    "You previously proposed a tool call that was evaluated and blocked by "
    "an enforcement system. You will be shown the original request, your "
    "original proposal, the actual verdict, the actual triggered rule "
    "identifiers, and the system's own suggested repairs (passed through "
    "completely unchanged). Propose exactly one revised choice: a tool name "
    "(from the same list of available tools you were shown) and business "
    "arguments (inactive_days, environment). You may revise the tool "
    "and/or the business arguments. You have no ability to create or "
    "attach recovery proof, a snapshot, or any other evidence -- do not "
    "invent or reference any such thing. Respond with ONLY a single JSON "
    "object, no prose, no markdown fences, matching exactly this shape: "
    '{"tool_name": string, "arguments": {"inactive_days": integer, '
    '"environment": "test"|"production"|null}, "explanation": string '
    "(optional, one brief sentence)}. Do not include any other fields, and "
    "do not include any field other than inactive_days/environment inside "
    "arguments."
)


def _build_repair_user_prompt(
    instruction: str,
    original_proposal: AgentToolProposal,
    feedback: SanitizedRepairFeedback,
    discovered_tools: list[DiscoveredTool],
) -> str:
    tools_payload = [tool.model_dump() for tool in discovered_tools]
    return (
        f"Original request: {instruction}\n\n"
        f"Your original proposal: {json.dumps(original_proposal.model_dump())}\n\n"
        f"Enforcement result: verdict={feedback.verdict}, "
        f"triggered_rules={feedback.triggered_rules}\n\n"
        f"Suggested repairs (from the enforcement system, unchanged): "
        f"{json.dumps(feedback.suggested_repairs)}\n\n"
        f"Available tools (from live discovery):\n{json.dumps(tools_payload, indent=2)}"
    )


def propose_repair(
    instruction: str,
    original_proposal: AgentToolProposal,
    feedback: SanitizedRepairFeedback,
    discovered_tools: list[DiscoveredTool],
    client=None,
) -> AgentRepairOutcome:
    """Ask a real Nebius-hosted model for exactly one revised proposal in
    response to sanitized enforcement feedback. Makes at most one
    provider request. Never executes anything, never evaluates policy,
    never creates an EnforcementResult, never creates or attaches proof.
    Never raises -- any failure returns
    AgentRepairOutcome(status="PROPOSAL_FAILURE", ...). Never retried by
    this function or its caller.
    """
    should_attempt = client is not None or (live_enabled() and has_api_key())
    if not should_attempt:
        return AgentRepairOutcome(
            status="PROPOSAL_FAILURE",
            proposal=None,
            model=None,
            source="live",
            error_category="missing_configuration",
        )

    model_name = model()

    try:
        active_client = client or _build_client()
        response = active_client.chat.completions.create(
            model=model_name,
            temperature=0,
            messages=[
                {"role": "system", "content": _REPAIR_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_repair_user_prompt(
                        instruction, original_proposal, feedback, discovered_tools
                    ),
                },
            ],
        )
    except Exception as exc:  # noqa: BLE001 -- any failure must degrade to PROPOSAL_FAILURE
        logger.warning("Repair proposal provider request failed: %s", _sanitize_exception_message(exc))
        return AgentRepairOutcome(
            status="PROPOSAL_FAILURE",
            proposal=None,
            model=model_name,
            source="live",
            error_category="upstream_failure",
        )

    try:
        content = response.choices[0].message.content
        if not content:
            raise ValueError("Empty model response.")
        raw = _extract_json_object(content)
        proposal = AgentRepairProposal.model_validate(raw)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Repair proposal response was invalid: %s", _sanitize_exception_message(exc))
        return AgentRepairOutcome(
            status="PROPOSAL_FAILURE",
            proposal=None,
            model=model_name,
            source="live",
            error_category="invalid_response",
        )

    discovered_names = {tool.name for tool in discovered_tools}
    if proposal.tool_name not in discovered_names:
        logger.warning(
            "Repair proposed an undiscovered tool %r (discovered: %s)",
            proposal.tool_name,
            sorted(discovered_names),
        )
        return AgentRepairOutcome(
            status="PROPOSAL_FAILURE",
            proposal=None,
            model=model_name,
            source="live",
            error_category="unknown_tool",
        )

    return AgentRepairOutcome(
        status="VALID_REPAIR",
        proposal=proposal,
        model=model_name,
        source="live",
        error_category=None,
    )
