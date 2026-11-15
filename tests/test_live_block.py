"""block_resources, in a real Chrome: images and fonts blocked, the page loads."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest

from tests.live_harness import Sites, live, sc, with_browser

pytestmark = live

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=="
)
PAGE = """<!doctype html><title>pics</title><p>Text stays</p>
<img id="i" src="/pic.png"><script src="/app.js"></script>"""


async def _scenario(c: Any, sites: Sites) -> dict[str, Any]:
    out: dict[str, Any] = {}
    call = lambda name, args: c.call_tool("call_tool", {"name": name, "arguments": args})  # noqa: E731
    sc(await c.call_tool("navigate", {"url": f"{sites.a}/blank.html", "settle": 1}))
    out["rule"] = sc(await call("block_resources", {"types": ["Image"]}))
    sc(await c.call_tool("navigate", {"url": f"{sites.a}/pics.html", "settle": 2}))
    out["page"] = sc(
        await c.call_tool(
            "execute_javascript",
            {
                "script": "return {w: document.getElementById('i').naturalWidth, js: window.ran === true, text: document.body.innerText}"
            },
        )
    )["value"]
    out["list"] = sc(await call("list_interceptions", {}))
    return out


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    sites = Sites(
        {
            "/blank.html": "<title>b</title>",
            "/pics.html": PAGE,
            "/pic.png": PNG,
            "/app.js": "window.ran = true;",
        }
    )
    try:
        return asyncio.run(with_browser(lambda c: _scenario(c, sites)))
    finally:
        sites.close()


def test_images_are_blocked_and_the_rest_loads(run: dict[str, Any]) -> None:
    page = run["page"]
    assert page["w"] == 0, "the image was blocked"
    assert page["js"] is True, "scripts are not a blocked type here"
    assert "Text stays" in page["text"]


def test_the_rule_is_listed_with_its_types_and_matches(run: dict[str, Any]) -> None:
    (rule,) = run["list"]["rules"]
    assert rule["resource_types"] == ["Image"]
    assert rule["match_count"] >= 1
