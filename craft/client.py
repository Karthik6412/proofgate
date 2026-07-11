"""Real read-only CRAFT MCP workflow: connect, discover schema, generate_sql,
execute_query. This module never performs writes/mutations and never
authorizes, previews, or influences the operational deletion -- it only
produces enterprise cohort evidence for upstream context.
"""

import asyncio
import json
from dataclasses import dataclass

from craft import config
from craft.auth import build_oauth_provider


class CraftWorkflowError(Exception):
    """Raised (with a sanitized message) on any live-workflow failure."""


def unwrap_exception_group(exc: BaseException) -> BaseException:
    """Recursively unwrap (Base)ExceptionGroup wrappers (e.g. from anyio
    TaskGroups) down to the first concrete leaf exception, so the real
    underlying error isn't hidden behind a generic "unhandled errors in a
    TaskGroup" message."""
    current = exc
    while isinstance(current, BaseExceptionGroup) and current.exceptions:
        current = current.exceptions[0]
    return current


# ---------------------------------------------------------------------------
# --debug-only raw tool-result inspection. Purely additive: never called by,
# and never affects, the actual parsing/extraction logic below. Used to
# diagnose why a real MCP response fails to parse without guessing blind.
# ---------------------------------------------------------------------------

_DEBUG_SANITIZE_MARKERS = (
    "authorization",
    "bearer ",
    "token",
    "api_key",
    "apikey",
    "cookie",
    "client_secret",
    "set-cookie",
)

_DEBUG_FULL_DUMP_CHAR_THRESHOLD = 500
_DEBUG_PREVIEW_CHAR_LIMIT = 1000


def _redact_if_sensitive(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in _DEBUG_SANITIZE_MARKERS):
        return "(withheld: this text appears to contain a credential)"
    return text


def _safe_describe(result) -> dict:
    try:
        return describe_tool_result(result)
    except Exception:
        return {"error": "failed to summarize the raw tool result"}


def describe_tool_result(result) -> dict:
    """Sanitized, bounded structural summary of a raw MCP tool result, for
    --debug diagnostics only. Read-only: never mutates result, never used
    by _tool_result_to_data/_extract_generated_sql, and never changes what
    the real parser sees."""
    attributes = sorted(a for a in dir(result) if not a.startswith("_"))

    structured = getattr(result, "structuredContent", None)
    structured_snake = getattr(result, "structured_content", None)
    content = getattr(result, "content", None)

    content_block_count = len(content) if content else 0
    content_block_types = [type(block).__name__ for block in content] if content else []

    top_level_dict = None
    if isinstance(structured, dict):
        top_level_dict = structured
    elif isinstance(structured_snake, dict):
        top_level_dict = structured_snake
    top_level_keys = sorted(top_level_dict.keys()) if top_level_dict is not None else None

    raw_text = None
    if top_level_dict is not None:
        raw_text = json.dumps(top_level_dict, default=str)
    elif content:
        first_text = getattr(content[0], "text", None)
        if isinstance(first_text, str):
            raw_text = first_text

    repr_summary = _redact_if_sensitive(repr(result))[:300]

    if raw_text is not None:
        sanitized_raw = _redact_if_sensitive(raw_text)
        preview_truncated = len(sanitized_raw) > _DEBUG_FULL_DUMP_CHAR_THRESHOLD
        preview = sanitized_raw[:_DEBUG_PREVIEW_CHAR_LIMIT]
    else:
        preview_truncated = False
        preview = None

    return {
        "python_type": type(result).__name__,
        "repr_summary": repr_summary,
        "attributes": attributes,
        "has_structuredContent": structured is not None,
        "has_structured_content": structured_snake is not None,
        "has_content": content is not None,
        "content_block_count": content_block_count,
        "content_block_types": content_block_types,
        "top_level_keys": top_level_keys,
        "preview": preview,
        "preview_truncated": preview_truncated,
    }


@dataclass
class CraftWorkflowResult:
    database: str
    question: str
    generated_sql: str
    result_summary: str
    result_preview: list[dict]
    tool_trace: list[str]


def run_craft_workflow(
    question: str,
    session_factory=None,
    diagnostics: dict | None = None,
) -> CraftWorkflowResult:
    """Synchronous entry point. session_factory is a test-only dependency
    injection seam: an async zero-arg callable returning a fake session
    object exposing list_tools()/call_tool(), bypassing real OAuth/HTTP
    entirely so unit tests never touch the network."""
    return asyncio.run(
        _run_workflow_async(question, session_factory=session_factory, diagnostics=diagnostics)
    )


