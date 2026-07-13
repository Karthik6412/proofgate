"""Slice 21 tests: independent agent tool-selection proposal
(agent/agent_proposal.py).

Every test here uses a fake OpenAI-compatible client (mirroring
tests/test_nebius_live.py's FakeNebiusClient convention) -- no test ever
calls the real Nebius API. propose_mcp_action never executes anything and
never evaluates policy; these tests only exercise proposal validation and
failure classification.
"""

import json

import pytest
from pydantic import ValidationError

from agent.agent_proposal import (
    AgentToolProposal,
    DiscoveredTool,
    ProposedMutationArguments,
    propose_mcp_action,
)

DISCOVERED_TOOLS = [
    DiscoveredTool(
        name="delete_users",
        description="Guarded hard delete of inactive user accounts.",
        input_schema={"type": "object", "properties": {}},
    ),
    DiscoveredTool(
        name="deactivate_users",
        description="Guarded reversible deactivation of inactive user accounts.",
        input_schema={"type": "object", "properties": {}},
    ),
]

INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class FakeProposalClient:
    def __init__(self, content=None, exception=None):
        self.content = content
        self.exception = exception
        self.chat = self
        self.completions = self
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exception is not None:
            raise self.exception
        return _FakeResponse(self.content)


def _valid_payload(tool_name="delete_users", inactive_days=90, environment="test", explanation=None):
    payload = {"tool_name": tool_name, "arguments": {"inactive_days": inactive_days, "environment": environment}}
    if explanation is not None:
        payload["explanation"] = explanation
    return payload


# ---------------------------------------------------------------------------
# AgentToolProposal / ProposedMutationArguments validation
# ---------------------------------------------------------------------------


def test_valid_delete_users_proposal():
    proposal = AgentToolProposal.model_validate(_valid_payload("delete_users", 90, "test"))
    assert proposal.tool_name == "delete_users"
    assert proposal.arguments.inactive_days == 90
    assert proposal.arguments.environment == "test"


def test_valid_deactivate_users_proposal():
    proposal = AgentToolProposal.model_validate(_valid_payload("deactivate_users", 90, "test"))
    assert proposal.tool_name == "deactivate_users"


def test_environment_null_is_accepted():
    proposal = AgentToolProposal.model_validate(_valid_payload(environment=None))
    assert proposal.arguments.environment is None


def test_environment_test_is_accepted():
    proposal = AgentToolProposal.model_validate(_valid_payload(environment="test"))
    assert proposal.arguments.environment == "test"


def test_environment_production_is_accepted():
    proposal = AgentToolProposal.model_validate(_valid_payload(environment="production"))
    assert proposal.arguments.environment == "production"


def test_unsupported_environment_alias_rejected():
    with pytest.raises(ValidationError):
        AgentToolProposal.model_validate(_valid_payload(environment="testing"))


def test_unknown_top_level_field_rejected():
    payload = _valid_payload()
    payload["workflow_id"] = "evil-injected-workflow"
    with pytest.raises(ValidationError):
        AgentToolProposal.model_validate(payload)


def test_unknown_argument_field_rejected():
    payload = _valid_payload()
    payload["arguments"]["extra_field"] = "surprise"
    with pytest.raises(ValidationError):
        AgentToolProposal.model_validate(payload)


def test_missing_required_argument_rejected():
    payload = {"tool_name": "delete_users", "arguments": {"environment": "test"}}
    with pytest.raises(ValidationError):
        AgentToolProposal.model_validate(payload)


def test_non_positive_inactivity_rejected():
    with pytest.raises(ValidationError):
        AgentToolProposal.model_validate(_valid_payload(inactive_days=0))
    with pytest.raises(ValidationError):
        AgentToolProposal.model_validate(_valid_payload(inactive_days=-5))


def test_invalid_inactivity_type_rejected():
    with pytest.raises(ValidationError):
        ProposedMutationArguments.model_validate({"inactive_days": "ninety", "environment": "test"})


@pytest.mark.parametrize(
    "control_plane_field",
    ["rollback_proof", "requesting_user", "agent_id", "workflow_id", "instruction"],
)
def test_control_plane_fields_rejected_in_arguments(control_plane_field):
    payload = _valid_payload()
    payload["arguments"][control_plane_field] = "should not be allowed"
    with pytest.raises(ValidationError):
        AgentToolProposal.model_validate(payload)


@pytest.mark.parametrize("field", ["verdict", "policy_rules", "proof_status", "audit_event_id"])
def test_verdict_and_policy_fields_rejected(field):
    payload = _valid_payload()
    payload[field] = "ALLOW"
    with pytest.raises(ValidationError):
        AgentToolProposal.model_validate(payload)


# ---------------------------------------------------------------------------
# propose_mcp_action: success, discovery-based rejection, failure handling
# ---------------------------------------------------------------------------


def test_propose_mcp_action_success():
    client = FakeProposalClient(content=json.dumps(_valid_payload("deactivate_users", 90, "test")))
    outcome = propose_mcp_action(INSTRUCTION, DISCOVERED_TOOLS, client=client)

    assert outcome.status == "VALID_PROPOSAL"
    assert outcome.proposal.tool_name == "deactivate_users"
    assert outcome.source == "live"
    assert outcome.error_category is None
    assert len(client.calls) == 1


