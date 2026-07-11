"""Tests for the server-truth-only schema discovery fix.

The live generate_sql tool requires {"question", "connection",
"schema": {"schema_name", "schema_fqn"}}. schema_name/schema_fqn must come
from real server responses to list_databases and get_schema -- never
constructed or guessed from CRAFT_DATABASE. Discovery sequence:

  1. list_data_connections -> select connection slug
  2. list_databases(connection) -> real database entries
  3. select the database matching CRAFT_DATABASE -> its server-returned
     name and fully_qualified_name
  4. get_schema(fqn=database_fqn, include_children=true) -> children
  5. select a real schema entry with a server-returned name and a valid
     three-segment fully_qualified_name
  6. generate_sql(question, connection, schema={schema_name, schema_fqn})

tests/conftest.py disables live CRAFT by default, so these tests use
dependency-injected fake sessions and never touch the network. Response
parsing (_tool_result_to_data / _extract_generated_sql) is not touched by
this fix.
"""

from craft.client import (
    CraftWorkflowError,
    _build_get_schema_arguments,
    _build_list_databases_arguments,
    _extract_entries_list,
    _is_valid_schema_fqn,
    _select_database_entry,
    _select_schema_entry,
    _select_tool_argument_name,
    run_craft_workflow,
)

BROAD_INSTRUCTION = "Clean up inactive test accounts that have not logged in for 90 days."


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeCallToolResult:
    def __init__(self, content=None, structured=None):
        self.structuredContent = structured
        self.content = content or []


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


def _good_responses():
    return {
        "list_data_connections": _connections_result(),
        "list_databases": _databases_result(),
        "get_schema": _schema_result(),
        "generate_sql": _FakeCallToolResult(structured={"sql": "SELECT 1"}),
        "execute_query": _FakeCallToolResult(structured={"rows": []}),
    }


# ---------------------------------------------------------------------------
# _is_valid_schema_fqn: exactly three dot-separated, non-empty segments
# ---------------------------------------------------------------------------


def test_valid_three_segment_fqn_is_accepted():
    assert _is_valid_schema_fqn("thelook.THELOOK_ECOMMERCE.PUBLIC") is True


def test_two_segment_fqn_is_rejected():
    assert _is_valid_schema_fqn("THELOOK_ECOMMERCE.PUBLIC") is False


def test_four_segment_fqn_is_rejected():
    assert _is_valid_schema_fqn("thelook.THELOOK_ECOMMERCE.PUBLIC.EXTRA") is False


def test_empty_segment_fqn_is_rejected():
    assert _is_valid_schema_fqn("thelook..PUBLIC") is False


def test_non_string_fqn_is_rejected():
    assert _is_valid_schema_fqn(None) is False
    assert _is_valid_schema_fqn(123) is False
    assert _is_valid_schema_fqn("") is False


# ---------------------------------------------------------------------------
# _extract_entries_list
# ---------------------------------------------------------------------------


def test_extract_entries_list_finds_databases_key():
    data = {"databases": [{"name": "A"}, {"name": "B"}]}
    assert _extract_entries_list(data) == [{"name": "A"}, {"name": "B"}]


def test_extract_entries_list_finds_children_key():
    data = {"children": [{"name": "PUBLIC"}]}
    assert _extract_entries_list(data) == [{"name": "PUBLIC"}]


def test_extract_entries_list_treats_single_object_as_one_entry():
    data = {"name": "PUBLIC", "fully_qualified_name": "a.b.PUBLIC"}
    assert _extract_entries_list(data) == [data]


def test_extract_entries_list_returns_empty_for_unrecognized_shape():
    assert _extract_entries_list({"tables": ["ORDERS"]}) == []
    assert _extract_entries_list(None) == []


def test_extract_entries_list_recognizes_list_metadata_results():
    # Confirmed live list_databases response shape.
    data = {
        "ok": True,
        "list_metadata": {
            "pagination": {"offset": 0, "limit": 50, "total": 1},
            "results": [
                {
                    "type": "database",
                    "name": "THELOOK_ECOMMERCE",
                    "fully_qualified_name": "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE",
                }
            ],
        },
    }
    entries = _extract_entries_list(data)
    assert entries == [
        {
            "type": "database",
            "name": "THELOOK_ECOMMERCE",
            "fully_qualified_name": "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE",
        }
    ]


