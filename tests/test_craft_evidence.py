"""Slice 12 tests: real CRAFT MCP integration for enterprise cohort evidence.

tests/conftest.py disables live CRAFT and redirects the evidence cache to a
per-test tmp_path by default, so no test here ever attempts real OAuth/
network access unless it explicitly injects a fake session via
session_factory (dependency injection), which bypasses the network
entirely regardless of the CRAFT_LIVE_ENABLED/CRAFT_PROJECT_ID env vars.
"""

import json
import uuid
from unittest import mock

from craft import config
from craft.evidence import (
    DEMO_COHORT_QUESTION,
    LABEL_CACHED,
    LABEL_LIVE,
    prepare_craft_evidence,
)
from operations.actions import count_rows, preview_delete_users
from operations.database import WORKING_DB_PATH, reset_working_db
from operations.snapshots import create_snapshot
from proofgate.budgets import get_craft_evidence, get_workflow_budget, reset_workflow_state
from proofgate.core import guarded_delete_users
from proofgate.models import ActionContext
from proofgate.policy import RULE_INTENT_BOUNDARY, RULE_WORKFLOW_BUDGET

BROAD_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


# ---------------------------------------------------------------------------
# Fake CRAFT MCP session
# ---------------------------------------------------------------------------


class _FakeTool:
    def __init__(self, name):
        self.name = name


class _FakeToolsResponse:
    def __init__(self, names):
        self.tools = [_FakeTool(n) for n in names]


class _FakeContentBlock:
    def __init__(self, text):
        self.text = text


class _FakeCallToolResult:
    def __init__(self, data=None, text=None, structured=None):
        if structured is not None:
            self.structuredContent = structured
            self.content = []
        else:
            self.structuredContent = None
            payload = text if text is not None else json.dumps(data)
            self.content = [_FakeContentBlock(payload)]


class FakeCraftSession:
    def __init__(self, tool_names, responses, list_tools_exception=None, call_tool_exceptions=None):
        self._tool_names = tool_names
        self._responses = responses
        self._list_tools_exception = list_tools_exception
        self._call_tool_exceptions = call_tool_exceptions or {}
        self.calls = []

    async def list_tools(self):
        if self._list_tools_exception is not None:
            raise self._list_tools_exception
        return _FakeToolsResponse(self._tool_names)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name in self._call_tool_exceptions:
            raise self._call_tool_exceptions[name]
        return self._responses.get(name)


def _default_tool_names():
    return {
        "list_data_connections",
        "list_databases",
        "get_schema",
        "generate_sql",
        "execute_query",
    }


def _default_responses(row_count=1, generated_sql="SELECT COUNT(*) AS cohort_count FROM ORDERS"):
    rows = [{"cohort_count": 42 + i} for i in range(row_count)]
    return {
        "list_data_connections": _FakeCallToolResult(
            data={
                "connections": [
                    {
                        "slug": "thelook-ecommerce-0f0a359c",
                        "name": "THELOOK_ECOMMERCE_0f0a359c",
                        "description": "THELOOK_ECOMMERCE",
                    }
                ]
            }
        ),
        "list_databases": _FakeCallToolResult(
            data={
                "databases": [
                    {
                        "type": "database",
                        "name": "THELOOK_ECOMMERCE",
                        "fully_qualified_name": "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE",
                    }
                ]
            }
        ),
        "get_schema": _FakeCallToolResult(
            data={
                "children": [
                    {
                        "type": "schema",
                        "name": "PUBLIC",
                        "fully_qualified_name": "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE.PUBLIC",
                    }
                ]
            }
        ),
        "generate_sql": _FakeCallToolResult(data={"sql": generated_sql}),
        "execute_query": _FakeCallToolResult(data={"rows": rows}),
    }


def _fake_session_factory(session):
    async def factory():
        return session

    return factory


def _fresh_workflow_id() -> str:
    return f"wf-{uuid.uuid4().hex[:12]}"


def _action_context(workflow_id: str) -> ActionContext:
    return ActionContext(
        workflow_id=workflow_id,
        requesting_user="demo-user",
        agent_id="demo-agent",
        original_instruction=BROAD_INSTRUCTION,
    )


