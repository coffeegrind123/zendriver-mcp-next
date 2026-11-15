"""Waiting on what single-page apps actually do, in a real Chrome.

wait_for_element could only wait for something to appear; there was no way to
wait for a spinner to go, for a message without a stable selector, or for a
client-side route change, so agents fell back to fixed sleeps.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.live_harness import data_url, live, sc, with_browser

pytestmark = live

PAGE = """<!doctype html><title>spa</title>
<div id="spinner">Loading...</div><div id="panel">Panel</div>
<script>
setTimeout(() => document.getElementById('spinner').remove(), 600);
setTimeout(() => { document.getElementById('panel').style.display = 'none'; }, 600);
setTimeout(() => { document.body.append('Order confirmed'); }, 900);
setTimeout(() => history.pushState({}, '', '#/checkout/done?order=42'), 1200);
</script>"""


async def _scenario(c: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sc(await c.call_tool("navigate", {"url": data_url(PAGE), "settle": 0}))
    call = lambda name, args: c.call_tool("call_tool", {"name": name, "arguments": args})  # noqa: E731
    out["detached"] = sc(
        await c.call_tool(
            "wait_for_element", {"selector": "#spinner", "state": "detached", "timeout": 5}
        )
    )
    out["hidden"] = sc(
        await c.call_tool(
            "wait_for_element", {"selector": "#panel", "state": "hidden", "timeout": 5}
        )
    )
    out["text"] = sc(await call("wait_for_text", {"text": "Order confirmed", "timeout": 5}))
    out["gone"] = sc(
        await call("wait_for_text", {"text": "Loading...", "gone": True, "timeout": 5})
    )
    out["url"] = sc(await call("wait_for_url", {"pattern": "/order=\\d+/", "timeout": 5}))
    out["url_timeout"] = (
        await call("wait_for_url", {"pattern": "never-there", "timeout": 1})
    ).structured_content
    out["still"] = (
        await c.call_tool(
            "wait_for_element", {"selector": "title", "state": "detached", "timeout": 1}
        )
    ).structured_content
    return out


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    return asyncio.run(with_browser(_scenario))


def test_waits_for_things_to_go(run: dict[str, Any]) -> None:
    assert run["detached"]["found"] is False and run["detached"]["state"] == "detached"
    assert run["hidden"]["found"] is True and run["hidden"]["state"] == "hidden"


def test_waits_for_text_and_its_absence(run: dict[str, Any]) -> None:
    assert run["text"]["text"] == "Order confirmed" and run["text"]["gone"] is False
    assert run["gone"]["gone"] is True


def test_waits_for_a_client_side_route(run: dict[str, Any]) -> None:
    assert run["url"]["url"].endswith("#/checkout/done?order=42")


def test_timeouts_say_what_is_still_true(run: dict[str, Any]) -> None:
    assert run["url_timeout"]["code"] == "timeout"
    assert run["still"]["code"] == "timeout"
    assert "still in the page" in run["still"]["error"]
