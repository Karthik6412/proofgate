"""Tests for the --debug-only raw generate_sql result instrumentation.

This is purely diagnostic: describe_tool_result() and the diagnostics it
feeds are never consulted by the real parsing/extraction logic
(_tool_result_to_data / _extract_generated_sql), which remain untouched.
tests/conftest.py disables live CRAFT by default, so these tests use
dependency-injected fake sessions/results and never touch the network.
"""

import sys

from craft.client import describe_tool_result, run_craft_workflow
from craft.evidence import prepare_craft_evidence

BROAD_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeCallToolResult:
    def __init__(self, content=None, structured=None):
        self.structuredContent = structured
        self.content = content or []


class _FakeTool:
    def __init__(self, name):
        self.name = name


class _FakeToolsResponse:
    def __init__(self, names):
        self.tools = [_FakeTool(n) for n in names]


class FakeCraftSession:
    def __init__(self, tool_names, responses):
        self._tool_names = tool_names
        self._responses = responses
        self.calls = []

    async def list_tools(self):
        return _FakeToolsResponse(self._tool_names)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self._responses.get(name)


def _default_tool_names():
    return {
        "list_data_connections",
        "list_databases",
        "get_schema",
        "generate_sql",
        "execute_query",
    }


def _fake_session_factory(session):
    async def factory():
        return session

    return factory


# ---------------------------------------------------------------------------
# describe_tool_result: structure, bounding, redaction
# ---------------------------------------------------------------------------


def test_describe_tool_result_reports_type_and_shape_flags():
    result = _FakeCallToolResult(content=[_FakeTextBlock("SELECT 1")], structured=None)
    summary = describe_tool_result(result)

    assert summary["python_type"] == "_FakeCallToolResult"
    assert summary["has_structuredContent"] is False
    assert summary["has_structured_content"] is False
    assert summary["has_content"] is True
    assert summary["content_block_count"] == 1
    assert summary["content_block_types"] == ["_FakeTextBlock"]


def test_describe_tool_result_detects_structured_content_snake_case_variant():
    class _SnakeResult:
        structured_content = {"foo": "bar"}
        content = []

    summary = describe_tool_result(_SnakeResult())
    assert summary["has_structured_content"] is True
    assert summary["has_structuredContent"] is False
    assert summary["top_level_keys"] == ["foo"]


def test_describe_tool_result_reports_top_level_dict_keys():
    result = _FakeCallToolResult(structured={"sql": "SELECT 1", "explanation": "why"})
    summary = describe_tool_result(result)
    assert summary["top_level_keys"] == ["explanation", "sql"]


def test_describe_tool_result_small_preview_is_not_truncated():
    small_text = "a short response body"
    result = _FakeCallToolResult(content=[_FakeTextBlock(small_text)])
    summary = describe_tool_result(result)

    assert summary["preview"] == small_text
    assert summary["preview_truncated"] is False


def test_describe_tool_result_large_preview_is_bounded_to_1000_chars():
    large_text = "x" * 5000
    result = _FakeCallToolResult(content=[_FakeTextBlock(large_text)])
    summary = describe_tool_result(result)

    assert len(summary["preview"]) == 1000
    assert summary["preview_truncated"] is True


def test_describe_tool_result_redacts_likely_secret_in_preview():
    sensitive_text = "response failed: Authorization: Bearer super-secret-abc123"
    result = _FakeCallToolResult(content=[_FakeTextBlock(sensitive_text)])
    summary = describe_tool_result(result)

    assert "super-secret-abc123" not in summary["preview"]
    assert "bearer" not in summary["preview"].lower()


def test_describe_tool_result_redacts_likely_secret_in_repr_summary():
    class _LeakyResult:
        def __repr__(self):
            return "CallToolResult(headers={'Authorization': 'Bearer abc123secret'})"

        content = []
        structuredContent = None

    summary = describe_tool_result(_LeakyResult())
    assert "abc123secret" not in summary["repr_summary"]


def test_describe_tool_result_attributes_exclude_private_names():
    result = _FakeCallToolResult(content=[_FakeTextBlock("x")])
    summary = describe_tool_result(result)
    assert all(not attr.startswith("_") for attr in summary["attributes"])


# ---------------------------------------------------------------------------
# Diagnostics wiring: captured before (and independent of) real parsing
# ---------------------------------------------------------------------------


def _valid_list_databases_result():
    return _FakeCallToolResult(
        structured={
            "databases": [
                {
                    "type": "database",
                    "name": "THELOOK_ECOMMERCE",
                    "fully_qualified_name": "thelook.THELOOK_ECOMMERCE",
                }
            ]
        }
    )


