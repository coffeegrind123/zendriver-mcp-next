"""A click on an element that must first be scrolled to lands on it.

scrollIntoViewIfNeeded can scroll smoothly (a page's scroll-behavior: smooth,
or an iframe's position settling after its parent scrolls), and the element's
position was read once, straight after asking for the scroll, so the click
went to where the element had been. Seen on CI as a click into a cross-site
iframe further down the page that never arrived.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.live_harness import data_url, live, sc, with_browser

pytestmark = live

PAGE = """<!doctype html><title>smooth</title>
<style>html { scroll-behavior: smooth }</style>
<body style="margin:0"><div style="height:4000px"></div>
<button id="far" onclick="document.title = 'hit'">Far away</button>
<div style="height:2000px"></div>
<script>window.clicks = []; addEventListener('click', (e) => clicks.push(e.target.id || e.target.tagName), true);</script>"""


async def _scenario(c: Any) -> dict[str, Any]:
    sc(await c.call_tool("navigate", {"url": data_url(PAGE), "settle": 1}))
    sc(await c.call_tool("click", {"selector": "#far"}))
    return sc(
        await c.call_tool(
            "execute_javascript", {"script": "return {title: document.title, clicks}"}
        )
    )["value"]


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    return asyncio.run(with_browser(_scenario))


def test_click_lands_after_a_smooth_scroll(run: dict[str, Any]) -> None:
    assert run["clicks"] == ["far"], run
    assert run["title"] == "hit"