async def _run_workflow_async(
    question: str,
    session_factory=None,
    diagnostics: dict | None = None,
) -> CraftWorkflowResult:
    if diagnostics is None:
        diagnostics = {}
    diagnostics["current_stage"] = "connect"

    if session_factory is not None:
        session = await session_factory()
        return await _run_with_session(session, question, diagnostics)

    project_id = config.project_id()
    if not project_id:
        raise CraftWorkflowError("CRAFT_PROJECT_ID is not configured.")

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    provider = build_oauth_provider(
        config.mcp_url(), config.oauth_client_id(), config.oauth_timeout_seconds()
    )
    headers = {"X-Project-ID": project_id}

    # timeout here is the normal MCP network/tool-call timeout, deliberately
    # kept short; it is separate from the OAuth browser-consent wait above.
    async with streamablehttp_client(
        config.mcp_url(),
        headers=headers,
        auth=provider,
        timeout=config.DEFAULT_TIMEOUT_SECONDS,
    ) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            diagnostics["current_stage"] = "initialize"
            await session.initialize()
            diagnostics["authenticated"] = True
            return await _run_with_session(session, question, diagnostics)


async def _run_with_session(session, question: str, diagnostics: dict) -> CraftWorkflowResult:
    tool_trace: list[str] = diagnostics.setdefault("tool_trace", [])

    diagnostics["current_stage"] = "list_tools"
    tools_response = await session.list_tools()
    tool_names = {t.name for t in tools_response.tools}
    diagnostics["discovered_tool_names"] = sorted(tool_names)

    input_schemas = {tool.name: getattr(tool, "inputSchema", None) for tool in tools_response.tools}
    diagnostics["generate_sql_input_schema"] = input_schemas.get("generate_sql")
    diagnostics["list_databases_input_schema"] = input_schemas.get("list_databases")
    diagnostics["get_schema_input_schema"] = input_schemas.get("get_schema")
    tool_trace.append("list_tools")

    # Step 1: list_data_connections -> select the configured connection slug.
    discovery_tool = _select_discovery_tool(tool_names)
    diagnostics["discovery_tool"] = discovery_tool
    if discovery_tool is None:
        raise CraftWorkflowError("No supported discovery tool exposed by the CRAFT MCP server.")

    diagnostics["current_stage"] = discovery_tool
    connections_result = await session.call_tool(discovery_tool, {})
    tool_trace.append(discovery_tool)
    connection_slug = _select_connection_slug(_tool_result_to_data(connections_result), config.database())
    diagnostics["selected_database"] = connection_slug

    # Step 2: list_databases on the selected connection, per its discovered
    # input schema.
    if "list_databases" not in tool_names:
        raise CraftWorkflowError("CRAFT MCP server does not expose list_databases.")
    diagnostics["current_stage"] = "list_databases"
    list_databases_arguments = _build_list_databases_arguments(
        input_schemas.get("list_databases"), connection_slug
    )
    diagnostics["list_databases_arguments_sent"] = dict(list_databases_arguments)
    databases_result = await session.call_tool("list_databases", list_databases_arguments)
    tool_trace.append("list_databases")
    diagnostics["list_databases_response_summary"] = _safe_describe(databases_result)

    # Step 3: extract the actual, server-returned database name and FQN.
    database_name, database_fqn = _select_database_entry(
        _tool_result_to_data(databases_result), config.database()
    )
    diagnostics["selected_database_name"] = database_name
    diagnostics["selected_database_fqn"] = database_fqn

    schema_name = None
    schema_fqn = None

    # Step 4+5: get_schema on the real database FQN with include_children,
    # per get_schema's discovered input schema; select a real schema entry
    # with a server-returned name and three-segment FQN. Never construct or
    # guess a schema_fqn -- only accept what the server actually returned.
    if database_fqn and "get_schema" in tool_names:
        diagnostics["current_stage"] = "get_schema"
        get_schema_arguments = _build_get_schema_arguments(
            input_schemas.get("get_schema"), connection_slug, database_fqn
        )
        diagnostics["get_schema_arguments_sent"] = dict(get_schema_arguments)
        schema_result = await session.call_tool("get_schema", get_schema_arguments)
        tool_trace.append("get_schema")
        diagnostics["get_schema_response_summary"] = _safe_describe(schema_result)

        schema_name, schema_fqn = _select_schema_entry(_tool_result_to_data(schema_result))

    diagnostics["discovered_schema_name"] = schema_name
    diagnostics["discovered_schema_fqn"] = schema_fqn

    # "Schema discovery succeeded" means a valid schema_name AND a valid
    # three-segment schema_fqn were actually extracted -- a get_schema call
    # merely returning without a transport error is not enough.
    schema_discovery_succeeded = bool(schema_name and schema_fqn and _is_valid_schema_fqn(schema_fqn))
    diagnostics["schema_discovery_succeeded"] = schema_discovery_succeeded

    if not schema_discovery_succeeded:
        raise CraftWorkflowError(
            "Schema discovery did not produce both a server-returned schema_name "
            "and a valid three-segment schema_fqn; refusing to call generate_sql "
            "with incomplete schema arguments."
        )

    # Step 6.
    if "generate_sql" not in tool_names:
        raise CraftWorkflowError("CRAFT MCP server does not expose generate_sql.")
    diagnostics["current_stage"] = "generate_sql"
    generate_sql_arguments = {
        "question": question,
        "connection": connection_slug,
        "schema": {"schema_name": schema_name, "schema_fqn": schema_fqn},
    }
    sql_result = await session.call_tool("generate_sql", generate_sql_arguments)
    tool_trace.append("generate_sql")

    # Debug-only capture: never affects parsing below and never raises.
    diagnostics["generate_sql_arguments_sent"] = {
        "question": generate_sql_arguments.get("question"),
        "connection": generate_sql_arguments.get("connection"),
        "schema_name": schema_name,
        "schema_fqn": schema_fqn,
    }
    diagnostics["generate_sql_raw_result_summary"] = _safe_describe(sql_result)

    generated_sql = _extract_generated_sql(sql_result)
    diagnostics["generate_sql_succeeded"] = True

    if "execute_query" not in tool_names:
        raise CraftWorkflowError("CRAFT MCP server does not expose execute_query.")
    diagnostics["current_stage"] = "execute_query"
    query_result = await session.call_tool(
        "execute_query", {"connection": connection_slug, "sql": generated_sql}
    )
    tool_trace.append("execute_query")
    diagnostics["execute_query_succeeded"] = True

    # Confirmed live execute_query shape wraps metadata (artifact_fqn,
    # row_count, truncated) with no rows inline; older/simpler shapes with
    # rows directly present fall back to _summarize_query_result unchanged.
    execute_query_metadata = _extract_execute_query_metadata(_tool_result_to_data(query_result))
    diagnostics["execute_query_metadata"] = execute_query_metadata

    if execute_query_metadata is not None:
        artifact_fqn = execute_query_metadata.get("artifact_fqn")
        row_count = execute_query_metadata.get("row_count")

        if artifact_fqn:
            # Always fetch when an artifact_fqn is present -- never decided
            # by truncated, which the live server has returned as false
            # while still omitting rows from execute_query itself.
            if "get_result_page" not in tool_names:
                raise CraftWorkflowError(
                    "execute_query returned an artifact_fqn but the CRAFT MCP "
                    "server does not expose get_result_page to fetch its rows."
                )
            diagnostics["current_stage"] = "get_result_page"
            get_result_page_arguments = {"artifact_fqn": artifact_fqn, "offset": 0, "limit": 100}
            diagnostics["get_result_page_arguments_sent"] = dict(get_result_page_arguments)
            page_result = await session.call_tool("get_result_page", get_result_page_arguments)
            tool_trace.append("get_result_page")
            diagnostics["get_result_page_response_summary"] = _safe_describe(page_result)

            result_preview, result_summary, rows_recognized = _summarize_result_page(
                _tool_result_to_data(page_result)
            )
            if not rows_recognized:
                raise CraftWorkflowError("get_result_page did not return any recognizable rows.")
        elif row_count is not None and row_count > 0:
            raise CraftWorkflowError(
                "execute_query reported row_count > 0 but did not include an "
                "artifact_fqn to fetch the result rows from."
            )
        else:
            result_preview, result_summary = [], "Query returned no rows."
    else:
        result_preview, result_summary = _summarize_query_result(query_result)

    return CraftWorkflowResult(
        database=connection_slug,
        question=question,
        generated_sql=generated_sql,
        result_summary=result_summary,
        result_preview=result_preview,
        tool_trace=list(tool_trace),
    )


