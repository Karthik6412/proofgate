"""Tests for the get_result_page result-fetching patch.

Confirmed live execute_query response shape wraps metadata with no rows
inline:

    {"ok": true, "execute_query": {"artifact_fqn": "artifact:...",
                                    "row_count": 1, "truncated": false}}

When artifact_fqn is present, get_result_page(artifact_fqn, offset=0,
limit=100) must be called -- unconditionally, never gated on truncated --
and its rows become the authoritative result summary/preview. This only
touches result-fetching after execute_query; OAuth, discovery, generate_sql
request/parsing, policy, deletion, Nebius, audit, and UI are untouched.

tests/conftest.py disables live CRAFT by default, so these tests use
dependency-injected fake sessions and never touch the network.
"""

from craft.client import CraftWorkflowError, _summarize_result_page, run_craft_workflow

BROAD_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


class _FakeCallToolResult:
    def __init__(self, structured=None):
        self.structuredContent = structured
        self.content = []


class _FakeTool:
    def __init__(self, name, input_schema=None):
        self.name = name
        self.inputSchema = input_schema


class _FakeToolsResponse:
    def __init__(self, tools):
        self.tools = tools


class FakeCraftSession:
    def __init__(self, tools, responses):
        self._tools = tools
        self._responses = responses
        self.calls = []

    async def list_tools(self):
        return _FakeToolsResponse(self._tools)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self._responses.get(name)


def _default_tools():
    return [
        _FakeTool("list_data_connections"),
        _FakeTool("list_databases", {"properties": {"connection": {"type": "string"}}}),
        _FakeTool(
            "get_schema",
            {
                "properties": {
                    "connection": {"type": "string"},
                    "fqn": {"type": "string"},
                    "include_children": {"type": "boolean"},
                }
            },
        ),
        _FakeTool("generate_sql"),
        _FakeTool("execute_query"),
        _FakeTool(
            "get_result_page",
            {
                "properties": {
                    "artifact_fqn": {"type": "string"},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 100},
                }
            },
        ),
    ]


def _connections_result():
    return _FakeCallToolResult(
        structured={"connections": [{"slug": "thelook", "description": "THELOOK_ECOMMERCE"}]}
    )


def _databases_result(fqn="thelook.THELOOK_ECOMMERCE", name="THELOOK_ECOMMERCE"):
    return _FakeCallToolResult(
        structured={"databases": [{"type": "database", "name": name, "fully_qualified_name": fqn}]}
    )


def _schema_result(fqn="thelook.THELOOK_ECOMMERCE.PUBLIC", name="PUBLIC"):
    return _FakeCallToolResult(
        structured={"children": [{"type": "schema", "name": name, "fully_qualified_name": fqn}]}
    )


def _fake_session_factory(session):
    async def factory():
        return session

    return factory


def _good_responses(execute_query_result=None):
    return {
        "list_data_connections": _connections_result(),
        "list_databases": _databases_result(),
        "get_schema": _schema_result(),
        "generate_sql": _FakeCallToolResult(structured={"sql": "SELECT 1"}),
        "execute_query": execute_query_result
        or _FakeCallToolResult(
            structured={
                "ok": True,
                "execute_query": {
                    "artifact_fqn": "artifact:abc123",
                    "row_count": 1,
                    "truncated": False,
                },
            }
        ),
        "get_result_page": _FakeCallToolResult(structured={"rows": [{"cohort_count": 42}]}),
    }


def _run(responses):
    session = FakeCraftSession(_default_tools(), responses)
    result = run_craft_workflow("q", session_factory=_fake_session_factory(session))
    return session, result


# ---------------------------------------------------------------------------
# artifact_fqn extraction and get_result_page invocation
# ---------------------------------------------------------------------------


def test_execute_query_artifact_fqn_is_extracted_and_get_result_page_is_called():
    session, _result = _run(_good_responses())

    get_result_page_calls = [args for name, args in session.calls if name == "get_result_page"]
    assert len(get_result_page_calls) == 1