# ---------------------------------------------------------------------------
# Live workflow success
# ---------------------------------------------------------------------------


def test_mocked_successful_workflow_produces_mode_live():
    session = FakeCraftSession(_default_tool_names(), _default_responses())
    outcome = prepare_craft_evidence(
        _fresh_workflow_id(), BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
    )

    assert outcome.mode == "live"
    assert outcome.evidence is not None


def test_live_evidence_uses_craft_enterprise_evidence_label():
    session = FakeCraftSession(_default_tool_names(), _default_responses())
    outcome = prepare_craft_evidence(
        _fresh_workflow_id(), BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
    )

    assert outcome.evidence.label == LABEL_LIVE


def test_authoritative_for_mutation_impact_is_false():
    session = FakeCraftSession(_default_tool_names(), _default_responses())
    outcome = prepare_craft_evidence(
        _fresh_workflow_id(), BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
    )

    assert outcome.evidence.authoritative_for_mutation_impact is False


def test_schema_discovery_occurs_before_sql_generation():
    session = FakeCraftSession(_default_tool_names(), _default_responses())
    prepare_craft_evidence(
        _fresh_workflow_id(), BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
    )

    call_names = [name for name, _args in session.calls]
    assert call_names.index("get_schema") < call_names.index("generate_sql")


def test_generated_sql_is_passed_to_execute_query():
    sql = "SELECT COUNT(*) AS cohort_count FROM ORDERS WHERE recency_days >= 90"
    session = FakeCraftSession(_default_tool_names(), _default_responses(generated_sql=sql))
    prepare_craft_evidence(
        _fresh_workflow_id(), BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
    )

    execute_calls = [args for name, args in session.calls if name == "execute_query"]
    assert len(execute_calls) == 1
    assert execute_calls[0]["sql"] == sql


def test_evidence_preview_is_bounded_and_sanitized():
    session = FakeCraftSession(_default_tool_names(), _default_responses(row_count=20))
    outcome = prepare_craft_evidence(
        _fresh_workflow_id(), BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
    )

    assert len(outcome.evidence.result_preview) <= 5
    for row in outcome.evidence.result_preview:
        assert isinstance(row, dict)


def test_successful_live_evidence_can_be_cached(tmp_path):
    cache_path = tmp_path / "cache.json"
    import os

    os.environ["CRAFT_CACHE_PATH"] = str(cache_path)
    try:
        session = FakeCraftSession(_default_tool_names(), _default_responses())
        prepare_craft_evidence(
            _fresh_workflow_id(), BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
        )
        assert cache_path.exists()
        saved = json.loads(cache_path.read_text())
        assert saved["label"] == LABEL_LIVE
    finally:
        del os.environ["CRAFT_CACHE_PATH"]


# ---------------------------------------------------------------------------
# Fallback behavior
# ---------------------------------------------------------------------------


def _seed_cache_with_live_evidence():
    session = FakeCraftSession(_default_tool_names(), _default_responses())
    prepare_craft_evidence(
        "seed-workflow", BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
    )


def test_disabled_live_mode_uses_cache_when_available(monkeypatch):
    _seed_cache_with_live_evidence()
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "false")

    outcome = prepare_craft_evidence(_fresh_workflow_id(), BROAD_INSTRUCTION)

    assert outcome.mode == "cached"
    assert outcome.evidence.label == LABEL_CACHED


def test_connection_failure_uses_cache_when_available():
    _seed_cache_with_live_evidence()
    failing_session = FakeCraftSession(
        _default_tool_names(),
        _default_responses(),
        list_tools_exception=ConnectionError("simulated connection failure"),
    )

    outcome = prepare_craft_evidence(
        _fresh_workflow_id(),
        BROAD_INSTRUCTION,
        session_factory=_fake_session_factory(failing_session),
    )

    assert outcome.mode == "cached"