def _select_discovery_tool(tool_names: set[str]) -> str | None:
    if "list_data_connections" in tool_names:
        return "list_data_connections"
    if "list_databases" in tool_names:
        return "list_databases"
    return None


def _tool_result_to_data(result):
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    content = getattr(result, "content", None) or []
    if content:
        text = getattr(content[0], "text", None)
        if text:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text
    return None


def _select_connection_slug(data, target_database: str) -> str:
    connections = data.get("connections", []) if isinstance(data, dict) else []
    target = target_database.strip().lower()

    for conn in connections:
        if str(conn.get("description", "")).strip().lower() == target:
            return conn["slug"]
    for conn in connections:
        if target in str(conn.get("name", "")).strip().lower():
            return conn["slug"]
    if connections:
        return connections[0]["slug"]
    raise CraftWorkflowError(f"No data connection found matching {target_database!r}.")


def _is_valid_schema_fqn(fqn) -> bool:
    """The live generate_sql tool requires schema_fqn in the exact form
    {connection_slug}.{database}.{schema} -- exactly three non-empty,
    dot-separated segments."""
    if not isinstance(fqn, str) or not fqn.strip():
        return False
    segments = fqn.split(".")
    return len(segments) == 3 and all(seg.strip() for seg in segments)


def _extract_entries_list(data) -> list:
    """Pull a list of child entries out of a container response, trying the
    conventional container keys used across the CRAFT MCP tools. Falls back
    to treating the object itself as a single entry, or to a bare list."""
    if isinstance(data, dict):
        # Confirmed live list_databases shape:
        # {"ok": true, "list_metadata": {"pagination": {...}, "results": [...]}}
        list_metadata = data.get("list_metadata")
        if isinstance(list_metadata, dict):
            nested_results = list_metadata.get("results")
            if isinstance(nested_results, list):
                return nested_results

        # Confirmed live get_schema shape:
        # {"ok": true, "metadata": {"children": [...]}}
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            nested_children = metadata.get("children")
            if isinstance(nested_children, list):
                return nested_children

        for key in ("databases", "results", "children", "schemas", "items"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                return candidate
        if "name" in data or "fully_qualified_name" in data:
            return [data]
        return []
    if isinstance(data, list):
        return data
    return []


def _select_tool_argument_name(input_schema, candidates: list[str], default: str) -> str:
    """Pick the property name the tool's own discovered input schema
    actually uses, from a priority-ordered candidate list. Falls back to
    the first candidate if the schema is unknown."""
    if isinstance(input_schema, dict):
        properties = input_schema.get("properties")
        if isinstance(properties, dict):
            for name in candidates:
                if name in properties:
                    return name
    return default


def _build_list_databases_arguments(input_schema, connection_slug: str) -> dict:
    connection_key = _select_tool_argument_name(input_schema, ["connection"], "connection")
    return {connection_key: connection_slug}


def _build_get_schema_arguments(input_schema, connection_slug: str, database_fqn: str) -> dict:
    fqn_key = _select_tool_argument_name(
        input_schema, ["fqn", "database_fqn", "resource_fqn", "path"], "fqn"
    )
    include_children_key = _select_tool_argument_name(
        input_schema, ["include_children", "includeChildren", "with_children"], "include_children"
    )
    arguments = {fqn_key: database_fqn, include_children_key: True}

    properties = input_schema.get("properties") if isinstance(input_schema, dict) else None
    if properties is None or "connection" in properties:
        arguments["connection"] = connection_slug
    return arguments


def _select_database_entry(data, target_database: str) -> tuple[str | None, str | None]:
    """Select the database entry matching the configured database name from
    a list_databases result, returning its server-returned (name, fqn)."""
    entries = _extract_entries_list(data)
    target = target_database.strip().lower()

    def _entry_fqn(entry: dict) -> str | None:
        fqn = entry.get("fully_qualified_name")
        return fqn.strip() if isinstance(fqn, str) and fqn.strip() else None

    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("name", "")).strip().lower() == target:
            fqn = _entry_fqn(entry)
            if fqn:
                return entry.get("name"), fqn
    for entry in entries:
        if isinstance(entry, dict) and target in str(entry.get("name", "")).strip().lower():
            fqn = _entry_fqn(entry)
            if fqn:
                return entry.get("name"), fqn
    return None, None