def test_get_result_page_is_called_with_artifact_fqn_offset_0_limit_100():
    session, _result = _run(_good_responses())

    get_result_page_calls = [args for name, args in session.calls if name == "get_result_page"]
    assert get_result_page_calls[0] == {
        "artifact_fqn": "artifact:abc123",
        "offset": 0,
        "limit": 100,
    }


def test_rows_from_get_result_page_populate_result_summary_and_preview():
    responses = _good_responses()
    responses["get_result_page"] = _FakeCallToolResult(
        structured={"rows": [{"cohort_count": 3187}]}
    )
    _session, result = _run(responses)

    assert result.result_preview == [{"cohort_count": 3187}]
    assert "1 row" in result.result_summary


def test_get_result_page_is_called_even_when_truncated_is_false():
    responses = _good_responses(
        execute_query_result=_FakeCallToolResult(
            structured={
                "ok": True,
                "execute_query": {
                    "artifact_fqn": "artifact:abc123",
                    "row_count": 1,
                    "truncated": False,
                },
            }
        )
    )
    session, _result = _run(responses)

    called_names = [name for name, _args in session.calls]
    assert "get_result_page" in called_names


def test_get_result_page_is_called_even_when_truncated_is_true():
    # Fetching must not be gated on truncated in either direction.
    responses = _good_responses(
        execute_query_result=_FakeCallToolResult(
            structured={
                "ok": True,
                "execute_query": {
                    "artifact_fqn": "artifact:abc123",
                    "row_count": 500,
                    "truncated": True,
                },
            }
        )
    )
    session, _result = _run(responses)

    called_names = [name for name, _args in session.calls]
    assert "get_result_page" in called_names


def test_tool_trace_includes_get_result_page_when_called():
    _session, result = _run(_good_responses())
    assert "get_result_page" in result.tool_trace


def test_tool_trace_excludes_get_result_page_when_not_called():
    # Legacy/simple shape: rows present directly, no execute_query envelope
    # -- get_result_page must not appear in the trace since it was never
    # actually invoked.
    responses = _good_responses(
        execute_query_result=_FakeCallToolResult(structured={"rows": [{"n": 1}]})
    )
    _session, result = _run(responses)
    assert "get_result_page" not in result.tool_trace


# ---------------------------------------------------------------------------
# Fail-honest cases
# ---------------------------------------------------------------------------


def test_missing_artifact_fqn_with_row_count_above_zero_fails_honestly():
    responses = _good_responses(
        execute_query_result=_FakeCallToolResult(
            structured={
                "ok": True,
                "execute_query": {"artifact_fqn": None, "row_count": 5, "truncated": False},
            }
        )
    )
    session = FakeCraftSession(_default_tools(), responses)

    raised = False
    try:
        run_craft_workflow("q", session_factory=_fake_session_factory(session))
    except CraftWorkflowError:
        raised = True

    assert raised is True
    called_names = [name for name, _args in session.calls]
    assert "get_result_page" not in called_names


def test_malformed_page_response_fails_honestly():
    responses = _good_responses()
    responses["get_result_page"] = _FakeCallToolResult(structured={"unexpected": "shape"})
    session = FakeCraftSession(_default_tools(), responses)

    raised = False
    try:
        run_craft_workflow("q", session_factory=_fake_session_factory(session))
    except CraftWorkflowError:
        raised = True

    assert raised is True


def test_artifact_fqn_present_but_get_result_page_not_exposed_fails_honestly():
    tools_without_page = [t for t in _default_tools() if t.name != "get_result_page"]
    session = FakeCraftSession(tools_without_page, _good_responses())

    raised = False
    try:
        run_craft_workflow("q", session_factory=_fake_session_factory(session))
    except CraftWorkflowError:
        raised = True

    assert raised is True


# ---------------------------------------------------------------------------
# Zero-row behavior remains valid
# ---------------------------------------------------------------------------


