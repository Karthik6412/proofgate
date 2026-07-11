"""Slice 11 hardening patch tests: canonicalize live intent output and make
scope_class/production_impact/irreversible deterministic (derived from the
authoritative ImpactEnvelope), never from the live Nebius response.

tests/conftest.py force-disables live Nebius by default; tests here that
need the live path re-enable it explicitly and inject a fake client.
"""

import json
import uuid

from agent.nebius_client import (
    extract_intent_live_or_fallback,
    extract_risk_features_live_or_fallback,
)
import agent.nebius_client as nebius_client_module
import proofgate.audit as audit_module
from operations.actions import preview_delete_users
from operations.database import reset_working_db
from operations.snapshots import create_snapshot
from proofgate.core import guarded_delete_users
from proofgate.models import ActionContext

BROAD_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


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
    def __init__(self, content=None, content_fn=None):
        self.content = content
        self.content_fn = content_fn
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        content = self.content_fn(kwargs) if self.content_fn else self.content
        return _FakeResponse(content)


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


def _corrected_impact():
    reset_working_db()
    return preview_delete_users(inactive_days=90, environment="test")


def _is_risk_prompt(kwargs) -> bool:
    return "risk feature" in kwargs["messages"][0]["content"].lower()


def _read_events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().splitlines() if line]


# ---------------------------------------------------------------------------
# Intent canonicalization
# ---------------------------------------------------------------------------


def test_live_action_type_delete_becomes_delete_users():
    payload = {
        "action_type": "delete",
        "target_resource": "users",
        "environment": "test",
        "inactivity_days": 90,
        "confidence": 0.95,
    }
    client = FakeNebiusClient(content=json.dumps(payload))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "nebius_live"
    assert outcome.value.action_type == "delete_users"


def test_live_target_resource_user_accounts_becomes_users():
    payload = {
        "action_type": "delete_users",
        "target_resource": "user accounts",
        "environment": "test",
        "inactivity_days": 90,
        "confidence": 0.95,
    }
    client = FakeNebiusClient(content=json.dumps(payload))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "nebius_live"
    assert outcome.value.target_resource == "users"


def test_already_canonical_intent_values_remain_unchanged():
    payload = {
        "action_type": "delete_users",
        "target_resource": "users",
        "environment": "test",
        "inactivity_days": 90,
        "confidence": 0.98,
    }
    client = FakeNebiusClient(content=json.dumps(payload))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "nebius_live"
    assert outcome.value.action_type == "delete_users"
    assert outcome.value.target_resource == "users"


def test_unsupported_action_type_falls_back_deterministically():
    payload = {
        "action_type": "purge_everything",
        "target_resource": "users",
        "environment": "test",
        "inactivity_days": 90,
        "confidence": 0.9,
    }
    client = FakeNebiusClient(content=json.dumps(payload))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"
    assert outcome.value.action_type == "delete_users"  # deterministic fallback's own result


def test_unsupported_resource_falls_back_deterministically():
    payload = {
        "action_type": "delete_users",
        "target_resource": "customer profiles",
        "environment": "test",
        "inactivity_days": 90,
        "confidence": 0.9,
    }
    client = FakeNebiusClient(content=json.dumps(payload))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"


def test_unsupported_environment_falls_back_deterministically():
    payload = {
        "action_type": "delete_users",
        "target_resource": "users",
        "environment": "staging",
        "inactivity_days": 90,
        "confidence": 0.9,
    }
    client = FakeNebiusClient(content=json.dumps(payload))
    outcome = extract_intent_live_or_fallback(BROAD_INSTRUCTION, client=client)

    assert outcome.mode == "deterministic_fallback"


# ---------------------------------------------------------------------------
# Deterministic risk fields (scope_class / production_impact / irreversible)
# ---------------------------------------------------------------------------


def _risk_payload(**overrides):
    payload = {
        "intent_mismatch": True,
        "missing_constraints": ["environment=test"],
        "scope_class": "both",  # deliberately wrong/loose
        "production_impact": False,  # deliberately wrong
        "irreversible": False,  # deliberately wrong
        "uncertainty": 0.03,
        "rationale": ["some rationale"],
    }
    payload.update(overrides)
    return payload


def test_broad_impact_count_always_produces_scope_class_mass():
    from agent.nebius_client import extract_intent

    client = FakeNebiusClient(content=json.dumps(_risk_payload()))
    intent = extract_intent(BROAD_INSTRUCTION)
    impact = _broad_impact()
    assert impact.estimated_count == 10073

    outcome = extract_risk_features_live_or_fallback(
        BROAD_INSTRUCTION, intent, {"inactive_days": 90, "environment": None}, impact, client=client
    )

    assert outcome.mode == "nebius_live"
    assert outcome.value.scope_class == "mass"


