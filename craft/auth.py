"""OAuth 2.1 + PKCE authentication for the CRAFT MCP server.

Uses the mcp SDK's OAuthClientProvider (mcp.client.auth), which implements
the full protected-resource discovery, authorization-server discovery, and
PKCE authorization_code flow. Tokens and client registration info live in
memory only for the process lifetime -- never persisted to disk, never
logged, never printed.
"""

import asyncio
import contextlib
import http.server
import threading
import urllib.parse
import webbrowser

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken


class InMemoryTokenStorage:
    """In-process-only TokenStorage. Never touches disk.

    client_info is pre-seeded with the project's fixed, pre-registered
    public OAuth client_id so the flow uses it directly instead of
    performing dynamic client registration each run.
    """

    def __init__(self, client_id: str, redirect_uri: str):
        self._tokens: OAuthToken | None = None
        self._client_info: OAuthClientInformationFull | None = OAuthClientInformationFull(
            client_id=client_id,
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )

    async def get_tokens(self) -> OAuthToken | None:
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client_info = client_info


class _CallbackServer:
    """Minimal loopback HTTP server that captures the OAuth redirect's
    `code`/`state` query params, then stops. Never logs request details,
    since the redirect URL contains the authorization code."""

    def __init__(self, port: int = 0):
        self._result: tuple[str | None, str | None] | None = None
        self._event = threading.Event()
        self._httpd = http.server.HTTPServer(("127.0.0.1", port), self._make_handler())
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def _make_handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]
                outer._result = (code, state)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"CRAFT authentication received. You may close this tab.")
                outer._event.set()

        return Handler

    def start(self) -> None:
        self._thread.start()

    def wait_for_callback(self, timeout: float) -> tuple[str, str | None]:
        received = self._event.wait(timeout)
        with contextlib.suppress(Exception):
            self._httpd.shutdown()
        if not received:
            raise TimeoutError("Timed out waiting for the CRAFT OAuth redirect.")
        code, state = self._result or (None, None)
        if not code:
            raise ValueError("OAuth redirect did not include an authorization code.")
        return code, state


def build_oauth_provider(
    server_url: str,
    client_id: str,
    callback_timeout_seconds: float,
) -> OAuthClientProvider:
    """Build a real OAuthClientProvider wired to a local loopback callback
    server. The redirect URL/authorization code are never logged.

    callback_timeout_seconds applies only to waiting for the human to
    complete browser-based consent (CRAFT_OAUTH_TIMEOUT_SECONDS). It has
    no effect on normal MCP network/tool-call timeouts, which are set
    separately and kept short (see craft.config.DEFAULT_TIMEOUT_SECONDS).
    """
    callback_server = _CallbackServer()
    callback_server.start()
    redirect_uri = f"http://127.0.0.1:{callback_server.port}/callback"

    async def redirect_handler(authorization_url: str) -> None:
        with contextlib.suppress(Exception):
            webbrowser.open(authorization_url)

    async def callback_handler() -> tuple[str, str | None]:
        return await asyncio.to_thread(callback_server.wait_for_callback, callback_timeout_seconds)

    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=OAuthClientMetadata(
            redirect_uris=[redirect_uri],
            client_name="ProofGate",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        storage=InMemoryTokenStorage(client_id=client_id, redirect_uri=redirect_uri),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=callback_timeout_seconds,
    )
