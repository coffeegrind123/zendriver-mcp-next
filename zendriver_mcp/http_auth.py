"""Security for the HTTP transport: tokens, Host/Origin checks, rate limits.

stdio needs none of this: the client spawned the process and owns its pipes.
Over HTTP anything that can reach the port can drive the browser, which holds
the user's sessions, so:

* **Bearer tokens.** ``run.py --create-token NAME`` prints a token once; only
  its SHA-256 is stored (``tokens.json`` in the config dir, mode 0600).
  Comparison is constant-time. Each token's name becomes the caller identity
  (``zendriver_mcp.session.CALLER``), so one client cannot reach another's tabs.
* **No tokens, loopback only.** With no token file the server still starts on a
  loopback address (local development), and refuses to start on any other.
* **Host and Origin allow-lists** against DNS rebinding: a web page in the
  user's own browser must not be able to POST to the port. The SDK's
  TransportSecuritySettings does the checking; this module supplies the lists.
* **Rate limit** per token, fixed one-minute window, 429 with Retry-After.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from zendriver_mcp.session import CALLER

TOKEN_PREFIX = "zmcp_"
TOKEN_BYTES = 32
DEFAULT_RATE_PER_MINUTE = 600
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
# Docker Desktop's name for the host, so a container can reach a host server.
EXTRA_LOCAL_HOSTS = ("host.docker.internal",)


def config_dir() -> Path:
    base = os.environ.get("ZENDRIVER_MCP_CONFIG_DIR")
    if base:
        return Path(base).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "zendriver-mcp"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class TokenStore:
    path: Path
    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> TokenStore:
        p = path or config_dir() / "tokens.json"
        if not p.exists():
            return cls(p)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(p, data.get("tokens", {}))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tokens": self.tokens}, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def create(self, name: str) -> str:
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Token names are letters, digits, '-' and '_'.")
        if name in self.tokens:
            raise ValueError(f"A token named {name!r} exists; revoke it first.")
        raw = base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).decode().rstrip("=")
        token = TOKEN_PREFIX + raw
        self.tokens[name] = {"sha256": _hash(token), "created": int(time.time())}
        self.save()
        return token

    def revoke(self, name: str) -> bool:
        if self.tokens.pop(name, None) is None:
            return False
        self.save()
        return True

    def identify(self, token: str) -> str | None:
        """The name of the token, or None. Compares every entry, constant-time."""
        digest = _hash(token)
        found = None
        for name, entry in self.tokens.items():
            if hmac.compare_digest(digest, entry.get("sha256", "")):
                found = name
        return found


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS or host.startswith("127.")


def transport_security(bind_host: str, extra_hosts: list[str]) -> TransportSecuritySettings:
    """Host/Origin allow-lists for the bound address plus local names."""
    hosts = set(LOOPBACK_HOSTS) | set(EXTRA_LOCAL_HOSTS) | {bind_host} | set(extra_hosts)
    hosts.discard("0.0.0.0")
    hosts.discard("::")
    allowed_hosts = sorted({h for h in hosts} | {f"{h}:*" for h in hosts})
    allowed_origins = sorted(
        {f"{scheme}://{h}" for h in hosts for scheme in ("http", "https")}
        | {f"{scheme}://{h}:*" for h in hosts for scheme in ("http", "https")}
    )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


async def _respond(
    send: Send, status: int, body: dict[str, Any], headers: list[tuple[bytes, bytes]]
) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), *headers],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class AuthMiddleware:
    """Bearer-token check, caller identity and rate limiting for the MCP path."""

    def __init__(
        self,
        app: ASGIApp,
        store: TokenStore,
        *,
        require: bool,
        rate_per_minute: int = DEFAULT_RATE_PER_MINUTE,
    ) -> None:
        self.app = app
        self.store = store
        self.require = require
        self.rate = rate_per_minute
        self._window: dict[str, tuple[int, int]] = {}

    def _allow(self, who: str) -> int | None:
        """None if under the limit, else seconds until the window resets."""
        now = int(time.time())
        minute = now // 60
        win, count = self._window.get(who, (minute, 0))
        if win != minute:
            win, count = minute, 0
        count += 1
        self._window[who] = (win, count)
        if count > self.rate:
            return 60 - now % 60
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        who: str | None = None
        if self.require:
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode("latin-1")
            scheme, _, token = auth.partition(" ")
            who = self.store.identify(token.strip()) if scheme.lower() == "bearer" else None
            if who is None:
                await _respond(
                    send,
                    401,
                    {
                        "error": "invalid_token",
                        "error_description": "Missing or unknown bearer token.",
                    },
                    [(b"www-authenticate", b'Bearer realm="zendriver-mcp", error="invalid_token"')],
                )
                return

        wait = self._allow(who or "anonymous")
        if wait is not None:
            await _respond(
                send,
                429,
                {"error": "rate_limited", "retry_after": wait},
                [(b"retry-after", str(wait).encode())],
            )
            return

        token = CALLER.set(who)
        try:
            await self.app(scope, receive, send)
        finally:
            CALLER.reset(token)
