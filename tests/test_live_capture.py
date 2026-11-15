"""Scrolling, screenshots and PDF, in a real Chrome.

scroll documented its amount as CSS pixels while zendriver's scroll_down reads
it as a percentage of the window height, so scroll(amount=500) moved five
window heights. Full-page screenshots must not use captureBeyondViewport:
measured 2026-11-15, Chrome enlarges the viewport for it and the page receives
a resize event, so the page is scrolled and stitched instead, as a person
scrolls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.live_harness import data_url, live, sc, with_browser

pytestmark = live

TALL = """<!doctype html><title>tall</title><body style="margin:0">
<div style="height:2600px;background:linear-gradient(#f00,#00f)"></div>
<button id="b" style="position:absolute;top:1200px;left:50px;width:160px;height:40px">Target</button>
<script>
window.ev = [];
addEventListener('resize', () => ev.push('resize'));
new ResizeObserver(() => ev.push('ro')).observe(document.documentElement);
</script>"""


async def _y(c: Any) -> float:
    return float(sc(await c.call_tool("execute_javascript", {"script": "return scrollY"}))["value"])


async def _scenario(c: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sc(await c.call_tool("navigate", {"url": data_url(TALL), "settle": 1}))
    sc(await c.call_tool("scroll", {"direction": "down", "amount": 300}))
    out["after_300"] = await _y(c)
    sc(await c.call_tool("scroll", {"direction": "up", "amount": 100}))
    out["after_up_100"] = await _y(c)
    # Clear what loading recorded (the first ResizeObserver callback), then capture.
    sc(await c.call_tool("execute_javascript", {"script": "ev.length = 0"}))
    out["full"] = sc(await c.call_tool("screenshot", {"full_page": True, "full_resolution": True}))
    out["client_width"] = sc(
        await c.call_tool(
            "execute_javascript", {"script": "return document.documentElement.clientWidth"}
        )
    )["value"]
    out["after_full"] = await _y(c)
    out["element"] = sc(
        await c.call_tool("screenshot", {"selector": "#b", "full_resolution": True})
    )
    out["events"] = sc(await c.call_tool("execute_javascript", {"script": "return ev"}))["value"]
    out["pdf"] = sc(await c.call_tool("call_tool", {"name": "save_pdf", "arguments": {}}))
    return out


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    return asyncio.run(with_browser(_scenario))


def test_scroll_amount_is_css_pixels(run: dict[str, Any]) -> None:
    assert abs(run["after_300"] - 300) <= 5, run["after_300"]
    assert abs(run["after_up_100"] - 200) <= 5, run["after_up_100"]


def test_full_page_covers_the_whole_page(run: dict[str, Any]) -> None:
    full = run["full"]
    # The page's own width: the stitched image leaves the scrollbar out.
    scale = full["full_width"] / run["client_width"]
    assert abs(full["full_height"] - 2600 * scale) <= 4 * scale, full


def test_full_page_is_not_observable_and_restores_the_scroll(run: dict[str, Any]) -> None:
    assert "resize" not in run["events"] and "ro" not in run["events"], run["events"]
    assert abs(run["after_full"] - run["after_up_100"]) <= 5


def test_element_screenshot_is_the_element(run: dict[str, Any]) -> None:
    el = run["element"]
    scale = el["full_width"] / 160
    assert abs(el["full_height"] - 40 * scale) <= 2 * scale, el


def test_pdf_is_a_pdf(run: dict[str, Any]) -> None:
    path = Path(run["pdf"]["files"][0]["path"])
    assert path.read_bytes()[:5] == b"%PDF-"