def test_propose_mcp_action_unknown_tool_not_in_discovery():
    client = FakeProposalClient(content=json.dumps(_valid_payload("reactivate_users", 90, "test")))
    outcome = propose_mcp_action(INSTRUCTION, DISCOVERED_TOOLS, client=client)

    assert outcome.status == "PROPOSAL_FAILURE"
    assert outcome.error_category == "unknown_tool"
    assert outcome.proposal is None


def test_propose_mcp_action_malformed_json():
    client = FakeProposalClient(content="{not valid json")
    outcome = propose_mcp_action(INSTRUCTION, DISCOVERED_TOOLS, client=client)

    assert outcome.status == "PROPOSAL_FAILURE"
    assert outcome.error_category == "invalid_response"


def test_propose_mcp_action_control_plane_injection_rejected():
    payload = _valid_payload()
    payload["arguments"]["workflow_id"] = "sneaky"
    client = FakeProposalClient(content=json.dumps(payload))
    outcome = propose_mcp_action(INSTRUCTION, DISCOVERED_TOOLS, client=client)

    assert outcome.status == "PROPOSAL_FAILURE"
    assert outcome.error_category == "invalid_response"


def test_propose_mcp_action_upstream_exception():
    client = FakeProposalClient(exception=ConnectionError("simulated upstream failure"))
    outcome = propose_mcp_action(INSTRUCTION, DISCOVERED_TOOLS, client=client)

    assert outcome.status == "PROPOSAL_FAILURE"
    assert outcome.error_category == "upstream_failure"


def test_propose_mcp_action_timeout():
    client = FakeProposalClient(exception=TimeoutError("simulated timeout"))
    outcome = propose_mcp_action(INSTRUCTION, DISCOVERED_TOOLS, client=client)

    assert outcome.status == "PROPOSAL_FAILURE"
    assert outcome.error_category == "upstream_failure"


def test_propose_mcp_action_makes_at_most_one_provider_request():
    client = FakeProposalClient(content=json.dumps(_valid_payload()))
    propose_mcp_action(INSTRUCTION, DISCOVERED_TOOLS, client=client)
    assert len(client.calls) == 1


def test_propose_mcp_action_never_retries_on_failure():
    client = FakeProposalClient(exception=ConnectionError("simulated"))
    propose_mcp_action(INSTRUCTION, DISCOVERED_TOOLS, client=client)
    assert len(client.calls) == 1


def test_propose_mcp_action_missing_configuration_without_client(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "false")
    outcome = propose_mcp_action(INSTRUCTION, DISCOVERED_TOOLS)
    assert outcome.status == "PROPOSAL_FAILURE"
    assert outcome.error_category == "missing_configuration"


# ---------------------------------------------------------------------------
# MCP discovery usage: real discovery output drives the prompt, no
# hardcoded duplicate tool list
# ---------------------------------------------------------------------------


def test_prompt_uses_the_actual_discovered_tools_not_a_hardcoded_list():
    from agent.agent_proposal import _build_user_prompt

    custom_tools = [
        DiscoveredTool(name="totally_different_tool", description="A hypothetical tool.", input_schema={"type": "object"})
    ]
    prompt = _build_user_prompt(INSTRUCTION, custom_tools)
    assert "totally_different_tool" in prompt
    assert "delete_users" not in prompt
    assert "deactivate_users" not in prompt


def test_changing_mocked_discovery_changes_the_prompt_context():
    from agent.agent_proposal import _build_user_prompt

    prompt_a = _build_user_prompt(INSTRUCTION, DISCOVERED_TOOLS)
    custom_tools = [DiscoveredTool(name="only_one_tool", description="x", input_schema={"type": "object"})]
    prompt_b = _build_user_prompt(INSTRUCTION, custom_tools)
    assert prompt_a != prompt_b


def test_discovered_names_and_descriptions_pass_through_unchanged():
    from agent.agent_proposal import _build_user_prompt

    tool = DiscoveredTool(
        name="delete_users",
        description="EXACT REAL DESCRIPTION STRING FROM DISCOVERY",
        input_schema={"type": "object", "properties": {"inactive_days": {"type": "integer"}}},
    )
    prompt = _build_user_prompt(INSTRUCTION, [tool])
    assert "EXACT REAL DESCRIPTION STRING FROM DISCOVERY" in prompt
    assert '"inactive_days"' in prompt


# ---------------------------------------------------------------------------
# Prompt does not leak forbidden hints (Part E)
# ---------------------------------------------------------------------------


def test_system_prompt_does_not_mention_policy_rules_or_expected_outcome():
    from agent.agent_proposal import _PROPOSAL_SYSTEM_PROMPT

    forbidden_terms = [
        "BLOCK",
        "ALLOW",
        "RULE_",
        "scope-drop",
        "scope drop",
        "expected",
        "blocked",
        "preferred",
        "safer",
        "reversible",
    ]
    lowered = _PROPOSAL_SYSTEM_PROMPT.lower()
    for term in forbidden_terms:
        assert term.lower() not in lowered