def test_timeout_uses_cache_when_available():
    _seed_cache_with_live_evidence()
    failing_session = FakeCraftSession(
        _default_tool_names(),
        _default_responses(),
        list_tools_exception=TimeoutError("simulated timeout"),
    )

    outcome = prepare_craft_evidence(
        _fresh_workflow_id(),
        BROAD_INSTRUCTION,
        session_factory=_fake_session_factory(failing_session),
    )

    assert outcome.mode == "cached"


def test_malformed_tool_output_uses_cache_when_available():
    _seed_cache_with_live_evidence()
    responses = _default_responses()
    responses["generate_sql"] = _FakeCallToolResult(text="not json and no sql field")
    failing_session = FakeCraftSession(_default_tool_names(), responses)

    outcome = prepare_craft_evidence(
        _fresh_workflow_id(),
        BROAD_INSTRUCTION,
        session_factory=_fake_session_factory(failing_session),
    )

    assert outcome.mode == "cached"


def test_cached_evidence_is_relabeled_previously_retrieved():
    _seed_cache_with_live_evidence()
    monkeypatch_env_disabled = {"CRAFT_LIVE_ENABLED": "false"}
    import os

    original = os.environ.get("CRAFT_LIVE_ENABLED")
    os.environ.update(monkeypatch_env_disabled)
    try:
        outcome = prepare_craft_evidence(_fresh_workflow_id(), BROAD_INSTRUCTION)
        assert outcome.evidence.label == "Previously retrieved CRAFT evidence"
    finally:
        if original is not None:
            os.environ["CRAFT_LIVE_ENABLED"] = original


def test_cached_evidence_uses_mode_cached(monkeypatch):
    _seed_cache_with_live_evidence()
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "false")

    outcome = prepare_craft_evidence(_fresh_workflow_id(), BROAD_INSTRUCTION)

    assert outcome.evidence.mode == "cached"


def test_live_failure_without_cache_returns_unavailable_and_none():
    failing_session = FakeCraftSession(
        _default_tool_names(),
        _default_responses(),
        list_tools_exception=RuntimeError("simulated failure"),
    )

    outcome = prepare_craft_evidence(
        _fresh_workflow_id(),
        BROAD_INSTRUCTION,
        session_factory=_fake_session_factory(failing_session),
    )

    assert outcome.mode == "unavailable"
    assert outcome.evidence is None


# ---------------------------------------------------------------------------
# guarded_delete_users integration: never calls CRAFT, never crashes
# ---------------------------------------------------------------------------


def test_unavailable_craft_does_not_crash_guarded_execution():
    reset_working_db()
    result = guarded_delete_users(
        action_context=_action_context(_fresh_workflow_id()),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )
    assert result.verdict == "BLOCK"


def test_evidence_is_stored_and_isolated_by_workflow_id():
    session = FakeCraftSession(_default_tool_names(), _default_responses())
    wf_a = _fresh_workflow_id()
    wf_b = _fresh_workflow_id()

    prepare_craft_evidence(wf_a, BROAD_INSTRUCTION, session_factory=_fake_session_factory(session))

    assert get_craft_evidence(wf_a) is not None
    assert get_craft_evidence(wf_b) is None


def test_reset_workflow_state_clears_craft_evidence():
    session = FakeCraftSession(_default_tool_names(), _default_responses())
    workflow_id = _fresh_workflow_id()
    prepare_craft_evidence(
        workflow_id, BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
    )
    assert get_craft_evidence(workflow_id) is not None

    reset_workflow_state(workflow_id)

    assert get_craft_evidence(workflow_id) is None


def test_reset_working_db_does_not_clear_craft_evidence():
    session = FakeCraftSession(_default_tool_names(), _default_responses())
    workflow_id = _fresh_workflow_id()
    prepare_craft_evidence(
        workflow_id, BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
    )
    assert get_craft_evidence(workflow_id) is not None

    reset_working_db()

    assert get_craft_evidence(workflow_id) is not None


def test_guarded_delete_users_does_not_call_craft():
    with mock.patch("craft.client.run_craft_workflow") as mocked_workflow, mock.patch(
        "craft.evidence.prepare_craft_evidence"
    ) as mocked_prepare:
        reset_working_db()
        result = guarded_delete_users(
            action_context=_action_context(_fresh_workflow_id()),
            inactive_days=90,
            environment=None,
            rollback_proof=None,
        )
        assert result.verdict == "BLOCK"
        mocked_workflow.assert_not_called()
        mocked_prepare.assert_not_called()