def _select_schema_entry(data) -> tuple[str | None, str | None]:
    """Select a real schema entry from a get_schema(include_children=true)
    result, using only server-returned name/fully_qualified_name values.
    Never constructs or guesses an FQN -- a candidate is only accepted when
    its own fully_qualified_name is already a valid three-segment FQN."""
    entries = _extract_entries_list(data)

    def _valid_named_entry(entry) -> tuple[str, str] | None:
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        fqn = entry.get("fully_qualified_name")
        if isinstance(name, str) and name.strip() and _is_valid_schema_fqn(fqn):
            return name.strip(), fqn
        return None

    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("type", "")).strip().lower() == "schema":
            found = _valid_named_entry(entry)
            if found:
                return found

    for entry in entries:
        found = _valid_named_entry(entry)
        if found:
            return found

    return None, None


def _looks_like_sql(text: str) -> bool:
    lowered = text.lower()
    return "select" in lowered or "with" in lowered


_SQL_KEYS = ("sql", "query", "generated_sql")


def _first_sql_like_string(mapping: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip() and _looks_like_sql(value):
            return value.strip()
    return None


def _extract_generated_sql(result) -> str:
    data = _tool_result_to_data(result)
    if isinstance(data, str) and data.strip() and _looks_like_sql(data):
        return data.strip()
    if isinstance(data, dict):
        found = _first_sql_like_string(data, _SQL_KEYS)
        if found:
            return found

        # Live shape confirmed: {"generate_sql": {"sql": "...", "session_id":
        # ..., "explanation": ..., "assumptions": [...]}}. Only the nested
        # "sql" field is used; sibling metadata is ignored.
        nested = data.get("generate_sql")
        if isinstance(nested, dict):
            found = _first_sql_like_string(nested, _SQL_KEYS)
            if found:
                return found
    raise CraftWorkflowError("generate_sql did not return a recognizable SQL string.")


_PREVIEW_ROW_LIMIT = 5


def _summarize_query_result(result) -> tuple[list[dict], str]:
    data = _tool_result_to_data(result)
    rows = []
    if isinstance(data, dict):
        for key in ("rows", "results", "data"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
    elif isinstance(data, list):
        rows = data

    if not isinstance(rows, list):
        rows = []

    bounded_preview = [row for row in rows[:_PREVIEW_ROW_LIMIT] if isinstance(row, dict)]
    if rows:
        summary = f"Returned {len(rows)} row(s) (showing up to {_PREVIEW_ROW_LIMIT})."
    else:
        summary = "Query returned no rows."
    return bounded_preview, summary


def _extract_execute_query_metadata(data) -> dict | None:
    """Extract the confirmed live execute_query response envelope.

    Two confirmed live shapes exist:
        {"ok": true, "execute_query": {"artifact_fqn": ..., "row_count": ...,
                                        "truncated": ...}}
        {"ok": true, "result": {"execute_query": {"artifact_fqn": ...,
                                                   "row_count": ...,
                                                   "truncated": ...}}}
    The direct "execute_query" key is preferred if both somehow exist.
    Returns None when the response doesn't use either nested envelope
    shape, so older/simpler shapes (rows present directly) fall back to
    _summarize_query_result unchanged."""
    if isinstance(data, dict):
        nested = data.get("execute_query")
        if not isinstance(nested, dict):
            result = data.get("result")
            if isinstance(result, dict):
                nested = result.get("execute_query")

        if isinstance(nested, dict):
            return {
                "artifact_fqn": nested.get("artifact_fqn"),
                "row_count": nested.get("row_count"),
                "truncated": nested.get("truncated"),
            }
    return None


def _summarize_result_page(data) -> tuple[list[dict], str, bool]:
    """Summarize a get_result_page response. Returns (preview, summary,
    rows_recognized) -- rows_recognized is False only when no known rows
    container key was found at all (a malformed/unrecognizable response),
    not merely when the recognized container is empty."""
    rows = None
    if isinstance(data, dict):
        # Confirmed live get_result_page shape:
        # {"ok": true, "preview": {"columns": [...], "rows": [...]}, ...}
        # preview.columns is never inspected for rows -- its presence must
        # not interfere with extracting preview.rows.
        preview = data.get("preview")
        if isinstance(preview, dict):
            nested_rows = preview.get("rows")
            if isinstance(nested_rows, list):
                rows = nested_rows

        if rows is None:
            for key in ("rows", "results", "data"):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    rows = candidate
                    break
    elif isinstance(data, list):
        rows = data

    if rows is None:
        return [], "get_result_page did not return recognizable rows.", False

    bounded_preview = [row for row in rows[:_PREVIEW_ROW_LIMIT] if isinstance(row, dict)]
    if rows:
        summary = f"Returned {len(rows)} row(s) (showing up to {_PREVIEW_ROW_LIMIT})."
    else:
        summary = "Query returned no rows."
    return bounded_preview, summary, True
