"""Slice 22 tests: one bounded, live-only repair-proposal capability
(agent/agent_repair.py).

Mirrors tests/test_agent_proposal.py's discipline exactly: every test uses
a fake OpenAI-compatible client (reusing the same fake-client shape) --
no test ever calls the real Nebius API. propose_repair never executes
anything, never evaluates policy, never creates or attaches proof; these
tests only exercise repair-proposal validation, prompt content, and
failure classification.
"""

import json

import pytest
from pydantic import ValidationError

from agent.agent_proposal import AgentToolProposal, DiscoveredTool, ProposedMutationArguments
from agent.agent_repair import (
    AgentRepairProposal,
    SanitizedRepairFeedback,
    propose_repair,
)
from proofgate.policy import RULE_RECOVERY_PROOF

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

ORIGINAL_PROPOSAL = AgentToolProposal(
    tool_name="delete_users",
    arguments=ProposedMutationArguments(inactive_days=90, environment=None),
    explanation="Broad cleanup as requested.",
)

FEEDBACK = SanitizedRepairFeedback(
    verdict="BLOCK",
    triggered_rules=[RULE_RECOVERY_PROOF],
    suggested_repairs=[
        {
            "tool": "delete_users",
            "arguments": {"inactive_days": 90, "environment": "test"},
            "next_step": "create_snapshot",
        }
    ],
)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class FakeRepairClient:
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


def _repair_payload(tool_name="deactivate_users", inactive_days=90, environment="test", explanation=None):
    payload = {"tool_name": tool_name, "arguments": {"inactive_days": inactive_days, "environment": environment}}
    if explanation is not None:
        payload["explanation"] = explanation
    return payload


# ---------------------------------------------------------------------------
# SanitizedRepairFeedback validation
# ---------------------------------------------------------------------------


def test_sanitized_repair_feedback_accepts_valid_shape():
    feedback = SanitizedRepairFeedback(
        verdict="BLOCK", triggered_rules=[RULE_RECOVERY_PROOF], suggested_repairs=[{"tool": "delete_users"}]
    )
    assert feedback.verdict == "BLOCK"


def test_sanitized_repair_feedback_rejects_allow_verdict():
    with pytest.raises(ValidationError):
        SanitizedRepairFeedback(verdict="ALLOW", triggered_rules=[], suggested_repairs=[])


@pytest.mark.parametrize(
    "extra_field",
    ["workflow_id", "rollback_proof", "proof_status", "audit_event_id", "budget", "requesting_user"],
)
def test_sanitized_repair_feedback_rejects_extra_fields(extra_field):
    payload = {
        "verdict": "BLOCK",
        "triggered_rules": [RULE_RECOVERY_PROOF],
        "suggested_repairs": [],
        extra_field: "should not be allowed",
    }
    with pytest.raises(ValidationError):
        SanitizedRepairFeedback.model_validate(payload)


def test_sanitized_repair_feedback_suggested_repairs_pass_through_unchanged():
    repairs = [{"tool": "delete_users", "arguments": {"inactive_days": 90, "environment": "test"}}]
    feedback = SanitizedRepairFeedback(verdict="BLOCK", triggered_rules=[RULE_RECOVERY_PROOF], suggested_repairs=repairs)
    assert feedback.suggested_repairs == repairs


# ---------------------------------------------------------------------------
# AgentRepairProposal validation: valid repairs (Part K worked examples)
# ---------------------------------------------------------------------------


def test_valid_unchanged_repair_proposal():
    proposal = AgentRepairProposal.model_validate(_repair_payload("delete_users", 90, None))
    assert proposal.tool_name == "delete_users"
    assert proposal.arguments.environment is None


def test_valid_tool_switch_repair_proposal():
    proposal = AgentRepairProposal.model_validate(_repair_payload("deactivate_users", 90, "test"))
    assert proposal.tool_name == "deactivate_users"


def test_valid_scope_change_repair_proposal():
    proposal = AgentRepairProposal.model_validate(_repair_payload("delete_users", 90, "test"))
    assert proposal.arguments.environment == "test"


def test_valid_threshold_change_repair_proposal():
    proposal = AgentRepairProposal.model_validate(_repair_payload("delete_users", 180, None))
    assert proposal.arguments.inactive_days == 180


def test_valid_multi_field_change_repair_proposal():
    proposal = AgentRepairProposal.model_validate(_repair_payload("deactivate_users", 180, "test"))
    assert proposal.tool_name == "deactivate_users"
    assert proposal.arguments.inactive_days == 180
    assert proposal.arguments.environment == "test"


