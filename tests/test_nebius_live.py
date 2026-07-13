"""Slice 11 tests: live Nebius Token Factory integration (additive).

tests/conftest.py force-disables live Nebius (NEBIUS_LIVE_ENABLED=false,
NEBIUS_API_KEY unset) for every test by default, so no test here ever
attempts a real network call unless it explicitly re-enables the env vars
AND injects a fake client (either via the client= parameter on the
nebius_client functions directly, or by monkeypatching
agent.nebius_client._build_client for tests that go through
guarded_delete_users, which has no test-only seam).
"""

import json
import uuid

import pytest

import agent.nebius_client as nebius_client_module
import proofgate.audit as audit_module
from agent.nebius_client import (
    extract_intent,
    extract_intent_live_or_fallback,
    extract_risk_features_live_or_fallback,
    model as configured_model,
)
from operations.database import reset_working_db
from operations.actions import preview_delete_users
from operations.snapshots import create_snapshot
from proofgate.core import guarded_delete_users
from proofgate.models import ActionContext
from proofgate.policy import RULE_INTENT_BOUNDARY, RULE_WORKFLOW_BUDGET

BROAD_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible client
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class FakeNebiusClient:
    """Minimal stand-in for the OpenAI-compatible client's
    .chat.completions.create(...) surface used by agent.nebius_client."""

    def __init__(self, content=None, exception=None, content_fn=None):
        self.content = content
        self.exception = exception
        self.content_fn = content_fn
        self.calls = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exception is not None:
            raise self.exception
        content = self.content_fn(kwargs) if self.content_fn else self.content
        return _FakeResponse(content)


VALID_INTENT_PAYLOAD = {
    "action_type": "delete_users",
    "target_resource": "users",
    "environment": "test",
    "inactivity_days": 90,
    "confidence": 0.98,
}
VALID_INTENT_JSON = json.dumps(VALID_INTENT_PAYLOAD)

VALID_RISK_PAYLOAD = {
    "intent_mismatch": True,
    "missing_constraints": ["environment=test"],
    "scope_class": "mass",
    "production_impact": True,
    "irreversible": True,
    "uncertainty": 0.03,
    "rationale": ["The requested test constraint is missing."],
}
VALID_RISK_JSON = json.dumps(VALID_RISK_PAYLOAD)


def _fresh_workflow_id() -> str:
    return f"wf-{uuid.uuid4().hex[:12]}"


def _action_context(workflow_id: str) -> ActionContext:
    return ActionContext(
        workflow_id=workflow_id,
        requesting_user="demo-user",
        agent_id="demo-agent",
        original_instruction=BROAD_INSTRUCTION,
    )


def _broad_impact():
    reset_working_db()
    return preview_delete_users(inactive_days=90, environment=None)


def _read_events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().splitlines() if line]


def _is_risk_prompt(kwargs) -> bool:
    return "risk feature" in kwargs["messages"][0]["content"].lower()


# ---------------------------------------------------------------------------
# Direct unit tests of the live-or-fallback extractors (client injected)
# ---------------------------------------------------------------------------


def test_valid_mocked_live_intent_json_is_accepted():
    client = FakeNebiusClient(content=VALID_INTENT_JSON)
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "nebius_live"
    assert outcome.value.action_type == "delete_users"
    assert outcome.value.environment == "test"
    assert outcome.value.inactivity_days == 90


def test_valid_mocked_live_risk_json_is_accepted():
    client = FakeNebiusClient(content=VALID_RISK_JSON)
    intent = extract_intent(BROAD_INSTRUCTION)
    impact = _broad_impact()

    outcome = extract_risk_features_live_or_fallback(
        BROAD_INSTRUCTION, intent, {"inactive_days": 90, "environment": None}, impact, client=client
    )

    assert outcome.mode == "nebius_live"
    assert outcome.value.scope_class == "mass"
    assert outcome.value.production_impact is True


def test_successful_live_extraction_returns_mode_nebius_live():
    client = FakeNebiusClient(content=VALID_INTENT_JSON)
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "nebius_live"
    assert outcome.model == configured_model()


def test_disabled_live_mode_uses_deterministic_fallback(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "false")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")

    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION)

    assert outcome.mode == "deterministic_fallback"
    assert outcome.model is None


def test_missing_api_key_uses_deterministic_fallback(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)

    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION)

    assert outcome.mode == "deterministic_fallback"


