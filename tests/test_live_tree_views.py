"""get_interaction_tree's summary view and within scoping, in a real Chrome."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.live_harness import data_url, live, sc, with_browser

pytestmark = live

GUARD = """<script>
window.log = [];
addEventListener('load', () => new MutationObserver((ms) => ms.forEach((m) =>
  log.push(m.type + ':' + (m.attributeName || ''))))
  .observe(document, {attributes: true, childList: true, subtree: true}));
</script>"""

NAV = "".join(f'<a href="/n{i}">Nav {i}</a> ' for i in range(5))
CARDS = "".join(
    f'<div><a href="/p{i}">Product {i}</a> <button onclick="window.last={i}">Add to cart</button></div>'
    for i in range(30)
)
SHOP = f"""{GUARD}
<h1>Shop</h1><h2>Deals</h2><h3 style="display:none">Hidden heading</h3>
<nav aria-label="Primary">{NAV}</nav>
<form name="filters">
  <select name="country"><option>Canada</option><option>Mexico</option><option>Peru</option>
    <option>Chile</option><option>Brazil</option><option>Spain</option></select>
  <x-shadow></x-shadow>
</form>
<main id="list">{CARDS}
  <iframe srcdoc="<button>Inside frame</button>" style="width:200px;height:60px"></iframe>
</main>
<script>
customElements.define('x-shadow', class extends HTMLElement {{
  constructor() {{ super(); this.attachShadow({{mode: 'open'}}).innerHTML = '<button>Shadow apply</button>'; }}
}});
</script>"""


async def _scenario(c: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sc(await c.call_tool("navigate", {"url": data_url(SHOP), "settle": 1}))
    summary = sc(await c.call_tool("get_interaction_tree", {"view": "summary"}))
    out["summary"] = summary
    out["list"] = sc(await c.call_tool("get_interaction_tree", {"limit": 500}))

    form_line = next(line for line in summary["regions"] if line.startswith("form:filters"))
    rid = form_line.split("@", 1)[1].split(" ", 1)[0]
    out["form"] = sc(await c.call_tool("get_interaction_tree", {"within": rid}))
    out["main"] = sc(await c.call_tool("get_interaction_tree", {"within": "#list", "limit": 500}))
    out["main_summary"] = sc(
        await c.call_tool("get_interaction_tree", {"within": "#list", "view": "summary"})
    )

    # The folded "Add to cart x30" entry's id is the first card's button.
    main_line = next(line for line in summary["regions"] if line.startswith("main"))
    first = main_line.split("btn: ", 1)[1].split(" ", 1)[0]
    sc(await c.call_tool("click", {"selector": first}))
    out["clicked"] = sc(await c.call_tool("execute_javascript", {"script": "return window.last"}))
    out["log"] = sc(await c.call_tool("execute_javascript", {"script": "return window.log"}))
    missing = await c.call_tool("get_interaction_tree", {"within": "#nope"})
    out["missing"] = missing.structured_content if missing.is_error else None
    return out


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    return asyncio.run(with_browser(_scenario))


def test_summary_folds_repeats_and_keeps_regions(run: dict[str, Any]) -> None:
    s = run["summary"]
    assert s["elements"] == []
    assert s["outline"] == ["h1 Shop", "h2 Deals"]
    regions = s["regions"]
    assert any(r.startswith("nav:Primary @") and "Nav 4" in r for r in regions), regions
    main = next(r for r in regions if r.startswith("main"))
    assert "Add to cart x30" in main
    assert "+22 more" in main, "30 product links, 8 shown"
    form = next(r for r in regions if r.startswith("form:filters"))
    assert "(6 options: Canada, Mexico, Peru, Chile, +2)" in form
    assert "Shadow apply" in form
    assert any(r.startswith("f1:") and "Inside frame" in r for r in regions), regions


def test_summary_is_far_smaller_than_the_list(run: dict[str, Any]) -> None:
    import json

    summary = len(json.dumps(run["summary"]))
    full = len(json.dumps(run["list"]))
    assert summary * 3 < full, (summary, full)


def test_a_folded_id_is_clickable(run: dict[str, Any]) -> None:
    assert run["clicked"]["value"] == 0


def test_within_a_region_id(run: dict[str, Any]) -> None:
    labels = {e["l"] for e in run["form"]["elements"]}
    assert "Shadow apply" in labels
    assert any(e["t"] == "sel" for e in run["form"]["elements"])
    assert not any(label.startswith(("Nav", "Product", "Add")) for label in labels), labels


def test_within_a_selector_takes_its_iframes(run: dict[str, Any]) -> None:
    els = run["main"]["elements"]
    labels = [e["l"] for e in els]
    assert labels.count("Add to cart") == 30
    assert "Inside frame" in labels
    assert not any(label.startswith("Nav") for label in labels)
    assert run["main"]["frames"] and run["main"]["frames"][0]["f"] == 1
    assert all(not r.startswith(("nav", "form")) for r in run["main_summary"]["regions"])


def test_no_view_writes_to_the_page(run: dict[str, Any]) -> None:
    assert run["log"]["value"] == [], run["log"]


def test_within_nothing_is_element_not_found(run: dict[str, Any]) -> None:
    assert run["missing"] and run["missing"]["code"] == "element_not_found", run["missing"]