def test_corrected_impact_count_always_produces_scope_class_moderate():
    from agent.nebius_client import extract_intent

    client = FakeNebiusClient(content=json.dumps(_risk_payload()))
    intent = extract_intent(BROAD_INSTRUCTION)
    impact = _corrected_impact()
    assert impact.estimated_count == 92

    outcome = extract_risk_features_live_or_fallback(
        BROAD_INSTRUCTION, intent, {"inactive_days": 90, "environment": "test"}, impact, client=client
    )

    assert outcome.mode == "nebius_live"
    assert outcome.value.scope_class == "moderate"


def test_production_impact_is_derived_from_impact_envelope_not_model():
    from agent.nebius_client import extract_intent

    client = FakeNebiusClient(content=json.dumps(_risk_payload(production_impact=False)))
    intent = extract_intent(BROAD_INSTRUCTION)
    impact = _broad_impact()  # has 9,981 production rows

    outcome = extract_risk_features_live_or_fallback(
        BROAD_INSTRUCTION, intent, {"inactive_days": 90, "environment": None}, impact, client=client
    )

    assert outcome.value.production_impact is True  # authoritative, not the model's False


def test_irreversible_is_derived_from_impact_envelope_not_model():
    from agent.nebius_client import extract_intent

    client = FakeNebiusClient(content=json.dumps(_risk_payload(irreversible=False)))
    intent = extract_intent(BROAD_INSTRUCTION)
    impact = _broad_impact()  # hard_delete=True

    outcome = extract_risk_features_live_or_fallback(
        BROAD_INSTRUCTION, intent, {"inactive_days": 90, "environment": None}, impact, client=client
    )

    assert outcome.value.irreversible is True  # authoritative, not the model's False


def test_mocked_nebius_cannot_override_scope_class_production_impact_or_irreversible():
    from agent.nebius_client import extract_intent

    malicious_payload = _risk_payload(
        scope_class="both", production_impact=False, irreversible=False
    )
    client = FakeNebiusClient(content=json.dumps(malicious_payload))
    intent = extract_intent(BROAD_INSTRUCTION)
    impact = _broad_impact()

    outcome = extract_risk_features_live_or_fallback(
        BROAD_INSTRUCTION, intent, {"inactive_days": 90, "environment": None}, impact, client=client
    )

    assert outcome.mode == "nebius_live"
    assert outcome.value.scope_class == "mass"
    assert outcome.value.production_impact is True
    assert outcome.value.irreversible is True


def test_nebius_semantic_fields_still_pass_through():
    from agent.nebius_client import extract_intent

    payload = _risk_payload(
        intent_mismatch=True,
        missing_constraints=["environment=test"],
        uncertainty=0.07,
        rationale=["custom live rationale line"],
    )
    client = FakeNebiusClient(content=json.dumps(payload))
    intent = extract_intent(BROAD_INSTRUCTION)
    impact = _broad_impact()

    outcome = extract_risk_features_live_or_fallback(
        BROAD_INSTRUCTION, intent, {"inactive_days": 90, "environment": None}, impact, client=client
    )

    assert outcome.value.intent_mismatch is True
    assert outcome.value.missing_constraints == ["environment=test"]
    assert outcome.value.uncertainty == 0.07
    assert outcome.value.rationale == ["custom live rationale line"]


# ---------------------------------------------------------------------------
# End-to-end: verdicts unaffected by mocked live values
# ---------------------------------------------------------------------------