# ---------------------------------------------------------------------------
# AgentRepairProposal validation: rejections
# ---------------------------------------------------------------------------


def test_unsupported_environment_alias_rejected():
    with pytest.raises(ValidationError):
        AgentRepairProposal.model_validate(_repair_payload(environment="testing"))


def test_missing_required_argument_rejected():
    payload = {"tool_name": "delete_users", "arguments": {"environment": "test"}}
    with pytest.raises(ValidationError):
        AgentRepairProposal.model_validate(payload)


def test_non_positive_inactivity_rejected():
    with pytest.raises(ValidationError):
        AgentRepairProposal.model_validate(_repair_payload(inactive_days=0))


def test_unknown_argument_field_rejected():
    payload = _repair_payload()
    payload["arguments"]["extra_field"] = "surprise"
    with pytest.raises(ValidationError):
        AgentRepairProposal.model_validate(payload)


@pytest.mark.parametrize(
    "control_plane_field",
    [
        "instruction",
        "workflow_id",
        "rollback_proof",
        "proof_data",
        "proof_path",
        "snapshot_id",
        "selector_hash",
        "verdict",
        "policy_rules",
        "audit_event_id",
        "requesting_user",
        "agent_id",
        "retry_count",
    ],
)
def test_control_plane_fields_rejected_at_top_level(control_plane_field):
    payload = _repair_payload()
    payload[control_plane_field] = "should not be allowed"
    with pytest.raises(ValidationError):
        AgentRepairProposal.model_validate(payload)


@pytest.mark.parametrize(
    "control_plane_field",
    ["rollback_proof", "proof_data", "workflow_id", "instruction", "requesting_user"],
)
def test_control_plane_fields_rejected_in_arguments(control_plane_field):
    payload = _repair_payload()
    payload["arguments"][control_plane_field] = "should not be allowed"
    with pytest.raises(ValidationError):
        AgentRepairProposal.model_validate(payload)


def test_arbitrary_nested_metadata_rejected():
    payload = _repair_payload()
    payload["metadata"] = {"anything": "nested"}
    with pytest.raises(ValidationError):
        AgentRepairProposal.model_validate(payload)


# ---------------------------------------------------------------------------
# propose_repair: success, discovery-based rejection, failure handling
# ---------------------------------------------------------------------------


def test_propose_repair_success_tool_switch():
    client = FakeRepairClient(content=json.dumps(_repair_payload("deactivate_users", 90, "test")))
    outcome = propose_repair(INSTRUCTION, ORIGINAL_PROPOSAL, FEEDBACK, DISCOVERED_TOOLS, client=client)

    assert outcome.status == "VALID_REPAIR"
    assert outcome.proposal.tool_name == "deactivate_users"
    assert outcome.source == "live"
    assert outcome.error_category is None
    assert len(client.calls) == 1


def test_propose_repair_unknown_tool_not_in_discovery():
    client = FakeRepairClient(content=json.dumps(_repair_payload("reactivate_users", 90, "test")))
    outcome = propose_repair(INSTRUCTION, ORIGINAL_PROPOSAL, FEEDBACK, DISCOVERED_TOOLS, client=client)

    assert outcome.status == "PROPOSAL_FAILURE"
    assert outcome.error_category == "unknown_tool"
    assert outcome.proposal is None


def test_propose_repair_malformed_json():
    client = FakeRepairClient(content="{not valid json")
    outcome = propose_repair(INSTRUCTION, ORIGINAL_PROPOSAL, FEEDBACK, DISCOVERED_TOOLS, client=client)

    assert outcome.status == "PROPOSAL_FAILURE"
    assert outcome.error_category == "invalid_response"


def test_propose_repair_rollback_proof_injection_rejected():
    payload = _repair_payload()
    payload["rollback_proof"] = {"snapshot_id": "fake"}
    client = FakeRepairClient(content=json.dumps(payload))
    outcome = propose_repair(INSTRUCTION, ORIGINAL_PROPOSAL, FEEDBACK, DISCOVERED_TOOLS, client=client)

    assert outcome.status == "PROPOSAL_FAILURE"
    assert outcome.error_category == "invalid_response"


def test_propose_repair_upstream_exception():
    client = FakeRepairClient(exception=ConnectionError("simulated upstream failure"))
    outcome = propose_repair(INSTRUCTION, ORIGINAL_PROPOSAL, FEEDBACK, DISCOVERED_TOOLS, client=client)

    assert outcome.status == "PROPOSAL_FAILURE"
    assert outcome.error_category == "upstream_failure"