def test_extract_entries_list_list_metadata_takes_priority_over_other_keys():
    # If a response somehow had both, the confirmed live nested location
    # must win over the other container-key guesses.
    data = {
        "list_metadata": {"results": [{"name": "REAL", "fully_qualified_name": "a.REAL"}]},
        "results": [{"name": "DECOY", "fully_qualified_name": "a.DECOY"}],
    }
    entries = _extract_entries_list(data)
    assert entries == [{"name": "REAL", "fully_qualified_name": "a.REAL"}]


def test_extract_entries_list_empty_list_metadata_results_returns_empty():
    data = {"ok": True, "list_metadata": {"pagination": {"total": 0}, "results": []}}
    assert _extract_entries_list(data) == []


def test_extract_entries_list_malformed_list_metadata_falls_back_gracefully():
    # "results" under list_metadata is not a list -- must not crash, and
    # must not silently pick up an unrelated container key either since
    # list_metadata's presence signals this response's real shape.
    data = {"list_metadata": {"results": "not-a-list"}}
    assert _extract_entries_list(data) == []


def test_extract_entries_list_recognizes_metadata_children():
    # Confirmed live get_schema response shape.
    data = {
        "ok": True,
        "metadata": {
            "children": [
                {
                    "type": "schema",
                    "name": "THELOOK_ECOMMERCE",
                    "fully_qualified_name": "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE.THELOOK_ECOMMERCE",
                }
            ]
        },
    }
    entries = _extract_entries_list(data)
    assert entries == [
        {
            "type": "schema",
            "name": "THELOOK_ECOMMERCE",
            "fully_qualified_name": "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE.THELOOK_ECOMMERCE",
        }
    ]


def test_extract_entries_list_metadata_children_takes_priority_over_other_keys():
    data = {
        "metadata": {"children": [{"name": "REAL", "fully_qualified_name": "a.b.REAL"}]},
        "children": [{"name": "DECOY", "fully_qualified_name": "a.b.DECOY"}],
    }
    entries = _extract_entries_list(data)
    assert entries == [{"name": "REAL", "fully_qualified_name": "a.b.REAL"}]


def test_extract_entries_list_empty_metadata_children_returns_empty():
    data = {"ok": True, "metadata": {"children": []}}
    assert _extract_entries_list(data) == []


def test_extract_entries_list_malformed_metadata_falls_back_gracefully():
    # "children" under metadata is not a list -- must not crash, and must
    # not silently pick up an unrelated container key either since
    # metadata's presence signals this response's real shape.
    data = {"metadata": {"children": "not-a-list"}}
    assert _extract_entries_list(data) == []


def test_extract_entries_list_list_metadata_takes_priority_over_metadata():
    # If a response somehow had both list_metadata and metadata containers,
    # list_metadata (the list_databases shape) must still win, since it is
    # checked first -- confirms adding metadata.children didn't reorder or
    # override the existing list_metadata.results priority.
    data = {
        "list_metadata": {"results": [{"name": "REAL", "fully_qualified_name": "a.REAL"}]},
        "metadata": {"children": [{"name": "DECOY", "fully_qualified_name": "a.b.DECOY"}]},
    }
    entries = _extract_entries_list(data)
    assert entries == [{"name": "REAL", "fully_qualified_name": "a.REAL"}]


# ---------------------------------------------------------------------------
# _select_database_entry: only accepts server-returned name + fqn
# ---------------------------------------------------------------------------


def test_select_database_entry_matches_configured_database_name():
    data = {
        "databases": [
            {"name": "OTHER_DB", "fully_qualified_name": "thelook.OTHER_DB"},
            {"name": "THELOOK_ECOMMERCE", "fully_qualified_name": "thelook.THELOOK_ECOMMERCE"},
        ]
    }
    name, fqn = _select_database_entry(data, "THELOOK_ECOMMERCE")
    assert name == "THELOOK_ECOMMERCE"
    assert fqn == "thelook.THELOOK_ECOMMERCE"


def test_select_database_entry_returns_none_when_no_fqn_present():
    data = {"databases": [{"name": "THELOOK_ECOMMERCE"}]}  # no fully_qualified_name at all
    name, fqn = _select_database_entry(data, "THELOOK_ECOMMERCE")
    assert name is None
    assert fqn is None


def test_select_database_entry_returns_none_when_no_match():
    data = {"databases": [{"name": "SOMETHING_ELSE", "fully_qualified_name": "thelook.SOMETHING_ELSE"}]}
    name, fqn = _select_database_entry(data, "THELOOK_ECOMMERCE")
    assert name is None
    assert fqn is None


