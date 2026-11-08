"""Command line: run the server, or manage HTTP tokens."""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zendriver-mcp", description=__doc__)
    p.add_argument("--browser-path", help="Path to the Chrome executable")
    p.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help=(
            "stdio (default) spawns one server per client, so the browser dies with the "
            "client. streamable-http keeps ONE long-lived server that many clients share. "
            "sse is the pre-2025-03-26 HTTP transport, for old clients only."
        ),
    )
    p.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    p.add_argument("--port", type=int, default=8931, help="HTTP port")
    p.add_argument(
        "--stateful",
        action="store_true",
        help=(
            "Keep per-client sessions for 2025-era clients (2026-07-28 clients are "
            "always stateless). The BROWSER is process-global either way."
        ),
    )
    p.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="Extra Host/Origin name to accept (repeatable), e.g. a LAN hostname.",
    )
    p.add_argument(
        "--rate-limit",
        type=int,
        default=None,
        help="Requests per minute per token (default 600).",
    )
    tokens = p.add_argument_group("HTTP tokens (stored hashed in the config dir)")
    tokens.add_argument("--create-token", metavar="NAME", help="Create a token and print it once")
    tokens.add_argument("--revoke-token", metavar="NAME", help="Revoke a token")
    tokens.add_argument("--list-tokens", action="store_true", help="List token names")
    return p


def _token_command(args: argparse.Namespace) -> int | None:
    from src.http_auth import TokenStore

    store = TokenStore.load()
    if args.create_token:
        try:
            token = store.create(args.create_token)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        print(token)
        print(
            f"Saved (hashed) to {store.path}. Send it as 'Authorization: Bearer <token>'.",
            file=sys.stderr,
        )
        return 0
    if args.revoke_token:
        if store.revoke(args.revoke_token):
            print(f"Revoked {args.revoke_token}.")
            return 0
        print(f"No token named {args.revoke_token}.", file=sys.stderr)
        return 1
    if args.list_tokens:
        for name in sorted(store.tokens):
            print(name)
        return 0
    return None


def _serve_http(args: argparse.Namespace) -> int:
    import uvicorn

    from src.http_auth import (
        DEFAULT_RATE_PER_MINUTE,
        AuthMiddleware,
        TokenStore,
        is_loopback,
        transport_security,
    )
    from src.tools import mcp

    store = TokenStore.load()
    require = bool(store.tokens)
    if not require and not is_loopback(args.host):
        print(
            f"Refusing to serve on {args.host} without tokens: anyone who can reach the port "
            "could drive the browser. Create one with --create-token NAME, or bind 127.0.0.1.",
            file=sys.stderr,
        )
        return 2
    if not require:
        logger.warning("No HTTP tokens configured: serving unauthenticated on loopback only.")

    app = mcp.streamable_http_app(
        stateless_http=not args.stateful,
        # Plain JSON instead of an SSE frame per response when nothing streams:
        # every HTTP client can read it, including curl and the stdlib.
        json_response=True,
        transport_security=transport_security(args.host, args.allowed_host),
        host=args.host,
    )
    secured = AuthMiddleware(
        app,
        store,
        require=require,
        rate_per_minute=args.rate_limit or DEFAULT_RATE_PER_MINUTE,
    )
    uvicorn.run(secured, host=args.host, port=args.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    code = _token_command(args)
    if code is not None:
        return code

    from src.session import BrowserSession
    from src.tools import mcp

    if args.browser_path:
        BrowserSession.default_browser_path = args.browser_path

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return 0
    if args.transport == "sse":
        if args.host not in ("127.0.0.1", "localhost"):
            print("The legacy sse transport has no authentication: loopback only.", file=sys.stderr)
            return 2
        mcp.run(transport="sse", host=args.host, port=args.port)
        return 0
    return _serve_http(args)


if __name__ == "__main__":
    sys.exit(main())