def test_propose_repair_timeout():
    client = FakeRepairClient(exception=TimeoutError("simulated timeout"))
    outcome = propose_repair(INSTRUCTION, ORIGINAL_PROPOSAL, FEEDBACK, DISCOVERED_TOOLS, client=client)

    assert outcome.status == "PROPOSAL_FAILURE"
    assert outcome.error_category == "upstream_failure"


def test_propose_repair_makes_at_most_one_provider_request():
    client = FakeRepairClient(content=json.dumps(_repair_payload()))
    propose_repair(INSTRUCTION, ORIGINAL_PROPOSAL, FEEDBACK, DISCOVERED_TOOLS, client=client)
    assert len(client.calls) == 1


def test_propose_repair_never_retries_on_failure():
    client = FakeRepairClient(exception=ConnectionError("simulated"))
    propose_repair(INSTRUCTION, ORIGINAL_PROPOSAL, FEEDBACK, DISCOVERED_TOOLS, client=client)
    assert len(client.calls) == 1


def test_propose_repair_missing_configuration_without_client(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "false")
    outcome = propose_repair(INSTRUCTION, ORIGINAL_PROPOSAL, FEEDBACK, DISCOVERED_TOOLS)
    assert outcome.status == "PROPOSAL_FAILURE"
    assert outcome.error_category == "missing_configuration"


# ---------------------------------------------------------------------------
# Repair prompt content (Part E)
# ---------------------------------------------------------------------------


def test_repair_prompt_includes_original_instruction_and_proposal_and_feedback():
    from agent.agent_repair import _build_repair_user_prompt

    prompt = _build_repair_user_prompt(INSTRUCTION, ORIGINAL_PROPOSAL, FEEDBACK, DISCOVERED_TOOLS)
    assert INSTRUCTION in prompt
    assert "delete_users" in prompt
    assert RULE_RECOVERY_PROOF in prompt
    assert "create_snapshot" in prompt  # exact suggested_repairs content, unchanged


def test_repair_prompt_passes_suggested_repairs_through_verbatim():
    from agent.agent_repair import _build_repair_user_prompt

    prompt = _build_repair_user_prompt(INSTRUCTION, ORIGINAL_PROPOSAL, FEEDBACK, DISCOVERED_TOOLS)
    assert json.dumps(FEEDBACK.suggested_repairs) in prompt


def test_repair_prompt_uses_actual_discovered_tools_not_hardcoded():
    from agent.agent_repair import _build_repair_user_prompt

    custom_tools = [
        DiscoveredTool(name="totally_different_tool", description="A hypothetical tool.", input_schema={"type": "object"})
    ]
    prompt = _build_repair_user_prompt(INSTRUCTION, ORIGINAL_PROPOSAL, FEEDBACK, custom_tools)
    assert "totally_different_tool" in prompt


def test_repair_system_prompt_does_not_instruct_a_specific_tool_or_environment():
    from agent.agent_repair import _REPAIR_SYSTEM_PROMPT

    forbidden_terms = [
        "choose test",
        'environment="test"',
        "select deactivate_users",
        "make the request pass",
        "chain of thought",
        "step by step reasoning",
    ]
    lowered = _REPAIR_SYSTEM_PROMPT.lower()
    for term in forbidden_terms:
        assert term.lower() not in lowered


def test_repair_system_prompt_states_exactly_one_revision_and_no_invented_proof():
    from agent.agent_repair import _REPAIR_SYSTEM_PROMPT

    lowered = _REPAIR_SYSTEM_PROMPT.lower()
    assert "exactly one" in lowered
    assert "proof" in lowered and ("no ability" in lowered or "do not invent" in lowered)


def test_repair_prompt_does_not_include_full_mcp_response_or_workflow_metadata():
    from agent.agent_repair import _build_repair_user_prompt

    prompt = _build_repair_user_prompt(INSTRUCTION, ORIGINAL_PROPOSAL, FEEDBACK, DISCOVERED_TOOLS)
    for forbidden in ("workflow_id", "audit_event_id", "selector_hash", "snapshot_path", "NEBIUS_API_KEY"):
        assert forbidden not in prompt


# ---------------------------------------------------------------------------
# Module boundary: never executes, never evaluates policy, never imports
# guarded_execute/Operations
# ---------------------------------------------------------------------------


def test_agent_repair_module_never_imports_guarded_execute_or_operations():
    import ast

    source_path = __file__.replace("tests/test_agent_repair.py", "agent/agent_repair.py")
    source = open(source_path).read()
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert "proofgate.core" not in imported
    assert "operations.actions" not in imported
    assert "guarded_execute" not in source