def test_stored_evidence_appears_in_block_audit_event():
    import proofgate.audit as audit_module

    session = FakeCraftSession(_default_tool_names(), _default_responses())
    workflow_id = _fresh_workflow_id()
    prepare_craft_evidence(
        workflow_id, BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
    )

    reset_working_db()
    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )
    assert result.verdict == "BLOCK"

    events = [
        json.loads(line)
        for line in audit_module.DEFAULT_AUDIT_PATH.read_text().strip().splitlines()
    ]
    assert events[-1]["craft_evidence"] is not None
    assert events[-1]["craft_evidence"]["label"] == LABEL_LIVE
    assert events[-1]["craft_evidence"]["authoritative_for_mutation_impact"] is False


def test_stored_evidence_appears_in_allow_audit_event():
    import proofgate.audit as audit_module

    session = FakeCraftSession(_default_tool_names(), _default_responses())
    workflow_id = _fresh_workflow_id()
    prepare_craft_evidence(
        workflow_id, BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
    )

    reset_working_db()
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

    events = [
        json.loads(line)
        for line in audit_module.DEFAULT_AUDIT_PATH.read_text().strip().splitlines()
    ]
    assert events[-1]["craft_evidence"] is not None
    assert events[-1]["craft_evidence"]["label"] == LABEL_LIVE


def test_audit_preserves_cached_label_and_mode(monkeypatch):
    import proofgate.audit as audit_module

    _seed_cache_with_live_evidence()
    monkeypatch.setenv("CRAFT_LIVE_ENABLED", "false")
    workflow_id = _fresh_workflow_id()
    prepare_craft_evidence(workflow_id, BROAD_INSTRUCTION)

    reset_working_db()
    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )
    assert result.verdict == "BLOCK"

    events = [
        json.loads(line)
        for line in audit_module.DEFAULT_AUDIT_PATH.read_text().strip().splitlines()
    ]
    assert events[-1]["craft_evidence"]["label"] == LABEL_CACHED
    assert events[-1]["craft_evidence"]["mode"] == "cached"
    assert events[-1]["craft_evidence"]["authoritative_for_mutation_impact"] is False


def test_audit_and_cache_contain_no_tokens_or_secrets(tmp_path):
    import os

    import proofgate.audit as audit_module

    cache_path = tmp_path / "cache.json"
    os.environ["CRAFT_CACHE_PATH"] = str(cache_path)
    try:
        session = FakeCraftSession(_default_tool_names(), _default_responses())
        workflow_id = _fresh_workflow_id()
        prepare_craft_evidence(
            workflow_id, BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
        )

        reset_working_db()
        guarded_delete_users(
            action_context=_action_context(workflow_id),
            inactive_days=90,
            environment=None,
            rollback_proof=None,
        )

        cache_text = cache_path.read_text().lower()
        audit_text = audit_module.DEFAULT_AUDIT_PATH.read_text().lower()
        for suspicious in ("bearer ", "authorization", "client_secret", "x-project-id", "token"):
            assert suspicious not in cache_text
            assert suspicious not in audit_text
    finally:
        del os.environ["CRAFT_CACHE_PATH"]


# ---------------------------------------------------------------------------
# CRAFT cannot influence enforcement
# ---------------------------------------------------------------------------


def test_craft_evidence_cannot_override_rule_intent_boundary_or_workflow_budget():
    # CRAFT evidence falsely implies a "safe" cohort; real preview still has
    # 9,981 production users behind the broad call.
    safe_looking_responses = _default_responses()
    safe_looking_responses["execute_query"] = _FakeCallToolResult(
        data={"rows": [{"cohort_count": 0}]}
    )
    session = FakeCraftSession(_default_tool_names(), safe_looking_responses)
    workflow_id = _fresh_workflow_id()
    prepare_craft_evidence(
        workflow_id, BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
    )
    assert get_craft_evidence(workflow_id) is not None

    reset_working_db()
    result = guarded_delete_users(
        action_context=_action_context(workflow_id),
        inactive_days=90,
        environment=None,
        rollback_proof=None,
    )

    assert result.verdict == "BLOCK"
    triggered_ids = {r.rule_id for r in result.triggered_rules}
    assert RULE_INTENT_BOUNDARY in triggered_ids
    assert RULE_WORKFLOW_BUDGET in triggered_ids


