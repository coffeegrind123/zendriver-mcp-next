"""Iframes, same-process and cross-site, against a real Chrome.

Before frame support the walker saw only the main document, so everything in
an embedded form (payment fields, login widgets, comment boxes) was invisible
and unclickable by id. Each test below names one thing an agent needs to do
inside a frame, checked from the frame's own point of view: a listener in each
document records what arrived and whether it was trusted.

Also here: a cached isolated-world context id must not be reused after a
cross-site navigation. Measured 2026-11-15: the new renderer process reuses
small context ids, and the stale id resolved to a different live context.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.live_harness import Sites, foreign_attrs, live, sc, with_browser

pytestmark = live

GUARD = """<script>
window.log = [];
const rec = (e) => log.push(e.type + ':' + e.isTrusted + ':' + (e.target.id || e.target.tagName) +
  (e.type === 'click' ? '@' + Math.round(e.clientX) + ',' + Math.round(e.clientY) : ''));
addEventListener('click', rec, true);
addEventListener('input', rec, true);
new MutationObserver((ms) => ms.forEach((m) => m.type === 'attributes' &&
  log.push('attr:' + m.attributeName + '@' + m.target.tagName + (m.target.id ? '#' + m.target.id : '') + '=' + m.target.getAttribute(m.attributeName)))).observe(document, {attributes: true, subtree: true});
</script>"""


# A cross-site frame's window cannot be read from the top page, so each inner
# page prints its own log as text, and get_text_content reads it from inside.
SHOW_LOG = """<pre id="logview"></pre><script>
const push = log.push.bind(log);
log.push = (x) => { push(x); document.getElementById('logview').textContent = 'LOG ' + log.join(' '); };
</script>"""


def _inner(name: str, extra: str = "") -> str:
    return f"""<!doctype html><title>{name}</title>{GUARD}
<body style="margin:0;font:16px sans-serif">
{SHOW_LOG}
<p>Text of {name}</p>
<button id="ib" style="margin:20px">Inner {name}</button>
<input id="in-{name}" placeholder="Field {name}">
{extra}
</body>"""


PAGES = {
    "/outer.html": f"""<!doctype html><title>outer</title>{GUARD}