def test_zero_row_count_with_no_artifact_fqn_is_a_valid_empty_result():
    responses = _good_responses(
        execute_query_result=_FakeCallToolResult(
            structured={
                "ok": True,
                "execute_query": {"artifact_fqn": None, "row_count": 0, "truncated": False},
            }
        )
    )
    session, result = _run(responses)

    assert result.result_preview == []
    assert result.result_summary == "Query returned no rows."
    called_names = [name for name, _args in session.calls]
    assert "get_result_page" not in called_names


def test_zero_row_count_with_missing_row_count_field_is_a_valid_empty_result():
    responses = _good_responses(
        execute_query_result=_FakeCallToolResult(
            structured={"ok": True, "execute_query": {"artifact_fqn": None, "truncated": False}}
        )
    )
    session, result = _run(responses)

    assert result.result_preview == []
    assert result.result_summary == "Query returned no rows."
    called_names = [name for name, _args in session.calls]
    assert "get_result_page" not in called_names


def test_legacy_top_level_rows_shape_with_empty_rows_still_works():
    responses = _good_responses(
        execute_query_result=_FakeCallToolResult(structured={"rows": []})
    )
    _session, result = _run(responses)

    assert result.result_preview == []
    assert result.result_summary == "Query returned no rows."


def test_get_result_page_with_empty_but_recognized_rows_is_not_an_error():
    responses = _good_responses()
    responses["get_result_page"] = _FakeCallToolResult(structured={"rows": []})
    _session, result = _run(responses)

    assert result.result_preview == []
    assert result.result_summary == "Query returned no rows."


# ---------------------------------------------------------------------------
# Previously supported top-level rows shapes still work
# ---------------------------------------------------------------------------


def test_legacy_top_level_rows_shape_with_data_still_works():
    responses = _good_responses(
        execute_query_result=_FakeCallToolResult(structured={"rows": [{"n": 1}, {"n": 2}]})
    )
    _session, result = _run(responses)

    assert result.result_preview == [{"n": 1}, {"n": 2}]
    assert "2 row" in result.result_summary


# ---------------------------------------------------------------------------
# result.execute_query nested envelope (second confirmed live shape)
# ---------------------------------------------------------------------------


def _nested_result_execute_query_result(artifact_fqn="artifact:nested456", row_count=1, truncated=False):
    return _FakeCallToolResult(
        structured={
            "ok": True,
            "result": {
                "execute_query": {
                    "artifact_fqn": artifact_fqn,
                    "row_count": row_count,
                    "truncated": truncated,
                }
            },
        }
    )


def test_result_execute_query_artifact_fqn_is_extracted():
    responses = _good_responses(execute_query_result=_nested_result_execute_query_result())
    session, _result = _run(responses)

    get_result_page_calls = [args for name, args in session.calls if name == "get_result_page"]
    assert len(get_result_page_calls) == 1
    assert get_result_page_calls[0]["artifact_fqn"] == "artifact:nested456"


def test_row_count_and_truncated_are_preserved_for_nested_result_shape():
    import craft.client as client_module

    data = {
        "ok": True,
        "result": {
            "execute_query": {"artifact_fqn": "artifact:nested456", "row_count": 7, "truncated": True}
        },
    }
    metadata = client_module._extract_execute_query_metadata(data)

    assert metadata == {"artifact_fqn": "artifact:nested456", "row_count": 7, "truncated": True}


def test_get_result_page_is_called_for_the_live_nested_result_shape():
    responses = _good_responses(execute_query_result=_nested_result_execute_query_result())
    session, _result = _run(responses)

    called_names = [name for name, _args in session.calls]
    assert "get_result_page" in called_names


def test_get_result_page_called_with_correct_arguments_for_nested_result_shape():
    responses = _good_responses(execute_query_result=_nested_result_execute_query_result())
    session, _result = _run(responses)

    get_result_page_calls = [args for name, args in session.calls if name == "get_result_page"]
    assert get_result_page_calls[0] == {
        "artifact_fqn": "artifact:nested456",
        "offset": 0,
        "limit": 100,
    }


def test_tool_trace_includes_get_result_page_for_nested_result_shape():
    responses = _good_responses(execute_query_result=_nested_result_execute_query_result())
    _session, result = _run(responses)

    assert "get_result_page" in result.tool_trace