def test_missing_craft_evidence_does_not_prevent_corrected_allow():
    # No prepare_craft_evidence call at all for this workflow_id.
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


def test_impact_envelope_counts_remain_operations_derived():
    session = FakeCraftSession(_default_tool_names(), _default_responses())
    workflow_id = _fresh_workflow_id()
    prepare_craft_evidence(
        workflow_id, BROAD_INSTRUCTION, session_factory=_fake_session_factory(session)
    )

    reset_working_db()
    impact = preview_delete_users(inactive_days=90, environment=None)

    assert impact.estimated_count == 10073
    assert impact.environment_counts == {"production": 9981, "test": 92}


# ---------------------------------------------------------------------------
# No real network access
# ---------------------------------------------------------------------------


def test_no_real_craft_client_used_by_default():
    def _explode(*_args, **_kwargs):
        raise AssertionError("must not run the real CRAFT workflow when live mode is disabled")

    with mock.patch("craft.evidence.run_craft_workflow", _explode):
        # conftest.py disables CRAFT_LIVE_ENABLED and clears CRAFT_PROJECT_ID
        # by default, so should_attempt_live is False and run_craft_workflow
        # (real or otherwise) is never reached.
        outcome = prepare_craft_evidence(_fresh_workflow_id(), BROAD_INSTRUCTION)
        assert outcome.mode == "unavailable"


# ---------------------------------------------------------------------------
# OAuth browser-consent timeout is separate from network/tool timeouts
# ---------------------------------------------------------------------------


def test_oauth_timeout_defaults_to_180_seconds(monkeypatch):
    monkeypatch.delenv("CRAFT_OAUTH_TIMEOUT_SECONDS", raising=False)
    assert config.oauth_timeout_seconds() == 180.0


def test_oauth_timeout_is_configurable_from_env(monkeypatch):
    monkeypatch.setenv("CRAFT_OAUTH_TIMEOUT_SECONDS", "45")
    assert config.oauth_timeout_seconds() == 45.0


def test_oauth_timeout_falls_back_to_default_on_invalid_value(monkeypatch):
    monkeypatch.setenv("CRAFT_OAUTH_TIMEOUT_SECONDS", "not-a-number")
    assert config.oauth_timeout_seconds() == 180.0


def test_network_tool_timeout_is_unaffected_by_oauth_timeout_env_var(monkeypatch):
    monkeypatch.setenv("CRAFT_OAUTH_TIMEOUT_SECONDS", "999")
    assert config.DEFAULT_TIMEOUT_SECONDS == 25.0


# ---------------------------------------------------------------------------
# ExceptionGroup unwrapping and sanitized, stage-aware error summaries
# ---------------------------------------------------------------------------


def test_unwrap_exception_group_returns_plain_exception_unchanged():
    from craft.client import unwrap_exception_group

    plain = ValueError("just a plain error")
    assert unwrap_exception_group(plain) is plain


def test_unwrap_exception_group_recursively_unwraps_nested_groups():
    from craft.client import unwrap_exception_group

    leaf = RuntimeError("the real underlying failure")
    inner_group = ExceptionGroup("inner", [leaf])
    outer_group = ExceptionGroup("unhandled errors in a TaskGroup", [inner_group])

    assert unwrap_exception_group(outer_group) is leaf


def test_sanitize_error_includes_stage_and_exception_type_for_clean_message():
    from craft.evidence import _sanitize_error

    exc = ValueError("clean, non-sensitive message")
    summary = _sanitize_error(exc, {"current_stage": "execute_query"})

    assert summary == "[execute_query] ValueError: clean, non-sensitive message"