<body style="margin:0;height:3000px">
<button id="top">Top</button>
<div id="cover" style="display:none;position:fixed;z-index:10;left:0;top:0;width:100%;height:100%;background:#0003"></div>
<iframe id="same" src="/same.html" style="position:absolute;left:20px;top:100px;width:420px;height:260px;border:5px solid red"></iframe>
<iframe id="cross" src="{{b}}/cross.html" style="position:absolute;left:40px;top:1500px;width:520px;height:420px;border:9px solid blue"></iframe>
<iframe id="hidden" src="/hidden.html" style="display:none"></iframe>
<iframe id="pixel" src="/pixel.html" width="0" height="0" style="border:0"></iframe>
</body>""",
    "/same.html": _inner("same"),
    "/cross.html": _inner(
        "cross",
        '<input id="only-cross" placeholder="Only cross">'
        '<iframe id="nested" src="{b}/nested.html" style="width:300px;height:120px;border:3px solid green"></iframe>',
    ),
    "/nested.html": _inner("nested"),
    "/hidden.html": _inner("hidden"),
    "/pixel.html": _inner("pixel"),
    "/plain.html": f"<!doctype html><title>plain</title>{GUARD}<button>Plain</button>",
    "/after.html": "<!doctype html><title>after</title><p>after</p>",
}

READ_LOGS = """
const out = {top: window.log, zd: typeof window.__zd};
for (const f of document.querySelectorAll('iframe')) {
  try { out[f.id] = f.contentWindow.log; } catch (e) { out[f.id] = 'cross-origin'; }
}
return out;"""


async def _scenario(c: Any, sites: Sites) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sc(await c.call_tool("navigate", {"url": f"{sites.a}/outer.html", "settle": 3}))
    tree = sc(await c.call_tool("get_interaction_tree", {}))
    out["tree"] = tree
    by = {(e.get("f", 0), e["l"]): e for e in tree["elements"]}
    frames = {fr["url"].split("/")[-1]: fr["f"] for fr in tree.get("frames") or []}
    out["frames"] = frames

    same, cross, nested = (
        frames.get("same.html"),
        frames.get("cross.html"),
        frames.get("nested.html"),
    )
    out["click_same"] = await c.call_tool(
        "click", {"selector": str(by[(same, "Inner same")]["id"])}
    )
    out["click_cross"] = await c.call_tool(
        "click", {"selector": str(by[(cross, "Inner cross")]["id"])}
    )
    out["type_cross"] = await c.call_tool(
        "type_text", {"selector": str(by[(cross, "Field cross")]["id"]), "text": "hello"}
    )
    out["click_nested"] = await c.call_tool(
        "click", {"selector": str(by[(nested, "Inner nested")]["id"])}
    )
    # A selector absent from the main page, unique to one frame, resolves there.
    out["type_unique"] = await c.call_tool("type_text", {"selector": "#only-cross", "text": "x"})
    # One that matches in several frames must not guess.
    out["ambiguous"] = (await c.call_tool("click", {"selector": "#ib"})).structured_content

    out["text"] = sc(await c.call_tool("get_text_content", {"max_chars": 20000}))["text"]

    out["top_logs"] = sc(await c.call_tool("execute_javascript", {"script": READ_LOGS}))

    # A cover over the whole page covers the iframe, so a click inside it is refused.
    await c.call_tool("execute_javascript", {"script": "cover.style.display='block'"})
    out["covered"] = (
        await c.call_tool("click", {"selector": str(by[(same, "Inner same")]["id"])})
    ).structured_content
    await c.call_tool("execute_javascript", {"script": "cover.style.display='none'"})

    # A frame that navigates takes its ids with it.
    await c.call_tool(
        "execute_javascript",
        {"script": f"document.getElementById('same').src = '{sites.a}/after.html'"},
    )
    for _ in range(40):
        frames_now = sc(await c.call_tool("get_interaction_tree", {})).get("frames") or []
        if any(fr["url"].endswith("/after.html") for fr in frames_now):
            break
        await asyncio.sleep(0.25)
    out["stale_after_frame_nav"] = (
        await c.call_tool("click", {"selector": str(by[(same, "Inner same")]["id"])})
    ).structured_content

    # Cross-site navigations back and forth: each walk must run in OUR world.
    walks = []
    for url in [
        f"{sites.b}/plain.html",
        f"{sites.a}/plain.html",
        f"{sites.b}/plain.html",
        f"{sites.a}/plain.html",
    ]:
        sc(await c.call_tool("navigate", {"url": url, "settle": 1}))
        r = await c.call_tool("get_interaction_tree", {})
        walks.append(r.structured_content)
    out["walks"] = walks
    out["plain_page"] = sc(
        await c.call_tool(
            "execute_javascript", {"script": "return {zd: typeof window.__zd, log: window.log}"}
        )
    )
    # An id from the first page, after many navigations and walks, is stale even
    # though the current page has issued ids of its own since.
    out["old_id"] = (
        await c.call_tool("click", {"selector": str(by[(0, "Top")]["id"])})
    ).structured_content
    return out


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    sites = Sites(PAGES)
    try:
        return asyncio.run(with_browser(lambda c: _scenario(c, sites)))
    finally:
        sites.close()


def _value(r: Any) -> Any:
    return r["value"] if isinstance(r, dict) and "value" in r else r


def test_tree_lists_frames_and_their_elements(run: dict[str, Any]) -> None:
    frames = run["frames"]
    assert {"same.html", "cross.html", "nested.html"} <= set(frames), run["tree"]
    labels = {(e.get("f", 0), e["l"]) for e in run["tree"]["elements"]}
    assert (0, "Top") in labels
    assert (frames["same.html"], "Inner same") in labels
    assert (frames["cross.html"], "Inner cross") in labels
    assert (frames["nested.html"], "Inner nested") in labels


def test_hidden_and_zero_size_frames_are_not_walked(run: dict[str, Any]) -> None:
    assert "hidden.html" not in run["frames"]
    assert "pixel.html" not in run["frames"]


def test_trusted_click_and_typing_inside_frames(run: dict[str, Any]) -> None:
    for key in ("click_same", "click_cross", "type_cross", "click_nested", "type_unique"):
        assert not run[key].is_error, (key, run[key].content)
    logs = _value(run["top_logs"])
    same = logs["same"]
    assert any(e.startswith("click:true:ib") for e in same), same
    assert logs["cross"] == "cross-origin"


def _frame_text(text: str, name: str) -> str:
    start = text.index(f"Text of {name}")
    head = text.rfind("--- frame", 0, start)
    end = text.find("--- frame", start)
    return text[head : end if end != -1 else len(text)]


def test_frame_text_is_read_under_a_header(run: dict[str, Any]) -> None:
    text = run["text"]
    for name in ("same", "cross", "nested"):
        assert "--- frame" in _frame_text(text, name), text


def test_cross_site_frame_events_are_trusted(run: dict[str, Any]) -> None:
    cross = _frame_text(run["text"], "cross")
    diag = {
        "click": run["click_cross"].structured_content,
        "geometry": run["geometry"],
        "top_log": _value(run["top_logs"]).get("top"),
        "frames": run["frames"],
    }
    assert "click:true:ib" in cross, (cross, diag)
    assert "input:true:in-cross" in cross, cross
    assert "input:true:only-cross" in cross, cross
    assert ":false:" not in cross, cross
    nested = _frame_text(run["text"], "nested")
    assert "click:true:ib" in nested, nested


def test_selector_unique_to_one_frame_and_ambiguous_ones(run: dict[str, Any]) -> None:
    assert run["ambiguous"]["code"] == "ambiguous", run["ambiguous"]


def test_cover_over_an_iframe_is_reported(run: dict[str, Any]) -> None:
    assert run["covered"]["code"] == "element_obscured", run["covered"]
    assert "cover" in run["covered"]["error"]


def test_frame_navigation_makes_its_ids_stale(run: dict[str, Any]) -> None:
    assert run["stale_after_frame_nav"]["code"] == "stale_id", run["stale_after_frame_nav"]


def _foreign(entries: list[str]) -> list[str]:
    return foreign_attrs(entries)


def test_no_trace_in_any_document(run: dict[str, Any]) -> None:
    logs = _value(run["top_logs"])
    assert logs["zd"] == "undefined"
    for name in ("top", "same"):
        assert not _foreign(logs[name]), (name, logs[name])
    for name in ("cross", "nested"):
        assert "attr:" not in _frame_text(run["text"], name)


def test_walks_survive_cross_site_navigations(run: dict[str, Any]) -> None:
    for w in run["walks"]:
        assert "elements" in w, w
        assert [e["l"] for e in w["elements"]] == ["Plain"], w
    page = _value(run["plain_page"])
    assert page["zd"] == "undefined"
    assert not _foreign(page["log"])


def test_old_id_is_stale_after_navigations(run: dict[str, Any]) -> None:
    assert run["old_id"]["code"] == "stale_id", run["old_id"]