def test_select_database_entry_selects_from_confirmed_live_list_metadata_shape():
    data = {
        "ok": True,
        "list_metadata": {
            "pagination": {"offset": 0, "limit": 50, "total": 1},
            "results": [
                {
                    "type": "database",
                    "name": "THELOOK_ECOMMERCE",
                    "fully_qualified_name": "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE",
                }
            ],
        },
    }
    name, fqn = _select_database_entry(data, "THELOOK_ECOMMERCE")
    assert name == "THELOOK_ECOMMERCE"
    assert fqn == "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE"


# ---------------------------------------------------------------------------
# _select_schema_entry: only accepts a server-returned three-segment fqn,
# never constructs or repairs one
# ---------------------------------------------------------------------------


def test_select_schema_entry_accepts_valid_server_returned_fqn():
    data = {"children": [{"type": "schema", "name": "PUBLIC", "fully_qualified_name": "thelook.THELOOK_ECOMMERCE.PUBLIC"}]}
    name, fqn = _select_schema_entry(data)
    assert name == "PUBLIC"
    assert fqn == "thelook.THELOOK_ECOMMERCE.PUBLIC"


def test_select_schema_entry_rejects_malformed_fqn_even_with_valid_name():
    # Only two segments -- must not be "fixed" or guessed at; reject outright.
    data = {"children": [{"type": "schema", "name": "PUBLIC", "fully_qualified_name": "THELOOK_ECOMMERCE.PUBLIC"}]}
    name, fqn = _select_schema_entry(data)
    assert name is None
    assert fqn is None


def test_select_schema_entry_returns_none_when_no_schema_typed_entry_present():
    data = {"children": [{"type": "table", "name": "ORDERS", "fully_qualified_name": "thelook.THELOOK_ECOMMERCE.ORDERS.rows"}]}
    name, fqn = _select_schema_entry(data)
    assert name is None
    assert fqn is None


def test_select_schema_entry_never_falls_back_to_hardcoded_public():
    # Even when no valid schema entry exists, the result must be (None, None)
    # -- never a guessed default like "PUBLIC".
    data = {"children": [{"type": "table", "name": "ORDERS"}]}
    name, fqn = _select_schema_entry(data)
    assert name is None
    assert fqn is None
    assert name != "PUBLIC"


def test_select_schema_entry_selects_from_confirmed_live_metadata_children_shape():
    data = {
        "ok": True,
        "metadata": {
            "children": [
                {
                    "type": "schema",
                    "name": "THELOOK_ECOMMERCE",
                    "fully_qualified_name": "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE.THELOOK_ECOMMERCE",
                }
            ]
        },
    }
    name, fqn = _select_schema_entry(data)
    assert name == "THELOOK_ECOMMERCE"
    assert fqn == "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE.THELOOK_ECOMMERCE"


# ---------------------------------------------------------------------------
# Argument builders honor the discovered input schema
# ---------------------------------------------------------------------------


def test_select_tool_argument_name_uses_discovered_property_name():
    schema = {"properties": {"database_fqn": {"type": "string"}}}
    assert _select_tool_argument_name(schema, ["fqn", "database_fqn"], "fqn") == "database_fqn"


def test_select_tool_argument_name_falls_back_to_default_when_schema_unknown():
    assert _select_tool_argument_name(None, ["fqn", "database_fqn"], "fqn") == "fqn"


def test_build_list_databases_arguments_uses_connection():
    args = _build_list_databases_arguments({"properties": {"connection": {}}}, "thelook")
    assert args == {"connection": "thelook"}


def test_build_get_schema_arguments_includes_fqn_and_include_children_true():
    schema = {
        "properties": {
            "connection": {},
            "fqn": {},
            "include_children": {},
        }
    }
    args = _build_get_schema_arguments(schema, "thelook", "thelook.THELOOK_ECOMMERCE")
    assert args == {
        "connection": "thelook",
        "fqn": "thelook.THELOOK_ECOMMERCE",
        "include_children": True,
    }


# ---------------------------------------------------------------------------
# End-to-end discovery sequence and generate_sql payload
# ---------------------------------------------------------------------------


def test_list_databases_runs_after_list_data_connections():
    session = FakeCraftSession(_default_tools(), _good_responses())
    run_craft_workflow("q", session_factory=_fake_session_factory(session))

    call_names = [name for name, _args in session.calls]
    assert call_names.index("list_data_connections") < call_names.index("list_databases")
    assert call_names.index("list_databases") < call_names.index("get_schema")
    assert call_names.index("get_schema") < call_names.index("generate_sql")


