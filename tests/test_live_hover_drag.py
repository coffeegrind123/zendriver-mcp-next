"""hover and drag, as trusted input, against pages that record what they receive.

Each page logs every event it gets with isTrusted, so the tests check what a
page could check: that the events are real, and in the order a person's
pointer would produce them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.live_harness import data_url, live, sc, with_browser

pytestmark = live

LOG = """<script>
window.log = [];
const rec = (e) => log.push(e.type + ':' + e.target.id + ':' + e.isTrusted);
</script>"""

HOVER = f"""{LOG}
<style>#menu .items {{ display: none }} #menu:hover .items {{ display: block }}</style>
<div id="menu" style="width:160px;padding:10px;background:#eee">Products
  <div class="items"><a id="laptops" href="/laptops">Laptops</a></div></div>
<script>for (const t of ['mouseover', 'mouseenter']) menu.addEventListener(t, rec);</script>"""

SLIDER = f"""{LOG}
<div id="track" style="position:relative;width:400px;height:20px;background:#ddd;margin:40px">
  <div id="knob" style="position:absolute;left:0;top:0;width:20px;height:20px;background:#333"></div>
</div>
<script>
let down = null;
knob.addEventListener('pointerdown', (e) => {{ rec(e); down = e.clientX - knob.offsetLeft; }});
addEventListener('pointermove', (e) => {{ if (down !== null) {{
  knob.style.left = Math.max(0, Math.min(380, e.clientX - down)) + 'px'; }} }});
addEventListener('pointerup', (e) => {{ rec(e); down = null; }});
</script>"""

DND = f"""{LOG}
<div id="card" draggable="true" style="width:80px;height:40px;background:#9cf">Card</div>
<div id="bin" style="width:200px;height:120px;margin-top:80px;background:#fc9">Drop here</div>
<script>
card.addEventListener('dragstart', (e) => {{ rec(e); e.dataTransfer.setData('text/plain', 'card-1'); }});
card.addEventListener('dragend', rec);
bin.addEventListener('dragenter', rec);
bin.addEventListener('dragover', (e) => {{ e.preventDefault(); }});
bin.addEventListener('drop', (e) => {{ e.preventDefault(); rec(e);
  window.dropped = e.dataTransfer.getData('text/plain'); }});
</script>"""


async def _call(c: Any, name: str, arguments: dict[str, Any]) -> Any:
    """hover and drag are reached through call_tool, as a client of the catalogue does."""
    return await c.call_tool("call_tool", {"name": name, "arguments": arguments})


async def _js(c: Any, script: str) -> Any:
    return sc(await c.call_tool("execute_javascript", {"script": script}))["value"]


async def _scenario(c: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}

    sc(await c.call_tool("navigate", {"url": data_url(HOVER), "settle": 1}))
    out["hover"] = sc(await _call(c, "hover", {"selector": "#menu"}))
    tree = sc(await c.call_tool("get_interaction_tree", {}))["elements"]
    out["hover_tree"] = [e["l"] for e in tree]
    out["hover_log"] = await _js(c, "return log")

    sc(await c.call_tool("navigate", {"url": data_url(SLIDER), "settle": 1}))
    out["slide"] = sc(await _call(c, "drag", {"source": "#knob", "dx": 200, "dy": 0}))
    out["slide_left"] = await _js(c, "return knob.offsetLeft")
    out["slide_log"] = await _js(c, "return log")

    sc(await c.call_tool("navigate", {"url": data_url(DND), "settle": 1}))
    out["dnd"] = sc(await _call(c, "drag", {"source": "#card", "target": "#bin"}))
    out["dnd_log"] = await _js(c, "return log")
    out["dropped"] = await _js(c, "return window.dropped || null")

    bad = await _call(c, "drag", {"source": "#card"})
    out["bad"] = bad.structured_content if bad.is_error else None
    return out


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    return asyncio.run(with_browser(_scenario))


def test_hover_opens_a_css_hover_menu(run: dict[str, Any]) -> None:
    assert "Laptops" in run["hover_tree"]
    assert "mouseover:menu:true" in run["hover_log"]
    assert "mouseenter:menu:true" in run["hover_log"]


def test_pointer_drag_moves_a_slider_with_trusted_events(run: dict[str, Any]) -> None:
    assert 180 <= run["slide_left"] <= 220, run["slide_left"]
    assert run["slide_log"][0] == "pointerdown:knob:true"
    assert run["slide_log"][-1].startswith("pointerup:") and run["slide_log"][-1].endswith(":true")


def test_html5_drag_and_drop_delivers_data(run: dict[str, Any]) -> None:
    log = run["dnd_log"]
    assert run["dropped"] == "card-1", log
    for event in ("dragstart:card:true", "dragenter:bin:true", "drop:bin:true"):
        assert event in log, log
    assert log.index("dragstart:card:true") < log.index("drop:bin:true")
    assert run["dnd"]["mode"] == "html5"


def test_html5_drag_ends_on_the_source_as_a_real_one_does(run: dict[str, Any]) -> None:
    log = run["dnd_log"]
    assert "dragend:card:true" in log, log
    assert log.index("drop:bin:true") < log.index("dragend:card:true")


def test_drag_needs_a_target_or_an_offset(run: dict[str, Any]) -> None:
    assert run["bad"] and run["bad"]["code"] == "invalid_argument", run["bad"]
