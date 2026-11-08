"""Interception rules are per tab, and a paused request is answered on its own tab.

The regression this guards: the handler used to answer on whatever tab was
ACTIVE, so a request paused in a background tab got its continue sent to the
wrong target and hung forever.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from zendriver import cdp

from zendriver_mcp.errors import ZendriverMCPError
from zendriver_mcp.session import CALL_TAB, BrowserSession
from zendriver_mcp.tools.interception import InterceptionTools


class _Tab:
    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[str] = []
        self.handlers: dict[Any, list[Any]] = {}

    def add_handler(self, event: Any, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)

    async def send(self, cmd: Any) -> None:
        method = next(cmd)["method"]
        self.sent.append(method)


def _paused(url: str, rid: str = "r1") -> cdp.fetch.RequestPaused:
    return cdp.fetch.RequestPaused.from_json(
        {
            "requestId": rid,
            "request": {
                "url": url,
                "method": "GET",
                "headers": {},
                "initialPriority": "High",
                "referrerPolicy": "no-referrer",
            },
            "frameId": "f",
            "resourceType": "XHR",
        }
    )


@pytest.fixture()
def setup() -> Any:
    tools = InterceptionTools(MCPServer("t"))
    s = BrowserSession.get_instance()
    a, b = _Tab("a"), _Tab("b")
    s._tabs = {"tab_1": a, "tab_2": b}  # type: ignore[assignment]
    s._page = a  # type: ignore[assignment]
    yield tools, s, a, b
    s._tabs = {}
    s._page = None


def test_rule_belongs_to_the_call_tab_and_enables_fetch_there(setup: Any) -> None:
    tools, _s, a, b = setup
    token = CALL_TAB.set(b)
    try:
        out = asyncio.run(tools.mock_response(url_pattern="*api*", status=418))
    finally:
        CALL_TAB.reset(token)
    assert out.rule.tab == "tab_2"
    assert "Fetch.enable" in b.sent and "Fetch.enable" not in a.sent


def test_paused_request_is_answered_on_its_own_tab(setup: Any) -> None:
    tools, s, a, b = setup
    token = CALL_TAB.set(b)
    try:
        asyncio.run(tools.fail_requests(url_pattern="*api*", error_reason="TimedOut"))
    finally:
        CALL_TAB.reset(token)
    handler = b.handlers[cdp.fetch.RequestPaused][0]
    # The ACTIVE tab is a; the request paused in b must be answered in b.
    assert s._page is a
    asyncio.run(handler(_paused("https://x.test/api/1")))
    asyncio.run(handler(_paused("https://x.test/other", "r2")))
    assert b.sent[-2:] == ["Fetch.failRequest", "Fetch.continueRequest"]
    assert not any(m.startswith("Fetch.") and m != "Fetch.enable" for m in a.sent)
    listed = asyncio.run(tools.list_interceptions())
    assert listed.rules[0].match_count == 1


def test_removing_last_rule_on_a_tab_disables_fetch_there(setup: Any) -> None:
    tools, _s, a, _b = setup
    added = asyncio.run(tools.mock_response(url_pattern="*", status=200))
    out = asyncio.run(tools.stop_interception(rule_id=added.rule.id))
    assert out.removed == [added.rule.id] and out.active_rules == 0
    assert a.sent[-1] == "Fetch.disable"


def test_unknown_rule_id_suggests_the_close_one(setup: Any) -> None:
    tools, *_ = setup
    asyncio.run(tools.mock_response(url_pattern="*", status=200))
    with pytest.raises(ZendriverMCPError) as exc:
        asyncio.run(tools.stop_interception(rule_id="rule_01"))
    assert "rule_001" in (exc.value.hint or "")


def test_bad_status_is_refused(setup: Any) -> None:
    tools, *_ = setup
    with pytest.raises(ZendriverMCPError):
        asyncio.run(tools.mock_response(url_pattern="*", status=42))