def test_sanitize_error_redacts_sensitive_leaf_message():
    from craft.evidence import _sanitize_error

    exc = RuntimeError("failed with Authorization: Bearer sometoken123")
    summary = _sanitize_error(exc, {"current_stage": "generate_sql"})

    assert "sometoken123" not in summary
    assert "generate_sql" in summary
    assert "RuntimeError" in summary


def test_sanitize_error_unwraps_exception_group_before_reporting():
    from craft.evidence import _sanitize_error

    leaf = ValueError("the actual generate_sql failure")
    wrapped = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [leaf])

    summary = _sanitize_error(wrapped, {"current_stage": "generate_sql"})

    assert "unhandled errors in a TaskGroup" not in summary
    assert "the actual generate_sql failure" in summary
    assert "ValueError" in summary
    assert "generate_sql" in summary


def test_prepare_craft_evidence_error_summary_uses_unwrapped_leaf_and_stage():
    inner = ValueError("real underlying generate_sql failure detail")
    wrapped = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [inner])
    failing_session = FakeCraftSession(
        _default_tool_names(),
        _default_responses(),
        call_tool_exceptions={"generate_sql": wrapped},
    )

    outcome = prepare_craft_evidence(
        _fresh_workflow_id(),
        BROAD_INSTRUCTION,
        session_factory=_fake_session_factory(failing_session),
    )

    assert outcome.mode == "unavailable"  # no cache seeded in this test
    assert "unhandled errors in a TaskGroup" not in outcome.error_summary
    assert "generate_sql" in outcome.error_summary
    assert "ValueError" in outcome.error_summary
    assert "real underlying generate_sql failure detail" in outcome.error_summary


def test_prepare_craft_evidence_error_summary_never_leaks_secrets():
    inner = RuntimeError("request failed: Authorization: Bearer super-secret-token-xyz")
    wrapped = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [inner])
    failing_session = FakeCraftSession(
        _default_tool_names(),
        _default_responses(),
        call_tool_exceptions={"generate_sql": wrapped},
    )

    outcome = prepare_craft_evidence(
        _fresh_workflow_id(),
        BROAD_INSTRUCTION,
        session_factory=_fake_session_factory(failing_session),
    )

    assert "super-secret-token-xyz" not in outcome.error_summary
    assert "bearer" not in outcome.error_summary.lower()


# ---------------------------------------------------------------------------
# --debug diagnostics: discovered tool names and generate_sql input schema
# ---------------------------------------------------------------------------


def test_diagnostics_capture_discovered_tool_names():
    session = FakeCraftSession(_default_tool_names(), _default_responses())
    diagnostics: dict = {}

    prepare_craft_evidence(
        _fresh_workflow_id(),
        BROAD_INSTRUCTION,
        session_factory=_fake_session_factory(session),
        diagnostics=diagnostics,
    )

    assert diagnostics["discovered_tool_names"] == sorted(_default_tool_names())


def test_diagnostics_capture_generate_sql_input_schema():
    class _SchemaTool:
        def __init__(self, name, schema):
            self.name = name
            self.inputSchema = schema

    class _CustomToolsResponse:
        def __init__(self, tools):
            self.tools = tools

    class _CustomSession(FakeCraftSession):
        async def list_tools(self):
            return _CustomToolsResponse(
                [
                    _SchemaTool("list_data_connections", None),
                    _SchemaTool("list_databases", None),
                    _SchemaTool("get_schema", None),
                    _SchemaTool(
                        "generate_sql",
                        {"type": "object", "properties": {"question": {"type": "string"}}},
                    ),
                    _SchemaTool("execute_query", None),
                ]
            )

    session = _CustomSession(_default_tool_names(), _default_responses())
    diagnostics: dict = {}

    prepare_craft_evidence(
        _fresh_workflow_id(),
        BROAD_INSTRUCTION,
        session_factory=_fake_session_factory(session),
        diagnostics=diagnostics,
    )

    assert diagnostics["generate_sql_input_schema"] == {
        "type": "object",
        "properties": {"question": {"type": "string"}},
    }
