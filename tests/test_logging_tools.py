"""Log tools: typed entries, per-tab filtering, waits that report and time out.

No browser: the session's log buffers are filled by hand, which is all these
tools read.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from src.session import CALL_TAB, BrowserSession
from src.tools.logging import LoggingTools


class _Tab:
    """Stands in for a zendriver Tab; only identity and wait() are used."""

    async def wait(self, t: float) -> None:
        await asyncio.sleep(t)


@pytest.fixture()
def tools() -> Any:
    server = MCPServer("t")
    inst = LoggingTools(server)
    s = BrowserSession.get_instance()
    a, b = _Tab(), _Tab()
    s._tabs = {"tab_1": a, "tab_2": b}  # type: ignore[assignment]
    s._page = a  # type: ignore[assignment]
    s._network_logs = [
        {
            "url": "https://x.test/a",
            "method": "GET",
            "status": 200,
            "type": "Document",
            "tab": "tab_1",
        },
        {
            "url": "https://y.test/api",
            "method": "POST",
            "status": 201,
            "type": "Fetch",
            "tab": "tab_2",
        },
        {
            "url": "https://x.test/fail",
            "method": "GET",
            "status": 0,
            "status_text": "FAILED: x",
            "tab": "tab_1",
        },
    ]
    s._console_logs = [
        {"type": "error", "text": "boom", "tab": "tab_2"},
        {"type": "log", "text": "hi", "tab": "tab_1"},
    ]
    yield server, inst, s, a, b
    s._tabs = {}
    s._page = None
    s._network_logs = []
    s._console_logs = []


def test_entries_from_every_tab_carry_their_tab(tools: Any) -> None:
    _, inst, *_ = tools
    out = asyncio.run(inst.get_network_logs(limit=10))
    assert [e.tab for e in out.entries] == ["tab_1", "tab_2", "tab_1"]
    assert out.tab_filter is None
    assert out.entries[2].status == 0 and out.entries[2].status_text == "FAILED: x"


def test_passing_tab_limits_entries_to_that_tab(tools: Any) -> None:
    _, inst, _s, _a, b = tools
    token = CALL_TAB.set(b)
    try:
        net = asyncio.run(inst.get_network_logs(limit=10))
        con = asyncio.run(inst.get_console_logs(limit=10))
    finally:
        CALL_TAB.reset(token)
    assert [e.url for e in net.entries] == ["https://y.test/api"]
    assert net.tab_filter == "tab_2"
    assert [e.text for e in con.entries] == ["boom"]


def test_limit_keeps_the_most_recent(tools: Any) -> None:
    _, inst, *_ = tools
    out = asyncio.run(inst.get_network_logs(limit=1))
    assert [e.url for e in out.entries] == ["https://x.test/fail"]


def test_wait_for_request_finds_an_already_captured_request(tools: Any) -> None:
    server, *_ = tools

    async def go() -> Any:
        async with Client(server) as c:
            return await c.call_tool("wait_for_request", {"url_pattern": "/API", "timeout": 2})

    r = asyncio.run(go())
    assert not r.is_error
    assert r.structured_content["request"]["method"] == "POST"


def test_wait_for_request_timeout_is_a_coded_error(tools: Any) -> None:
    server, *_ = tools

    async def go() -> Any:
        async with Client(server) as c:
            return await c.call_tool("wait_for_request", {"url_pattern": "/never", "timeout": 0.3})

    r = asyncio.run(go())
    assert r.is_error
    assert r.structured_content["code"] == "timeout"
    assert "get_network_logs" in r.structured_content["hint"]


def test_wait_for_request_method_filter(tools: Any) -> None:
    server, *_ = tools

    async def go() -> Any:
        async with Client(server) as c:
            return await c.call_tool(
                "wait_for_request", {"url_pattern": "/api", "method": "get", "timeout": 0.3}
            )

    assert asyncio.run(go()).is_error


def test_schemas_hide_ctx_and_expose_tab(tools: Any) -> None:
    server, *_ = tools

    async def go() -> Any:
        async with Client(server) as c:
            return (await c.list_tools()).tools

    by_name = {t.name: t for t in asyncio.run(go())}
    props = by_name["wait_for_network"].input_schema["properties"]
    assert "ctx" not in props and "tab" in props
    assert "tab" not in by_name["clear_logs"].input_schema["properties"]
    assert by_name["clear_logs"].annotations.destructive_hint is True
    assert by_name["get_network_logs"].annotations.read_only_hint is True
    for t in by_name.values():
        assert t.output_schema is not None, t.name
