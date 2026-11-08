"""Protocol-level checks: the server answers both protocol eras.

The 2026-07-28 revision has no initialize handshake (a client probes
``server/discover`` and sends the version on every request); 2025-era clients
still do the handshake. SDK 2 serves both from one server, and this test is the
proof that ours does: each mode lists tools and completes a tool call that needs
no browser.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp import Client

from src.tools import mcp

MODES = ["legacy", "2026-07-28"]


async def _list_and_call(mode: str) -> tuple[int, str]:
    async with Client(mcp, mode=mode) as client:
        listed = await client.list_tools()
        result = await client.call_tool("get_browser_status", {})
        text = "".join(getattr(c, "text", "") for c in result.content)
        return len(listed.tools), text


@pytest.mark.parametrize("mode", MODES)
def test_lists_tools_and_calls_one(mode: str) -> None:
    count, text = asyncio.run(_list_and_call(mode))
    assert count > 0
    assert "Not started" in text
