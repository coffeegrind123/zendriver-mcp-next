"""open_tabs: several pages loading at once, in a real Chrome."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from tests.live_harness import Sites, live, sc, with_browser

pytestmark = live


async def _scenario(c: Any, sites: Sites) -> dict[str, Any]:
    sc(await c.call_tool("navigate", {"url": f"{sites.a}/home.html", "settle": 1}))
    urls = [f"{sites.a}/p{i}.html" for i in range(4)] + ["http://127.0.0.1:9/refused"]
    t = time.monotonic()
    opened = sc(await c.call_tool("call_tool", {"name": "open_tabs", "arguments": {"urls": urls}}))
    elapsed = time.monotonic() - t
    titles = []
    for entry in opened["tabs"][:4]:
        info = sc(
            await c.call_tool(
                "call_tool", {"name": "get_page_info", "arguments": {"tab": entry["tab"]}}
            )
        )
        titles.append(info["title"])
    active = sc(await c.call_tool("call_tool", {"name": "get_page_info", "arguments": {}}))
    return {"opened": opened, "titles": titles, "active": active["title"], "elapsed": elapsed}


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    pages: dict[str, str | bytes] = {"/home.html": "<title>home</title>"}
    pages.update({f"/p{i}.html": f"<title>page {i}</title>" for i in range(4)})
    sites = Sites(pages)
    try:
        return asyncio.run(with_browser(lambda c: _scenario(c, sites)))
    finally:
        sites.close()


def test_each_url_gets_its_own_tab_in_order(run: dict[str, Any]) -> None:
    assert run["titles"] == ["page 0", "page 1", "page 2", "page 3"]
    assert run["opened"]["opened"] == 4


def test_a_failing_url_is_reported_not_raised(run: dict[str, Any]) -> None:
    last = run["opened"]["tabs"][-1]
    assert last["url"].endswith("/refused")
    assert last.get("error") == "the page could not be loaded", last
    assert last.get("tab"), "Chrome still opened a tab, on its error page"


def test_the_active_tab_does_not_change(run: dict[str, Any]) -> None:
    assert run["active"] == "home"
