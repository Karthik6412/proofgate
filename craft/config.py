"""CRAFT configuration, read from environment variables.

Never prints or persists complete environment-variable values -- only
booleans/derived non-secret strings are exposed for introspection.
"""

import os
from pathlib import Path

DEFAULT_MCP_URL = "https://nebius.emergence.ai/mcp"
DEFAULT_OAUTH_CLIENT_ID = "em-runtime-mcp"
DEFAULT_DATABASE = "THELOOK_ECOMMERCE"
DEFAULT_CACHE_PATH = "artifacts/craft_evidence_cache.json"

# Normal network/tool-call timeout (MCP list_tools/call_tool requests).
# Deliberately kept short so a stalled connection fails fast.
DEFAULT_TIMEOUT_SECONDS = 25.0

# Separate, much longer timeout that applies ONLY to waiting for the human
# to complete browser-based OAuth consent. 25s was too short for a real
# person to log in and approve; this does not affect any other timeout.
DEFAULT_OAUTH_TIMEOUT_SECONDS = 180.0


def oauth_timeout_seconds() -> float:
    raw = os.environ.get("CRAFT_OAUTH_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_OAUTH_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_OAUTH_TIMEOUT_SECONDS


def live_enabled() -> bool:
    return os.environ.get("CRAFT_LIVE_ENABLED", "true").strip().lower() in ("1", "true", "yes")


def mcp_url() -> str:
    return os.environ.get("CRAFT_MCP_URL", DEFAULT_MCP_URL)


def project_id() -> str | None:
    return os.environ.get("CRAFT_PROJECT_ID") or None


def has_project_id() -> bool:
    return project_id() is not None


def oauth_client_id() -> str:
    return os.environ.get("CRAFT_OAUTH_CLIENT_ID", DEFAULT_OAUTH_CLIENT_ID)


def database() -> str:
    return os.environ.get("CRAFT_DATABASE", DEFAULT_DATABASE)


def cache_path() -> Path:
    return Path(os.environ.get("CRAFT_CACHE_PATH", DEFAULT_CACHE_PATH))