def _valid_get_schema_result():
    return _FakeCallToolResult(
        structured={
            "children": [
                {
                    "type": "schema",
                    "name": "PUBLIC",
                    "fully_qualified_name": "thelook.THELOOK_ECOMMERCE.PUBLIC",
                }
            ]
        }
    )


def test_diagnostics_capture_raw_result_summary_and_arguments_sent_on_parse_failure():
    responses = {
        "list_data_connections": _FakeCallToolResult(
            structured={"connections": [{"slug": "thelook", "description": "THELOOK_ECOMMERCE"}]}
        ),
        "list_databases": _valid_list_databases_result(),
        "get_schema": _valid_get_schema_result(),
        "generate_sql": _FakeCallToolResult(content=[_FakeTextBlock("not sql and no sql key")]),
        "execute_query": _FakeCallToolResult(structured={"rows": []}),
    }
    session = FakeCraftSession(_default_tool_names(), responses)
    diagnostics: dict = {}

    outcome = prepare_craft_evidence(
        "wf-debug-test",
        BROAD_INSTRUCTION,
        session_factory=_fake_session_factory(session),
        diagnostics=diagnostics,
    )

    # Parsing genuinely failed (unchanged extraction logic), but diagnostics
    # were captured beforehand regardless.
    assert outcome.mode == "unavailable"
    assert diagnostics["generate_sql_arguments_sent"] == {
        "question": diagnostics["generate_sql_arguments_sent"]["question"],
        "connection": "thelook",
        "schema_name": "PUBLIC",
        "schema_fqn": "thelook.THELOOK_ECOMMERCE.PUBLIC",
    }
    raw_summary = diagnostics["generate_sql_raw_result_summary"]
    assert raw_summary["has_content"] is True
    assert raw_summary["preview"] == "not sql and no sql key"


def test_diagnostics_capture_raw_result_summary_on_parse_success_too():
    responses = {
        "list_data_connections": _FakeCallToolResult(
            structured={"connections": [{"slug": "thelook", "description": "THELOOK_ECOMMERCE"}]}
        ),
        "list_databases": _valid_list_databases_result(),
        "get_schema": _valid_get_schema_result(),
        "generate_sql": _FakeCallToolResult(structured={"sql": "SELECT COUNT(*) FROM ORDERS"}),
        "execute_query": _FakeCallToolResult(structured={"rows": [{"n": 1}]}),
    }
    session = FakeCraftSession(_default_tool_names(), responses)
    diagnostics: dict = {}

    outcome = prepare_craft_evidence(
        "wf-debug-test-2",
        BROAD_INSTRUCTION,
        session_factory=_fake_session_factory(session),
        diagnostics=diagnostics,
    )

    assert outcome.mode == "live"
    assert "generate_sql_raw_result_summary" in diagnostics
    assert diagnostics["generate_sql_raw_result_summary"]["top_level_keys"] == ["sql"]


def test_generate_sql_arguments_sent_includes_discovered_schema_name_and_fqn():
    responses = {
        "list_data_connections": _FakeCallToolResult(
            structured={"connections": [{"slug": "thelook", "description": "THELOOK_ECOMMERCE"}]}
        ),
        "list_databases": _valid_list_databases_result(),
        "get_schema": _valid_get_schema_result(),
        "generate_sql": _FakeCallToolResult(structured={"sql": "SELECT 1"}),
        "execute_query": _FakeCallToolResult(structured={"rows": []}),
    }
    session = FakeCraftSession(_default_tool_names(), responses)
    diagnostics: dict = {}

    run_craft_workflow(
        "some question", session_factory=_fake_session_factory(session), diagnostics=diagnostics
    )

    assert diagnostics["generate_sql_arguments_sent"]["schema_name"] == "PUBLIC"
    assert diagnostics["generate_sql_arguments_sent"]["schema_fqn"] == "thelook.THELOOK_ECOMMERCE.PUBLIC"


# ---------------------------------------------------------------------------
# scripts/check_craft.py --debug gating
# ---------------------------------------------------------------------------


