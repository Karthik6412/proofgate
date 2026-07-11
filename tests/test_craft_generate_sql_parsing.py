"""Tests for the narrow generate_sql response-parsing patch.

The live generate_sql tool was confirmed to respond with:

    {"generate_sql": {"sql": "...", "session_id": ..., "explanation": ...,
                       "assumptions": [...]}}

_extract_generated_sql must recognize generate_sql.sql specifically, while
continuing to support every previously-supported response shape. This only
touches SQL extraction -- schema discovery, OAuth, generate_sql request
construction, execute_query, policy, deletion, Nebius, audit, and UI are
untouched.
"""

from craft.client import CraftWorkflowError, _extract_generated_sql

VALID_SQL = "SELECT COUNT(*) AS cohort_count FROM ORDERS"


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeCallToolResult:
    def __init__(self, content=None, structured=None):
        self.structuredContent = structured
        self.content = content or []


def _structured(data):
    return _FakeCallToolResult(structured=data)


def _text(text):
    return _FakeCallToolResult(content=[_FakeTextBlock(text)])


# ---------------------------------------------------------------------------
# New shape: {"generate_sql": {"sql": "..."}}
# ---------------------------------------------------------------------------


def test_nested_generate_sql_sql_shape_is_parsed_successfully():
    result = _structured({"generate_sql": {"sql": VALID_SQL}})
    assert _extract_generated_sql(result) == VALID_SQL


def test_nested_metadata_does_not_interfere_with_extraction():
    result = _structured(
        {
            "generate_sql": {
                "session_id": "abc-123-session",
                "sql": VALID_SQL,
                "explanation": "Counts customers inactive for 90+ days.",
                "assumptions": ["Assumes ORDERS.created_at is the activity timestamp."],
            }
        }
    )
    assert _extract_generated_sql(result) == VALID_SQL


def test_nested_metadata_key_order_does_not_matter():
    result = _structured(
        {
            "generate_sql": {
                "assumptions": ["some assumption"],
                "explanation": "some explanation",
                "sql": VALID_SQL,
                "session_id": "xyz",
            }
        }
    )
    assert _extract_generated_sql(result) == VALID_SQL


def test_missing_generate_sql_dot_sql_fails_honestly():
    # "generate_sql" key present, but no "sql" (or "query"/"generated_sql")
    # inside it -- must raise, never fabricate or silently pick a wrong field.
    result = _structured(
        {
            "generate_sql": {
                "session_id": "abc-123",
                "explanation": "no sql field here",
                "assumptions": [],
            }
        }
    )
    raised = False
    try:
        _extract_generated_sql(result)
    except CraftWorkflowError:
        raised = True
    assert raised is True


def test_missing_generate_sql_key_entirely_fails_honestly():
    result = _structured({"session_id": "abc-123", "explanation": "no generate_sql wrapper"})
    raised = False
    try:
        _extract_generated_sql(result)
    except CraftWorkflowError:
        raised = True
    assert raised is True


def test_non_sql_text_at_generate_sql_dot_sql_is_rejected():
    result = _structured({"generate_sql": {"sql": "this is not a query at all"}})
    raised = False
    try:
        _extract_generated_sql(result)
    except CraftWorkflowError:
        raised = True
    assert raised is True


def test_generate_sql_value_that_is_not_a_dict_is_ignored_not_crashed():
    # "generate_sql" present but as a plain string, not the expected nested
    # object -- must fail closed, not raise an unrelated exception.
    result = _structured({"generate_sql": "not an object"})
    raised = False
    try:
        _extract_generated_sql(result)
    except CraftWorkflowError:
        raised = True
    assert raised is True


# ---------------------------------------------------------------------------
# Previously supported shapes must still work
# ---------------------------------------------------------------------------


def test_top_level_plain_sql_string_still_parses():
    result = _text(VALID_SQL)
    assert _extract_generated_sql(result) == VALID_SQL


def test_top_level_sql_key_still_parses():
    result = _structured({"sql": VALID_SQL})
    assert _extract_generated_sql(result) == VALID_SQL


def test_top_level_query_key_still_parses():
    result = _structured({"query": VALID_SQL})
    assert _extract_generated_sql(result) == VALID_SQL


def test_top_level_generated_sql_key_still_parses():
    result = _structured({"generated_sql": VALID_SQL})
    assert _extract_generated_sql(result) == VALID_SQL


def test_top_level_key_is_preferred_over_nested_generate_sql_key():
    # Defensive: if a response somehow had both, the direct top-level "sql"
    # (already-supported shape) should still resolve without needing the
    # new nested lookup.
    result = _structured({"sql": VALID_SQL, "generate_sql": {"sql": "SELECT 2"}})
    assert _extract_generated_sql(result) == VALID_SQL


def test_completely_unrecognizable_shape_still_fails_honestly():
    result = _structured({"unexpected": "shape"})
    raised = False
    try:
        _extract_generated_sql(result)
    except CraftWorkflowError:
        raised = True
    assert raised is True
