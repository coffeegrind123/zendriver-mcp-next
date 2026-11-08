"""HTTP transport security: tokens, identity, rate limits, Host/Origin lists."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from zendriver_mcp.http_auth import AuthMiddleware, TokenStore, is_loopback, transport_security
from zendriver_mcp.session import CALLER


def test_tokens_are_stored_hashed_and_identify(tmp_path: Path) -> None:
    store = TokenStore.load(tmp_path / "tokens.json")
    token = store.create("laptop")
    raw = (tmp_path / "tokens.json").read_text()
    assert token not in raw
    assert token.startswith("zmcp_")
    assert TokenStore.load(tmp_path / "tokens.json").identify(token) == "laptop"
    assert store.identify(token + "x") is None
    assert (tmp_path / "tokens.json").stat().st_mode & 0o777 == 0o600


def test_duplicate_and_bad_names_are_refused(tmp_path: Path) -> None:
    store = TokenStore.load(tmp_path / "t.json")
    store.create("a")
    with pytest.raises(ValueError):
        store.create("a")
    with pytest.raises(ValueError):
        store.create("bad name")
    assert store.revoke("a") and not store.revoke("a")


def _call(mw: AuthMiddleware, headers: dict[str, str]) -> tuple[int, dict, str | None]:
    seen: dict[str, str | None] = {}
    sent: list[dict] = []

    async def app(scope, receive, send):
        seen["caller"] = CALLER.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw.app = app

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    asyncio.run(mw(scope, receive, send))
    status = sent[0]["status"]
    body = json.loads(sent[1]["body"] or b"{}")
    return status, body, seen.get("caller")


def test_middleware_requires_a_known_token(tmp_path: Path) -> None:
    store = TokenStore.load(tmp_path / "t.json")
    token = store.create("agent1")
    mw = AuthMiddleware(lambda *a: None, store, require=True)
    assert _call(mw, {})[0] == 401
    assert _call(mw, {"Authorization": "Bearer nope"})[0] == 401
    status, _body, caller = _call(mw, {"Authorization": f"Bearer {token}"})
    assert status == 200
    assert caller == "agent1", "the token's name must become the caller identity"


def test_rate_limit_answers_429(tmp_path: Path) -> None:
    store = TokenStore.load(tmp_path / "t.json")
    token = store.create("x")
    mw = AuthMiddleware(lambda *a: None, store, require=True, rate_per_minute=2)
    h = {"Authorization": f"Bearer {token}"}
    assert [_call(mw, h)[0] for _ in range(3)] == [200, 200, 429]


def test_host_lists_cover_loopback_and_extras() -> None:
    s = transport_security("127.0.0.1", ["devbox.lan"])
    assert "localhost:*" in s.allowed_hosts and "devbox.lan:*" in s.allowed_hosts
    assert "http://localhost:*" in s.allowed_origins
    assert "0.0.0.0" not in s.allowed_hosts
    assert is_loopback("127.0.0.1") and not is_loopback("0.0.0.0")


def test_server_refuses_public_bind_without_tokens(tmp_path: Path, monkeypatch) -> None:
    from zendriver_mcp.server import main

    monkeypatch.setenv("ZENDRIVER_MCP_CONFIG_DIR", str(tmp_path))
    assert main(["--transport", "streamable-http", "--host", "0.0.0.0"]) == 2
