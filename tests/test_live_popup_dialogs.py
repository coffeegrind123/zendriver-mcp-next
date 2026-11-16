"""A dialog in a tab the page opened itself is seen before any tool touches it.

Listeners were set up on a popup only when a tool first acted on it, so an alert
the popup raised on load was never recorded, and the popup sat frozen with
nothing saying why.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.live_harness import Sites, live, sc, with_browser

pytestmark = live

OPENER = """<!doctype html><title>opener</title>
<button id="open" onclick="window.open('/popup.html', '_blank')">Open</button>"""
POPUP = """<!doctype html><title>popup</title>
<script>setTimeout(() => alert('Popup says hi'), 800);</script>"""


async def _scenario(c: Any, sites: Sites) -> dict[str, Any]:
    sc(await c.call_tool("navigate", {"url": f"{sites.a}/opener.html", "settle": 1}))
    sc(await c.call_tool("click", {"selector": "#open"}))
    await asyncio.sleep(2.5)
    return sc(await c.call_tool("call_tool", {"name": "list_dialogs", "arguments": {}}))


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    sites = Sites({"/opener.html": OPENER, "/popup.html": POPUP})
    try:
        return asyncio.run(with_browser(lambda c: _scenario(c, sites)))
    finally:
        sites.close()


def test_a_popup_alert_is_recorded_without_touching_the_popup(run: dict[str, Any]) -> None:
    messages = [d["message"] for d in run["dialogs"]]
    assert "Popup says hi" in messages, run
