"""Shared plumbing for the live tests: one real browser driven through an MCP client.

Live tests are opt-in (ZENDRIVER_MCP_LIVE=1) because they need Chrome and a
display. Starting Chrome costs seconds, so a module runs all of its pages in one
session and the individual tests assert on what that session recorded.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from mcp import Client

live = pytest.mark.skipif(
    os.environ.get("ZENDRIVER_MCP_LIVE") != "1", reason="live browser test (ZENDRIVER_MCP_LIVE=1)"
)


def data_url(html: str) -> str:
    return "data:text/html," + urllib.parse.quote(html)


def sc(result: Any) -> Any:
    """The structured content of a successful call; fails the test on an error result."""
    assert not result.is_error, result.content
    return result.structured_content


async def with_browser(scenario: Callable[[Client], Awaitable[Any]]) -> Any:
    from zendriver_mcp.tools import mcp

    async with Client(mcp, mode="2026-07-28") as c:
        sc(await c.call_tool("start_browser", {"window_size": "1200x900"}))
        try:
            return await scenario(c)
        finally:
            await c.call_tool("stop_browser", {})


class Sites:
    """Two local web sites, so an iframe from one inside the other is cross-site.

    `127.0.0.1` and `localhost` are different sites to Chrome, so a page from one
    embedding a page from the other gets an out-of-process iframe, exactly as a
    real third-party embed does. Pages are templates: `{a}` and `{b}` become the
    two sites' base URLs.
    """

    def __init__(self, pages: dict[str, str | bytes]) -> None:
        import http.server
        import mimetypes
        import threading

        self.pages = pages
        sites = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, data: bytes, ctype: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
                path = self.path.split("?")[0]
                body = sites.pages.get(path)
                if body is None:
                    self.send_error(404)
                    return
                if isinstance(body, bytes):
                    data = body
                else:
                    data = body.replace("{a}", sites.a).replace("{b}", sites.b).encode()
                ctype = mimetypes.guess_type(path)[0] or "text/html"
                if ctype.startswith("text/") or ctype.endswith("json"):
                    ctype += "; charset=utf-8"
                self._send(data, ctype)

            def do_POST(self) -> None:  # noqa: N802 - the stdlib's name
                # Echoes the request body back as JSON, for tests of request capture.
                n = int(self.headers.get("Content-Length") or 0)
                self._send(self.rfile.read(n), "application/json")

            def log_message(self, *args: object) -> None:
                pass

        self._servers = [
            http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)
        ]
        self.a = f"http://127.0.0.1:{self._servers[0].server_address[1]}"
        self.b = f"http://localhost:{self._servers[1].server_address[1]}"
        for server in self._servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def close(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