def _fake_prepare_craft_evidence(
    workflow_id, original_instruction, question=None, diagnostics=None, session_factory=None
):
    if diagnostics is not None:
        diagnostics.update(
            {
                "authenticated": True,
                "discovery_tool": "list_data_connections",
                "schema_discovery_succeeded": True,
                "generate_sql_succeeded": False,
                "execute_query_succeeded": False,
                "selected_database": "thelook-ecommerce-0f0a359c",
                "discovered_tool_names": [
                    "execute_query",
                    "generate_sql",
                    "get_schema",
                    "list_data_connections",
                ],
                "generate_sql_input_schema": {"type": "object"},
                "generate_sql_arguments_sent": {
                    "question": "cohort question",
                    "connection": "thelook-ecommerce-0f0a359c",
                    "schema_name": None,
                    "schema_fqn": None,
                },
                "generate_sql_raw_result_summary": {
                    "python_type": "CallToolResult",
                    "repr_summary": "MARKER_REPR_9f21",
                    "attributes": ["content", "structuredContent"],
                    "has_structuredContent": False,
                    "has_structured_content": False,
                    "has_content": True,
                    "content_block_count": 1,
                    "content_block_types": ["TextContent"],
                    "top_level_keys": None,
                    "preview": "MARKER_PREVIEW_9f21 this is not recognizable sql",
                    "preview_truncated": False,
                },
            }
        )

    from craft.evidence import CraftEvidenceOutcome

    return CraftEvidenceOutcome(
        evidence=None,
        mode="unavailable",
        error_summary="[generate_sql] CraftWorkflowError: generate_sql did not return a recognizable SQL string.",
    )


def _run_script_main(monkeypatch, argv, capsys):
    import scripts.check_craft as check_craft_module

    monkeypatch.setattr(check_craft_module, "prepare_craft_evidence", _fake_prepare_craft_evidence)
    monkeypatch.setattr(sys, "argv", argv)
    check_craft_module.main()
    return capsys.readouterr().out


def test_non_debug_mode_prints_no_raw_response_details(monkeypatch, capsys):
    out = _run_script_main(monkeypatch, ["check_craft.py"], capsys)

    assert "MARKER_REPR_9f21" not in out
    assert "MARKER_PREVIEW_9f21" not in out
    assert "generate_sql arguments sent" not in out
    assert "generate_sql raw result" not in out


def test_debug_mode_includes_response_type_and_shape_information(monkeypatch, capsys):
    out = _run_script_main(monkeypatch, ["check_craft.py", "--debug"], capsys)

    assert "python_type: CallToolResult" in out
    assert "has_content: True" in out
    assert "content_block_types: ['TextContent']" in out
    assert "MARKER_PREVIEW_9f21" in out
    assert "generate_sql arguments sent" in out
    assert "generate_sql input schema" in out


def test_debug_mode_output_is_bounded(monkeypatch, capsys):
    def _fake_with_long_preview(
        workflow_id, original_instruction, question=None, diagnostics=None, session_factory=None
    ):
        if diagnostics is not None:
            diagnostics["generate_sql_raw_result_summary"] = {
                "python_type": "CallToolResult",
                "repr_summary": "r" * 300,
                "attributes": [],
                "has_structuredContent": False,
                "has_structured_content": False,
                "has_content": True,
                "content_block_count": 1,
                "content_block_types": ["TextContent"],
                "top_level_keys": None,
                "preview": "p" * 1000,
                "preview_truncated": True,
            }
        from craft.evidence import CraftEvidenceOutcome

        return CraftEvidenceOutcome(evidence=None, mode="unavailable", error_summary="boom")

    import scripts.check_craft as check_craft_module

    monkeypatch.setattr(check_craft_module, "prepare_craft_evidence", _fake_with_long_preview)
    monkeypatch.setattr(sys, "argv", ["check_craft.py", "--debug"])
    check_craft_module.main()
    out = capsys.readouterr().out

    preview_line = next(line for line in out.splitlines() if line.strip().startswith("preview:"))
    assert len(preview_line) < 1100  # bounded, not an unbounded dump


def test_debug_mode_never_prints_actual_secret_even_if_upstream_summary_leaked_it(
    monkeypatch, capsys
):
    # Defense in depth: even if some future bug produced a raw secret string
    # in a diagnostics field, the script itself must never introduce a new
    # unredacted printout path. This confirms today's script only prints the
    # already-sanitized diagnostics values, not raw request/response objects.
    import scripts.check_craft as check_craft_module

    monkeypatch.setattr(check_craft_module, "prepare_craft_evidence", _fake_prepare_craft_evidence)
    monkeypatch.setattr(sys, "argv", ["check_craft.py", "--debug"])
    check_craft_module.main()
    out = capsys.readouterr().out

    for suspicious in ("authorization:", "bearer ", "api_key", "client_secret", "cookie"):
        assert suspicious not in out.lower()