def test_rows_from_get_result_page_populate_summary_for_nested_result_shape():
    responses = _good_responses(execute_query_result=_nested_result_execute_query_result())
    responses["get_result_page"] = _FakeCallToolResult(structured={"rows": [{"cohort_count": 3187}]})
    _session, result = _run(responses)

    assert result.result_preview == [{"cohort_count": 3187}]
    assert "1 row" in result.result_summary


def test_get_result_page_called_regardless_of_truncated_for_nested_result_shape():
    responses = _good_responses(
        execute_query_result=_nested_result_execute_query_result(row_count=500, truncated=True)
    )
    session, _result = _run(responses)

    called_names = [name for name, _args in session.calls]
    assert "get_result_page" in called_names


def test_direct_execute_query_shape_still_works():
    # The originally confirmed shape (no "result" wrapper) must keep working.
    responses = _good_responses(
        execute_query_result=_FakeCallToolResult(
            structured={
                "ok": True,
                "execute_query": {
                    "artifact_fqn": "artifact:direct789",
                    "row_count": 1,
                    "truncated": False,
                },
            }
        )
    )
    session, _result = _run(responses)

    get_result_page_calls = [args for name, args in session.calls if name == "get_result_page"]
    assert get_result_page_calls[0]["artifact_fqn"] == "artifact:direct789"


def test_direct_shape_takes_precedence_when_both_shapes_exist():
    responses = _good_responses(
        execute_query_result=_FakeCallToolResult(
            structured={
                "ok": True,
                "execute_query": {
                    "artifact_fqn": "artifact:direct-wins",
                    "row_count": 1,
                    "truncated": False,
                },
                "result": {
                    "execute_query": {
                        "artifact_fqn": "artifact:nested-loses",
                        "row_count": 1,
                        "truncated": False,
                    }
                },
            }
        )
    )
    session, _result = _run(responses)

    get_result_page_calls = [args for name, args in session.calls if name == "get_result_page"]
    assert get_result_page_calls[0]["artifact_fqn"] == "artifact:direct-wins"


def test_malformed_nested_result_envelope_with_row_count_fails_honestly():
    # Missing artifact_fqn but row_count > 0, reached via the new nested
    # result.execute_query location -- must raise exactly like the direct
    # shape already does, proving the fail-honest logic applies uniformly
    # regardless of which shape produced the metadata.
    responses = _good_responses(
        execute_query_result=_nested_result_execute_query_result(artifact_fqn=None, row_count=5)
    )
    session = FakeCraftSession(_default_tools(), responses)

    raised = False
    try:
        run_craft_workflow("q", session_factory=_fake_session_factory(session))
    except CraftWorkflowError:
        raised = True

    assert raised is True
    called_names = [name for name, _args in session.calls]
    assert "get_result_page" not in called_names


def test_malformed_nested_result_container_falls_back_without_crashing():
    # "result" key present but its "execute_query" value is not a dict at
    # all -- must not crash; falls back to being treated as an unrecognized
    # shape (legacy _summarize_query_result path), which reports zero rows
    # rather than fabricating data or raising an unrelated exception.
    responses = _good_responses(
        execute_query_result=_FakeCallToolResult(
            structured={"ok": True, "result": {"execute_query": "not-a-dict"}}
        )
    )
    _session, result = _run(responses)

    assert result.result_preview == []
    assert result.result_summary == "Query returned no rows."


# ---------------------------------------------------------------------------
# get_result_page preview.rows / preview.columns shape (confirmed live shape)
# ---------------------------------------------------------------------------


def test_preview_rows_is_recognized():
    preview, summary, rows_recognized = _summarize_result_page(
        {"ok": True, "preview": {"columns": ["cohort_count"], "rows": [{"cohort_count": 3187}]}}
    )
    assert rows_recognized is True
    assert preview == [{"cohort_count": 3187}]
    assert "1 row" in summary