def test_broad_action_still_returns_block_with_hardened_live_risk(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")

    def content_fn(kwargs):
        if _is_risk_prompt(kwargs):
            return json.dumps(_risk_payload(production_impact=False, irreversible=False))
        return json.dumps(
            {
                "action_type": "delete",
                "target_resource": "user accounts",
                "environment": "test",
                "inactivity_days": 90,
                "confidence": 0.95,
            }
        )

    fake_client = FakeNebiusClient(content_fn=content_fn)
    monkeypatch.setattr(nebius_client_module, "_build_client", lambda: fake_client)

    reset_working_db()
    result = guarded_delete_users(
        action_context=_action_context(_fresh_workflow_id()),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    assert result.verdict == "BLOCK"

    events = _read_events(audit_module.DEFAULT_AUDIT_PATH)
    assert events[-1]["intent_constraints"]["action_type"] == "delete_users"
    assert events[-1]["intent_constraints"]["target_resource"] == "users"
    assert events[-1]["risk_features"]["scope_class"] == "mass"
    assert events[-1]["risk_features"]["production_impact"] is True
    assert events[-1]["risk_features"]["irreversible"] is True


def test_corrected_valid_proof_action_still_returns_allow_with_hardened_live_risk(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")

    def content_fn(kwargs):
        if _is_risk_prompt(kwargs):
            return json.dumps(
                _risk_payload(intent_mismatch=False, production_impact=False, irreversible=False)
            )
        return json.dumps(
            {
                "action_type": "delete_users",
                "target_resource": "users",
                "environment": "test",
                "inactivity_days": 90,
                "confidence": 0.98,
            }
        )

    fake_client = FakeNebiusClient(content_fn=content_fn)
    monkeypatch.setattr(nebius_client_module, "_build_client", lambda: fake_client)

    reset_working_db()
    proof = create_snapshot(
        resource="users", inactive_days=90, environment="test", max_affected_rows=92
    )

    result = guarded_delete_users(
        action_context=_action_context(_fresh_workflow_id()),
        inactive_days=90,
        environment="test",
        rollback_proof=proof,
    )

    assert result.verdict == "ALLOW"
    assert result.executed is True


# ---------------------------------------------------------------------------
# extraction_mode preview-failure edge case
# ---------------------------------------------------------------------------


def test_preview_failure_with_live_intent_reports_mixed(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")

    valid_intent_json = json.dumps(
        {
            "action_type": "delete_users",
            "target_resource": "users",
            "environment": "test",
            "inactivity_days": 90,
            "confidence": 0.98,
        }
    )
    fake_client = FakeNebiusClient(content=valid_intent_json)
    monkeypatch.setattr(nebius_client_module, "_build_client", lambda: fake_client)

    # Make preview_delete_users fail by pointing it at a nonexistent db path.
    monkeypatch.setattr(
        "proofgate.core.preview_delete_users",
        lambda inactive_days, environment: (_ for _ in ()).throw(RuntimeError("preview failed")),
    )

    result = guarded_delete_users(
        action_context=_action_context(_fresh_workflow_id()),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    assert result.verdict == "BLOCK"  # RULE_UNKNOWN_IMPACT

    events = _read_events(audit_module.DEFAULT_AUDIT_PATH)
    assert events[-1]["extraction_mode"] == "mixed"
    assert events[-1]["nebius_model"] is not None


def test_preview_failure_with_fallback_intent_reports_deterministic_fallback(monkeypatch):
    # conftest.py default: NEBIUS_LIVE_ENABLED=false, so intent uses fallback.
    monkeypatch.setattr(
        "proofgate.core.preview_delete_users",
        lambda inactive_days, environment: (_ for _ in ()).throw(RuntimeError("preview failed")),
    )

    result = guarded_delete_users(
        action_context=_action_context(_fresh_workflow_id()),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    assert result.verdict == "BLOCK"

    events = _read_events(audit_module.DEFAULT_AUDIT_PATH)
    assert events[-1]["extraction_mode"] == "deterministic_fallback"
    assert events[-1]["nebius_model"] is None


def test_never_reports_nebius_live_when_risk_never_ran(monkeypatch):
    monkeypatch.setenv("NEBIUS_LIVE_ENABLED", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "dummy-key")

    valid_intent_json = json.dumps(
        {
            "action_type": "delete_users",
            "target_resource": "users",
            "environment": "test",
            "inactivity_days": 90,
            "confidence": 0.98,
        }
    )
    fake_client = FakeNebiusClient(content=valid_intent_json)
    monkeypatch.setattr(nebius_client_module, "_build_client", lambda: fake_client)
    monkeypatch.setattr(
        "proofgate.core.preview_delete_users",
        lambda inactive_days, environment: (_ for _ in ()).throw(RuntimeError("preview failed")),
    )

    guarded_delete_users(
        action_context=_action_context(_fresh_workflow_id()),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    events = _read_events(audit_module.DEFAULT_AUDIT_PATH)
    assert events[-1]["extraction_mode"] != "nebius_live"


# ---------------------------------------------------------------------------
# No real network calls
# ---------------------------------------------------------------------------


def test_no_real_client_constructed_by_default_for_canonicalization_tests(monkeypatch):
    def _explode():
        raise AssertionError("must not construct a real Nebius client by default")

    monkeypatch.setattr(nebius_client_module, "_build_client", _explode)

    reset_working_db()
    result = guarded_delete_users(
        action_context=_action_context(_fresh_workflow_id()),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )
    assert result.verdict == "BLOCK"