def test_network_exception_uses_deterministic_fallback():
    client = FakeNebiusClient(exception=ConnectionError("simulated network failure"))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"
    assert outcome.value.environment == "test"


def test_timeout_uses_deterministic_fallback():
    client = FakeNebiusClient(exception=TimeoutError("simulated timeout"))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"


def test_malformed_json_uses_deterministic_fallback():
    client = FakeNebiusClient(content="{not valid json at all")
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"


def test_prose_surrounding_json_uses_deterministic_fallback():
    client = FakeNebiusClient(
        content=f"Sure! Here is the JSON you asked for:\n{VALID_INTENT_JSON}\nHope that helps!"
    )
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"


def test_unknown_fields_use_deterministic_fallback():
    payload = dict(VALID_INTENT_PAYLOAD, extra_unexpected_field="surprise")
    client = FakeNebiusClient(content=json.dumps(payload))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"


def test_verdict_field_is_rejected():
    payload = dict(VALID_INTENT_PAYLOAD, verdict="ALLOW")
    client = FakeNebiusClient(content=json.dumps(payload))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"


@pytest.mark.parametrize("key", ["decision", "authorization"])
def test_decision_and_authorization_fields_are_rejected(key):
    payload = dict(VALID_INTENT_PAYLOAD, **{key: "approved"})
    client = FakeNebiusClient(content=json.dumps(payload))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"


@pytest.mark.parametrize("key", ["allow", "block"])
def test_allow_block_style_output_is_rejected(key):
    payload = dict(VALID_INTENT_PAYLOAD, **{key: True})
    client = FakeNebiusClient(content=json.dumps(payload))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"


def test_incomplete_intent_output_uses_deterministic_fallback():
    payload = dict(VALID_INTENT_PAYLOAD)
    del payload["confidence"]
    client = FakeNebiusClient(content=json.dumps(payload))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"


def test_incomplete_risk_output_uses_deterministic_fallback():
    payload = dict(VALID_RISK_PAYLOAD)
    del payload["uncertainty"]
    client = FakeNebiusClient(content=json.dumps(payload))
    intent = extract_intent(BROAD_INSTRUCTION)
    impact = _broad_impact()

    outcome = extract_risk_features_live_or_fallback(
        BROAD_INSTRUCTION, intent, {"inactive_days": 90, "environment": None}, impact, client=client
    )

    assert outcome.mode == "deterministic_fallback"


def test_fallback_still_extracts_environment_test_and_90_days():
    client = FakeNebiusClient(exception=RuntimeError("simulated failure"))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"
    assert outcome.value.environment == "test"
    assert outcome.value.inactivity_days == 90


def test_live_risk_output_cannot_supply_or_override_affected_row_counts():
    payload = dict(VALID_RISK_PAYLOAD, affected_count=5)
    client = FakeNebiusClient(content=json.dumps(payload))
    intent = extract_intent(BROAD_INSTRUCTION)
    impact = _broad_impact()

    outcome = extract_risk_features_live_or_fallback(
        BROAD_INSTRUCTION, intent, {"inactive_days": 90, "environment": None}, impact, client=client
    )

    # The unknown "affected_count" field is rejected outright (extra="forbid"),
    # so the call falls back rather than accepting a model-supplied count.
    assert outcome.mode == "deterministic_fallback"
    assert not hasattr(outcome.value, "affected_count")


# ---------------------------------------------------------------------------
# Deterministic enforcement survives a malicious/mocked live risk response
# ---------------------------------------------------------------------------


def test_broad_call_blocked_despite_malicious_live_risk_and_rules_derive_authoritatively(
    monkeypatch,
):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")

    malicious_risk_payload = {
        "intent_mismatch": False,
        "missing_constraints": [],
        "scope_class": "small",
        "production_impact": False,
        "irreversible": True,
        "uncertainty": 0.0,
        "rationale": [],
    }

    def content_fn(kwargs):
        if _is_risk_prompt(kwargs):
            return json.dumps(malicious_risk_payload)
        return VALID_INTENT_JSON  # correct intent: environment=test, 90 days

    fake_client = FakeNebiusClient(content_fn=content_fn)
    monkeypatch.setattr(nebius_client_module, "_build_client", lambda: fake_client)

    reset_working_db()
    workflow_id = _fresh_workflow_id()
    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    assert result.verdict == "BLOCK"
    triggered_ids = {r.rule_id for r in result.triggered_rules}
    # RULE_INTENT_BOUNDARY and RULE_WORKFLOW_BUDGET derive from intent and
    # ImpactEnvelope directly -- never from RiskFeatures -- so they still
    # fire even though the mocked live risk response falsely claims no
    # mismatch and no production impact.
    assert RULE_INTENT_BOUNDARY in triggered_ids
    assert RULE_WORKFLOW_BUDGET in triggered_ids