def test_get_schema_receives_the_server_returned_database_fqn():
    session = FakeCraftSession(_default_tools(), _good_responses())
    run_craft_workflow("q", session_factory=_fake_session_factory(session))

    get_schema_calls = [args for name, args in session.calls if name == "get_schema"]
    assert len(get_schema_calls) == 1
    assert get_schema_calls[0]["fqn"] == "thelook.THELOOK_ECOMMERCE"


def test_get_schema_receives_exact_required_arguments_with_confirmed_live_list_databases_shape():
    # Confirmed live list_databases response shape: results nested under
    # list_metadata, alongside pagination metadata.
    live_shaped_databases_result = _FakeCallToolResult(
        structured={
            "ok": True,
            "list_metadata": {
                "pagination": {"offset": 0, "limit": 50, "total": 1},
                "results": [
                    {
                        "type": "database",
                        "name": "THELOOK_ECOMMERCE",
                        "fully_qualified_name": "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE",
                    }
                ],
            },
        }
    )
    session = FakeCraftSession(
        _default_tools(), {**_good_responses(), "list_databases": live_shaped_databases_result}
    )

    run_craft_workflow("q", session_factory=_fake_session_factory(session))

    get_schema_calls = [args for name, args in session.calls if name == "get_schema"]
    assert len(get_schema_calls) == 1
    assert get_schema_calls[0] == {
        "connection": "thelook",
        "fqn": "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE",
        "include_children": True,
    }


def test_empty_list_metadata_results_fails_honestly():
    empty_live_shaped_result = _FakeCallToolResult(
        structured={"ok": True, "list_metadata": {"pagination": {"total": 0}, "results": []}}
    )
    session = FakeCraftSession(
        _default_tools(), {**_good_responses(), "list_databases": empty_live_shaped_result}
    )

    raised = False
    try:
        run_craft_workflow("q", session_factory=_fake_session_factory(session))
    except CraftWorkflowError:
        raised = True

    assert raised is True
    called_names = [name for name, _args in session.calls]
    assert "get_schema" not in called_names
    assert "generate_sql" not in called_names


def test_malformed_list_metadata_results_fails_honestly():
    malformed_result = _FakeCallToolResult(
        structured={"ok": True, "list_metadata": {"results": "not-a-list"}}
    )
    session = FakeCraftSession(
        _default_tools(), {**_good_responses(), "list_databases": malformed_result}
    )

    raised = False
    try:
        run_craft_workflow("q", session_factory=_fake_session_factory(session))
    except CraftWorkflowError:
        raised = True

    assert raised is True
    called_names = [name for name, _args in session.calls]
    assert "get_schema" not in called_names
    assert "generate_sql" not in called_names


def test_include_children_true_is_passed_when_supported_by_schema():
    session = FakeCraftSession(_default_tools(), _good_responses())
    run_craft_workflow("q", session_factory=_fake_session_factory(session))

    get_schema_calls = [args for name, args in session.calls if name == "get_schema"]
    assert get_schema_calls[0]["include_children"] is True


def test_schema_name_and_fqn_come_from_get_schema_result():
    session = FakeCraftSession(
        _default_tools(),
        {**_good_responses(), "get_schema": _schema_result(name="ANALYTICS", fqn="thelook.THELOOK_ECOMMERCE.ANALYTICS")},
    )
    run_craft_workflow("q", session_factory=_fake_session_factory(session))

    generate_sql_calls = [args for name, args in session.calls if name == "generate_sql"]
    assert generate_sql_calls[0]["schema"] == {
        "schema_name": "ANALYTICS",
        "schema_fqn": "thelook.THELOOK_ECOMMERCE.ANALYTICS",
    }


def test_schema_name_and_fqn_selected_from_confirmed_live_metadata_children_shape():
    live_shaped_get_schema_result = _FakeCallToolResult(
        structured={
            "ok": True,
            "metadata": {
                "children": [
                    {
                        "type": "schema",
                        "name": "THELOOK_ECOMMERCE",
                        "fully_qualified_name": "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE.THELOOK_ECOMMERCE",
                    }
                ]
            },
        }
    )
    session = FakeCraftSession(
        _default_tools(), {**_good_responses(), "get_schema": live_shaped_get_schema_result}
    )

    run_craft_workflow("q", session_factory=_fake_session_factory(session))

    generate_sql_calls = [args for name, args in session.calls if name == "generate_sql"]
    assert generate_sql_calls[0]["schema"] == {
        "schema_name": "THELOOK_ECOMMERCE",
        "schema_fqn": "thelook-ecommerce-0f0a359c.THELOOK_ECOMMERCE.THELOOK_ECOMMERCE",
    }


