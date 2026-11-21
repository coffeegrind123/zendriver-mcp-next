"""find_elements against a real page: the same elements the tree walks, ranked."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from tests.live_harness import data_url, live, sc, with_browser

pytestmark = live

PAGE = """
<header><a href="/login">Sign in</a> <a href="/cart">Cart</a></header>
<main>
  <form name="login"><input name="email" type="email" placeholder="Email address">
    <button>Sign in</button></form>
  <x-shadow></x-shadow>
  <iframe srcdoc="<button>Subscribe to newsletter</button>" style="width:300px;height:60px"></iframe>
  <section id="deals"><button>Add to cart</button></section>
</main>
<script>
customElements.define('x-shadow', class extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: 'open'}).innerHTML = '<button>Shadow checkout</button>'; }
});
</script>"""


def _first(result: dict[str, Any]) -> dict[str, Any]:
    assert result["matches"], result
    return result["matches"][0]


async def _scenario(c: Any) -> dict[str, Any]:
    sc(await c.call_tool("navigate", {"url": data_url(PAGE), "settle": 1}))
    out: dict[str, Any] = {}
    for key, args in {
        "header_link": {"description": "Sign in link in header"},
        "form_button": {"description": "sign in button"},
        "email": {"description": "email input"},
        "shadow": {"description": "checkout"},
        "frame": {"description": "subscribe newsletter"},
        "within": {"description": "cart", "within": "#deals"},
        "none": {"description": "unsubscribe forever"},
    }.items():
        out[key] = sc(await c.call_tool("find_elements", args))
    found = _first(out["form_button"])
    sc(await c.call_tool("click", {"selector": str(found["id"])}))
    if os.environ.get("ZENDRIVER_MCP_LIVE_FOLATE") == "1":
        sc(await c.call_tool("navigate", {"url": data_url(PAGE), "settle": 1}))
        out["folate"] = sc(
            await c.call_tool("find_elements", {"description": "log in", "ranker": "folate"})
        )
    return out


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    return asyncio.run(with_browser(_scenario))


def test_kind_and_region_pick_between_equal_labels(run: dict[str, Any]) -> None:
    link = _first(run["header_link"])
    assert (link["t"], link["r"]) == ("link", "hdr")
    button = _first(run["form_button"])
    assert (button["t"], button["r"]) == ("btn", "form:login")


def test_input_found_by_type_and_placeholder(run: dict[str, Any]) -> None:
    assert _first(run["email"])["l"] == "Email address"


def test_shadow_dom_and_iframes_are_searched(run: dict[str, Any]) -> None:
    assert _first(run["shadow"])["l"] == "Shadow checkout"
    frame = _first(run["frame"])
    assert frame["l"] == "Subscribe to newsletter" and frame.get("f") == 1


def test_within_limits_the_search(run: dict[str, Any]) -> None:
    labels = [m["l"] for m in run["within"]["matches"]]
    assert labels == ["Add to cart"], labels


def test_no_match_is_an_empty_success_that_says_what_to_do(run: dict[str, Any]) -> None:
    assert run["none"]["matches"] == []
    assert "view='summary'" in run["none"]["summary"]


@pytest.mark.skipif(
    os.environ.get("ZENDRIVER_MCP_LIVE_FOLATE") != "1",
    reason="needs the folate extra and its model download (ZENDRIVER_MCP_LIVE_FOLATE=1)",
)
def test_folate_matches_meaning_not_words(run: dict[str, Any]) -> None:
    assert _first(run["folate"])["l"] == "Sign in"
    assert run["folate"]["ranker"] == "folate"