def test_corrected_valid_proof_call_still_reaches_allow_with_live_nebius_enabled(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")

    correct_risk_payload = {
        "intent_mismatch": False,
        "missing_constraints": [],
        "scope_class": "small",
        "production_impact": False,
        "irreversible": True,
        "uncertainty": 0.03,
        "rationale": [],
    }

    def content_fn(kwargs):
        if _is_risk_prompt(kwargs):
            return json.dumps(correct_risk_payload)
        return VALID_INTENT_JSON

    fake_client = FakeNebiusClient(content_fn=content_fn)
    monkeypatch.setattr(nebius_client_module, "_build_client", lambda: fake_client)

    reset_working_db()
    workflow_id = _fresh_workflow_id()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )

    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )

    assert result.verdict == "ALLOW"
    assert result.executed is True
    assert result.triggered_rules == []


# ---------------------------------------------------------------------------
# Combined extraction_mode / nebius_model audit metadata
# ---------------------------------------------------------------------------


def test_extraction_mode_is_nebius_live_when_both_extractions_succeed(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")
    monkeypatch.setenv("NEBIUS_MODEL", "test-model-both-live")

    def content_fn(kwargs):
        if _is_risk_prompt(kwargs):
            return VALID_RISK_JSON
        return VALID_INTENT_JSON

    fake_client = FakeNebiusClient(content_fn=content_fn)
    monkeypatch.setattr(nebius_client_module, "_build_client", lambda: fake_client)

    reset_working_db()
    workflow_id = _fresh_workflow_id()
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    events = _read_events(audit_module.DEFAULT_AUDIT_PATH)
    assert events[-1]["extraction_mode"] == "nebius_live"
    assert events[-1]["nebius_model"] == "test-model-both-live"


def test_extraction_mode_is_mixed_when_exactly_one_extraction_succeeds(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")
    monkeypatch.setenv("NEBIUS_MODEL", "test-model-mixed")

    def content_fn(kwargs):
        if _is_risk_prompt(kwargs):
            return VALID_RISK_JSON  # risk succeeds live
        return "not valid json {{{"  # intent fails -> falls back

    fake_client = FakeNebiusClient(content_fn=content_fn)
    monkeypatch.setattr(nebius_client_module, "_build_client", lambda: fake_client)

    reset_working_db()
    workflow_id = _fresh_workflow_id()
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    events = _read_events(audit_module.DEFAULT_AUDIT_PATH)
    assert events[-1]["extraction_mode"] == "mixed"
    assert events[-1]["nebius_model"] == "test-model-mixed"


def test_extraction_mode_is_deterministic_fallback_when_neither_succeeds():
    # conftest.py already forces NEBIUS_LIVE_ENABLED=false by default.
    reset_working_db()
    workflow_id = _fresh_workflow_id()
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    events = _read_events(audit_module.DEFAULT_AUDIT_PATH)
    assert events[-1]["extraction_mode"] == "deterministic_fallback"
    assert events[-1]["nebius_model"] is None


def test_audit_contains_extraction_metadata_and_validated_structured_outputs(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")

    def content_fn(kwargs):
        if _is_risk_prompt(kwargs):
            return VALID_RISK_JSON
        return VALID_INTENT_JSON

    fake_client = FakeNebiusClient(content_fn=content_fn)
    monkeypatch.setattr(nebius_client_module, "_build_client", lambda: fake_client)

    reset_working_db()
    workflow_id = _fresh_workflow_id()
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    event = _read_events(audit_module.DEFAULT_AUDIT_PATH)[-1]
    assert event["intent_constraints"]["environment"] == "test"
    assert event["intent_constraints"]["inactivity_days"] == 90
    assert event["risk_features"]["scope_class"] == "mass"
    assert event["extraction_mode"] == "nebius_live"
    assert event["nebius_model"] is not None


def test_audit_contains_no_api_key_or_secret(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    secret_value = "sk-super-secret-nebius-key-should-never-leak"
    monkeypatch.setenv("NEBIUS_API_KEY", secret_value)

    fake_client = FakeNebiusClient(content=VALID_INTENT_JSON)
    monkeypatch.setattr(nebius_client_module, "_build_client", lambda: fake_client)

    reset_working_db()
    workflow_id = _fresh_workflow_id()
    guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    raw_text = audit_module.DEFAULT_AUDIT_PATH.read_text()
    assert secret_value not in raw_text
    lowered = raw_text.lower()
    for suspicious in ("api_key", "apikey", "bearer ", "authorization"):
        assert suspicious not in lowered


# ---------------------------------------------------------------------------
# No real network access under default (or accidental) test conditions
# ---------------------------------------------------------------------------


def test_no_real_client_constructed_when_live_disabled_by_default(monkeypatch):
    def _explode():
        raise AssertionError(
            "must not construct a real Nebius client when live mode is disabled"
        )

    monkeypatch.setattr(nebius_client_module, "_build_client", _explode)

    reset_working_db()
    workflow_id = _fresh_workflow_id()
    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    # Completed normally without ever reaching _build_client -- conftest.py's
    # default NEBIUS_LIVE_ENABLED=false / no API key kept the live path shut.
    assert result.verdict == "BLOCK"


# ---------------------------------------------------------------------------
# Slice 20: degraded_reason -- honestly distinguishes *why* mode fell back
# to deterministic_fallback (missing_configuration / upstream_failure /
# invalid_response), and a live success carries no degraded_reason at all.
# ---------------------------------------------------------------------------


def test_live_success_has_no_degraded_reason():
    client = FakeNebiusClient(content=VALID_INTENT_JSON)
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "nebius_live"
    assert outcome.degraded_reason is None


def test_missing_configuration_degraded_reason(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "false")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")

    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION)

    assert outcome.mode == "deterministic_fallback"
    assert outcome.degraded_reason == "missing_configuration"


def test_upstream_failure_degraded_reason_for_network_exception():
    client = FakeNebiusClient(exception=ConnectionError("simulated network failure"))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"
    assert outcome.degraded_reason == "upstream_failure"


def test_upstream_failure_degraded_reason_for_timeout():
    client = FakeNebiusClient(exception=TimeoutError("simulated timeout"))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.degraded_reason == "upstream_failure"


def test_invalid_response_degraded_reason_for_malformed_json():
    client = FakeNebiusClient(content="{not valid json at all")
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.degraded_reason == "invalid_response"


def test_invalid_response_degraded_reason_for_unsupported_environment():
    payload = dict(VALID_INTENT_PAYLOAD, environment="production_and_test")
    client = FakeNebiusClient(content=json.dumps(payload))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"
    assert outcome.degraded_reason == "invalid_response"


def test_risk_extraction_degraded_reason_missing_configuration(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "false")
    intent = extract_intent(BROAD_INSTRUCTION)
    impact = _broad_impact()

    outcome = extract_risk_features_live_or_fallback(
        BROAD_INSTRUCTION, intent, {"inactive_days": 90, "environment": None}, impact
    )

    assert outcome.mode == "deterministic_fallback"
    assert outcome.degraded_reason == "missing_configuration"


def test_degradation_never_writes_to_stdout(capsys):
    # agent.nebius_client configures its logger's stream handler once at
    # module-import time, so pytest's capsys (which swaps sys.stderr only
    # for the duration of one test) cannot observe writes to that already-
    # bound stream object -- caplog is used below to verify the log
    # *content* instead. This assertion still meaningfully proves stdout
    # itself was never touched, which is the actual stdio-hygiene concern.
    client = FakeNebiusClient(exception=ConnectionError("simulated network failure"))
    extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert capsys.readouterr().out == ""


def test_degradation_is_logged_with_reason_and_message(caplog):
    with caplog.at_level("WARNING", logger="agent.nebius_client"):
        client = FakeNebiusClient(exception=ConnectionError("simulated network failure"))
        extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert "upstream_failure" in caplog.text
    assert "simulated network failure" in caplog.text


def test_degradation_log_redacts_secret_looking_messages(caplog):
    with caplog.at_level("WARNING", logger="agent.nebius_client"):
        client = FakeNebiusClient(
            exception=RuntimeError("Authorization: Bearer sk-supersecrettoken123 rejected")
        )
        extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert "sk-supersecrettoken123" not in caplog.text
    assert "withheld" in caplog.text.lower()
