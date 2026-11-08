# Runner script for Zendriver MCP server
import argparse
import os
import sys

# Add the project directory to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.session import BrowserSession
from src.tools import mcp

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-path", help="Path to browser executable")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help=(
            "stdio (default) spawns one server per client, so the browser dies with the "
            "client. streamable-http keeps ONE long-lived server that many clients — or "
            "many separate CLI invocations — share, which is what a stateful browser needs."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    parser.add_argument("--port", type=int, default=8931, help="HTTP port")
    parser.add_argument(
        "--stateful",
        action="store_true",
        help=(
            "Keep per-client MCP sessions (requires the initialize handshake on every "
            "connection). Default is stateless: any client can POST a single tools/call "
            "and get an answer, which is what makes a shell wrapper practical. The BROWSER "
            "is process-global either way — stateless refers to the MCP session, not the tab."
        ),
    )
    args = parser.parse_args()

    if args.browser_path:
        BrowserSession.default_browser_path = args.browser_path

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "sse":
        # The pre-2025-03-26 HTTP+SSE transport, kept for old clients only.
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        # Passed to run() rather than set on mcp.settings: SDK 2 moved the
        # transport options off the server's settings and onto run()'s keywords.
        mcp.run(
            transport=args.transport,
            host=args.host,
            port=args.port,
            stateless_http=not args.stateful,
            # Plain JSON instead of an SSE frame per response: every HTTP client
            # can read it, including curl and the stdlib.
            json_response=True,
        )