def test_empty_metadata_children_fails_honestly():
    empty_live_shaped_result = _FakeCallToolResult(
        structured={"ok": True, "metadata": {"children": []}}
    )
    session = FakeCraftSession(
        _default_tools(), {**_good_responses(), "get_schema": empty_live_shaped_result}
    )

    raised = False
    try:
        run_craft_workflow("q", session_factory=_fake_session_factory(session))
    except CraftWorkflowError:
        raised = True

    assert raised is True
    called_names = [name for name, _args in session.calls]
    assert "generate_sql" not in called_names


def test_malformed_metadata_children_fails_honestly():
    malformed_result = _FakeCallToolResult(structured={"ok": True, "metadata": {"children": "not-a-list"}})
    session = FakeCraftSession(
        _default_tools(), {**_good_responses(), "get_schema": malformed_result}
    )

    raised = False
    try:
        run_craft_workflow("q", session_factory=_fake_session_factory(session))
    except CraftWorkflowError:
        raised = True

    assert raised is True
    called_names = [name for name, _args in session.calls]
    assert "generate_sql" not in called_names


def test_no_fqn_is_fabricated_from_configured_names():
    # get_schema's children contain no fully_qualified_name at all; the
    # workflow must fail rather than construct one from CRAFT_DATABASE.
    responses = {
        **_good_responses(),
        "get_schema": _FakeCallToolResult(
            structured={"children": [{"type": "schema", "name": "PUBLIC"}]}
        ),
    }
    session = FakeCraftSession(_default_tools(), responses)

    raised = False
    try:
        run_craft_workflow("q", session_factory=_fake_session_factory(session))
    except CraftWorkflowError:
        raised = True

    assert raised is True
    called_names = [name for name, _args in session.calls]
    assert "generate_sql" not in called_names


def test_generate_sql_is_skipped_when_discovery_is_incomplete():
    responses = {
        **_good_responses(),
        "list_databases": _FakeCallToolResult(structured={"databases": []}),
    }
    session = FakeCraftSession(_default_tools(), responses)

    raised = False
    try:
        run_craft_workflow("q", session_factory=_fake_session_factory(session))
    except CraftWorkflowError:
        raised = True

    assert raised is True
    called_names = [name for name, _args in session.calls]
    assert "get_schema" not in called_names
    assert "generate_sql" not in called_names


def test_generate_sql_receives_exact_server_returned_schema_values():
    session = FakeCraftSession(_default_tools(), _good_responses())
    result = run_craft_workflow("cohort question", session_factory=_fake_session_factory(session))

    generate_sql_calls = [args for name, args in session.calls if name == "generate_sql"]
    sent = generate_sql_calls[0]
    assert set(sent.keys()) == {"question", "connection", "schema"}
    assert sent["question"] == "cohort question"
    assert sent["connection"] == "thelook"
    assert sent["schema"] == {"schema_name": "PUBLIC", "schema_fqn": "thelook.THELOOK_ECOMMERCE.PUBLIC"}
    assert result.generated_sql == "SELECT 1"


def test_response_parsing_of_generated_sql_remains_unchanged():
    responses = {
        **_good_responses(),
        "generate_sql": _FakeCallToolResult(content=[_FakeTextBlock("SELECT COUNT(*) FROM ORDERS")]),
    }
    session = FakeCraftSession(_default_tools(), responses)

    result = run_craft_workflow("q", session_factory=_fake_session_factory(session))
    assert result.generated_sql == "SELECT COUNT(*) FROM ORDERS"


def test_incomplete_schema_discovery_allows_cached_fallback():
    from craft.evidence import LABEL_CACHED, prepare_craft_evidence

    good_session = FakeCraftSession(_default_tools(), _good_responses())
    prepare_craft_evidence(
        "seed-wf", BROAD_INSTRUCTION, session_factory=_fake_session_factory(good_session)
    )

    incomplete_responses = {
        **_good_responses(),
        "get_schema": _FakeCallToolResult(structured={"children": []}),
    }
    incomplete_session = FakeCraftSession(_default_tools(), incomplete_responses)

    outcome = prepare_craft_evidence(
        "wf-2", BROAD_INSTRUCTION, session_factory=_fake_session_factory(incomplete_session)
    )

    assert outcome.mode == "cached"
    assert outcome.evidence.label == LABEL_CACHED