def test_preview_columns_does_not_interfere():
    # A rich columns array (with types, nested metadata) must never be
    # mistaken for rows or otherwise change what rows are extracted.
    preview, summary, rows_recognized = _summarize_result_page(
        {
            "ok": True,
            "preview": {
                "columns": [
                    {"name": "cohort_count", "type": "INTEGER"},
                    {"name": "user_id", "type": "STRING"},
                ],
                "rows": [{"cohort_count": 3187, "user_id": "abc"}],
            },
        }
    )
    assert rows_recognized is True
    assert preview == [{"cohort_count": 3187, "user_id": "abc"}]
    assert "1 row" in summary


def test_one_returned_row_via_preview_rows_produces_expected_summary():
    responses = _good_responses()
    responses["get_result_page"] = _FakeCallToolResult(
        structured={
            "ok": True,
            "preview": {"columns": ["cohort_count"], "rows": [{"cohort_count": 3187}]},
            "x_project_id_received": "0f0a359c-b584-4529-888f-602298db3f1f",
        }
    )
    _session, result = _run(responses)

    assert result.result_preview == [{"cohort_count": 3187}]
    assert "1 row" in result.result_summary


def test_empty_preview_rows_remains_a_valid_zero_row_result():
    responses = _good_responses()
    responses["get_result_page"] = _FakeCallToolResult(
        structured={"ok": True, "preview": {"columns": ["cohort_count"], "rows": []}}
    )
    _session, result = _run(responses)

    assert result.result_preview == []
    assert result.result_summary == "Query returned no rows."


def test_malformed_preview_rows_value_fails_honestly():
    # preview present but rows is not a list at all -- must not silently
    # accept it, and there's no other recognizable container to fall back
    # to, so this must be rejected as unrecognizable.
    responses = _good_responses()
    responses["get_result_page"] = _FakeCallToolResult(
        structured={"ok": True, "preview": {"columns": ["cohort_count"], "rows": "not-a-list"}}
    )
    session = FakeCraftSession(_default_tools(), responses)

    raised = False
    try:
        run_craft_workflow("q", session_factory=_fake_session_factory(session))
    except CraftWorkflowError:
        raised = True

    assert raised is True


def test_malformed_preview_container_fails_honestly():
    # "preview" present but not a dict at all, and no other recognizable
    # rows container present either.
    responses = _good_responses()
    responses["get_result_page"] = _FakeCallToolResult(structured={"ok": True, "preview": "oops"})
    session = FakeCraftSession(_default_tools(), responses)

    raised = False
    try:
        run_craft_workflow("q", session_factory=_fake_session_factory(session))
    except CraftWorkflowError:
        raised = True

    assert raised is True


def test_preview_rows_takes_priority_over_legacy_top_level_rows():
    # If a response somehow had both, the confirmed live preview.rows
    # location must win.
    preview, _summary, rows_recognized = _summarize_result_page(
        {"rows": [{"n": "DECOY"}], "preview": {"rows": [{"n": "REAL"}]}}
    )
    assert rows_recognized is True
    assert preview == [{"n": "REAL"}]


def test_legacy_top_level_rows_shape_still_works_for_result_page():
    # Previously supported get_result_page shape (rows directly present,
    # no "preview" wrapper) must keep working unchanged.
    preview, summary, rows_recognized = _summarize_result_page({"rows": [{"n": 1}, {"n": 2}]})
    assert rows_recognized is True
    assert preview == [{"n": 1}, {"n": 2}]
    assert "2 row" in summary


def test_legacy_results_key_shape_still_works_for_result_page():
    preview, _summary, rows_recognized = _summarize_result_page({"results": [{"n": 1}]})
    assert rows_recognized is True
    assert preview == [{"n": 1}]


def test_legacy_data_key_shape_still_works_for_result_page():
    preview, _summary, rows_recognized = _summarize_result_page({"data": [{"n": 1}]})
    assert rows_recognized is True
    assert preview == [{"n": 1}]


def test_bare_list_shape_still_works_for_result_page():
    preview, _summary, rows_recognized = _summarize_result_page([{"n": 1}])
    assert rows_recognized is True
    assert preview == [{"n": 1}]
