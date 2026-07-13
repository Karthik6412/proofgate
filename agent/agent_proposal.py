"""Independent agent tool-selection proposal (Slice 21).

This is a completely separate capability from agent.nebius_client's
existing extract_intent_live_or_fallback / extract_risk_features_live_or_
fallback. Those extract semantic features from an instruction that a
human/script has already bound to a specific tool call; this module asks
a real model to independently CHOOSE which discovered MCP tool to call
and propose that tool's mutation-selecting business arguments, given
only the plain-language instruction and the live-discovered tool
definitions. Different prompt, different schema, different purpose. It
is never used as though it were intent/risk extraction, and it never
evaluates policy, computes risk, or produces an EnforcementResult -- it
only proposes.

Reuses agent.nebius_client's existing provider-client construction
(_build_client, model(), live_enabled(), has_api_key()) because that is
pure connectivity/configuration infrastructure, not prompt or schema
logic -- re-deriving a second OpenAI-compatible client builder would be
pure duplication. The proposal prompt, response schema, and validation
below are entirely new and independent.

The validated AgentToolProposal can express only a tool name and its two
mutation-selecting business arguments (inactive_days, environment). Both
ProposedMutationArguments and AgentToolProposal use
model_config = ConfigDict(extra="forbid"), so any attempt by the model to
also emit instruction/workflow_id/rollback_proof/requesting_user/
agent_id/budget/policy/verdict/audit fields -- whether nested inside
"arguments" or alongside "tool_name" -- is rejected by construction, not
by ad hoc field-name blocklisting.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.nebius_client import _build_client, has_api_key, live_enabled, model

logger = logging.getLogger("agent.agent_proposal")


def _configure_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_configure_logging()


class DiscoveredTool(BaseModel):
    """The real name/description/input schema for one MCP tool, exactly as
    returned by a live mcp.ClientSession.list_tools() call. Never a
    hardcoded duplicate -- callers must build this from actual discovery
    output."""

    name: str
    description: str
    input_schema: dict


class ProposedMutationArguments(BaseModel):
    """The only business arguments the model may propose. extra="forbid"
    rejects any other key (workflow_id, rollback_proof, instruction,
    requesting_user, agent_id, budget/policy/verdict/audit fields, ...)
    outright."""

    model_config = ConfigDict(extra="forbid")

    inactive_days: int = Field(gt=0)
    environment: Literal["test", "production"] | None = None


class AgentToolProposal(BaseModel):
    """The complete, validated model output. extra="forbid" rejects any
    top-level field beyond these three -- the model cannot smuggle
    trusted control-plane data in alongside its business choice."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: ProposedMutationArguments
    explanation: str | None = None


ProposalErrorCategory = Literal[
    "missing_configuration",
    "upstream_failure",
    "invalid_response",
    "unknown_tool",
]


@dataclass
class AgentProposalOutcome:
    """Internal runtime metadata, not a wire contract. status is
    PROPOSAL_FAILURE for any timeout/error/malformed/unknown-tool/invalid-
    argument outcome -- never silently replaced with a deterministic
    guess and never mislabeled as a successful live proposal."""

    status: Literal["VALID_PROPOSAL", "PROPOSAL_FAILURE"]
    proposal: AgentToolProposal | None
    model: str | None
    source: Literal["live"]
    error_category: ProposalErrorCategory | None


_PROPOSAL_SYSTEM_PROMPT = (
    "You help select and parameterize an internal administrative tool call "
    "based on a plain-language request. You will be shown a list of "
    "available tools, each with its real name, description, and JSON input "
    "schema, exactly as returned by live tool discovery. Choose exactly one "
    "tool by name and provide only its mutation-selecting business "
    "arguments: inactive_days (a positive integer number of days) and "
    "environment (one of \"test\", \"production\", or null if unspecified). "
    "Respond with ONLY a single JSON object, no prose, no markdown fences, "
    "matching exactly this shape: "
    '{"tool_name": string, "arguments": {"inactive_days": integer, '
    '"environment": "test"|"production"|null}, "explanation": string '
    "(optional, one brief sentence)}. Do not include any other fields, and "
    "do not include any field other than inactive_days/environment inside "
    "arguments."
)


def _build_user_prompt(instruction: str, discovered_tools: list[DiscoveredTool]) -> str:
    tools_payload = [tool.model_dump() for tool in discovered_tools]
    return (
        f"Request: {instruction}\n\n"
        f"Available tools (from live discovery):\n{json.dumps(tools_payload, indent=2)}"
    )


def _extract_json_object(text: str) -> dict:
    parsed = json.loads(text.strip())
    if not isinstance(parsed, dict):
        raise ValueError("Model response was not a JSON object.")
    return parsed


def _sanitize_exception_message(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if any(marker in lowered for marker in ("authorization:", "bearer ", "api_key", "apikey", "token=", "sk-")):
        return "(details withheld to avoid leaking credentials)"
    return text[:200]


def propose_mcp_action(
    instruction: str,
    discovered_tools: list[DiscoveredTool],
    client=None,
) -> AgentProposalOutcome:
    """Ask a real Nebius-hosted model to independently choose one
    discovered MCP tool and propose that tool's mutation-selecting
    arguments. Makes at most one provider request. Never executes
    anything, never evaluates policy, never produces an
    EnforcementResult. Never raises -- any failure returns
    AgentProposalOutcome(status="PROPOSAL_FAILURE", ...).
    """
    should_attempt = client is not None or (live_enabled() and has_api_key())
    if not should_attempt:
        return AgentProposalOutcome(
            status="PROPOSAL_FAILURE",
            proposal=None,
            model=None,
            source="live",
            error_category="missing_configuration",
        )

    model_name = model()

    # Stage 1: the actual provider request. Any failure here (connection
    # error, timeout, API error) is an upstream failure -- a response was
    # never received at all.
    try:
        active_client = client or _build_client()
        response = active_client.chat.completions.create(
            model=model_name,
            temperature=0,
            messages=[
                {"role": "system", "content": _PROPOSAL_SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(instruction, discovered_tools)},
            ],
        )
    except Exception as exc:  # noqa: BLE001 -- any failure must degrade to PROPOSAL_FAILURE
        logger.warning(
            "Agent proposal provider request failed: %s", _sanitize_exception_message(exc)
        )
        return AgentProposalOutcome(
            status="PROPOSAL_FAILURE",
            proposal=None,
            model=model_name,
            source="live",
            error_category="upstream_failure",
        )

    # Stage 2: a response was received -- parsing/schema/business-rule
    # failures here are all "invalid_response", never "upstream_failure".
    try:
        content = response.choices[0].message.content
        if not content:
            raise ValueError("Empty model response.")
        raw = _extract_json_object(content)
        proposal = AgentToolProposal.model_validate(raw)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "Agent proposal response was invalid: %s", _sanitize_exception_message(exc)
        )
        return AgentProposalOutcome(
            status="PROPOSAL_FAILURE",
            proposal=None,
            model=model_name,
            source="live",
            error_category="invalid_response",
        )

    discovered_names = {tool.name for tool in discovered_tools}
    if proposal.tool_name not in discovered_names:
        logger.warning(
            "Agent proposed an undiscovered tool %r (discovered: %s)",
            proposal.tool_name,
            sorted(discovered_names),
        )
        return AgentProposalOutcome(
            status="PROPOSAL_FAILURE",
            proposal=None,
            model=model_name,
            source="live",
            error_category="unknown_tool",
        )

    return AgentProposalOutcome(
        status="VALID_PROPOSAL",
        proposal=proposal,
        model=model_name,
        source="live",
        error_category=None,
    )
