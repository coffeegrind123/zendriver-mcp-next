"""End to end against a real Chrome, through an MCP client.

Opt-in: needs Chrome and a display, so it runs only with ZENDRIVER_MCP_LIVE=1
(for example `DISPLAY=:99 ZENDRIVER_MCP_LIVE=1 pytest tests/test_live.py`).

What it proves, from the page's own point of view (a main-world listener
records every event): clicks and keys arrive as trusted events, element ids
leave no trace in the DOM or in page globals, and the page cannot see our
helper scripts.
"""

from __future__ import annotations

import asyncio
import os
import urllib.parse

import pytest
from mcp import Client

pytestmark = pytest.mark.skipif(
    os.environ.get("ZENDRIVER_MCP_LIVE") != "1", reason="live browser test (ZENDRIVER_MCP_LIVE=1)"
)

PAGE = """<!doctype html><title>live</title>
<script>
window.log = [];
const rec = (e) => log.push(e.type + ':' + e.isTrusted + ':' + (e.target.id || e.target.tagName));
addEventListener('click', rec, true);
addEventListener('keydown', rec, true);
addEventListener('pointerdown', rec, true);
new MutationObserver((ms) => ms.forEach((m) => m.type === 'attributes' &&
  log.push('attr:' + m.attributeName))).observe(document, {attributes: true, subtree: true});
</script>
<button id="go" onclick="document.title='clicked'">Go</button>
<input id="q" placeholder="Search">
<a id="pop" href="about:blank" target="_blank">Open</a>
<div id="cover" style="display:none;position:fixed;inset:0;background:#0003"></div>
"""
URL = "data:text/html," + urllib.parse.quote(PAGE)


def _sc(r):
    assert not r.is_error, r.content
    return r.structured_content


async def _scenario() -> dict:
    from src.tools import mcp

    out: dict = {}
    async with Client(mcp, mode="2026-07-28") as c:
        _sc(await c.call_tool("start_browser", {"window_size": "1200x800"}))
        try:
            nav = _sc(await c.call_tool("navigate", {"url": URL, "settle": 3}))
            out["nav"] = nav
            tree = _sc(await c.call_tool("get_interaction_tree", {}))
            ids = {e["l"]: e["id"] for e in tree["elements"]}
            out["ids"] = ids

            _sc(await c.call_tool("click", {"selector": str(ids["Go"])}))
            _sc(await c.call_tool("type_text", {"selector": "#q", "text": "hello"}))
            _sc(await c.call_tool("press_key", {"key": "Enter"}))
            probe = _sc(
                await c.call_tool(
                    "execute_js",
                    {
                        "script": "return {log, title: document.title, value: q.value,"
                        " zd: typeof window.__zd, marked: document.querySelectorAll('[data-zendriver-id]').length}"
                    },
                )
            )
            out["page"] = probe["value"] if "value" in probe else probe

            # Something covering the button must be named, not clicked through.
            await c.call_tool("execute_js", {"script": "cover.style.display='block'"})
            covered = await c.call_tool("click", {"selector": "#go"})
            out["covered"] = covered.structured_content
            await c.call_tool("execute_js", {"script": "cover.style.display='none'"})

            # A stale id after navigation is stale_id, not a wrong element.
            await c.call_tool("navigate", {"url": "data:text/html,<p>two</p>", "settle": 2})
            stale = await c.call_tool("click", {"selector": str(ids["Go"])})
            out["stale"] = stale.structured_content

            # A second tab, read without switching to it.
            opened = _sc(
                await c.call_tool(
                    "call_tool",
                    {
                        "name": "new_tab",
                        "arguments": {"url": "data:text/html,<title>second</title>"},
                    },
                )
            )
            out["second_info"] = _sc(
                await c.call_tool(
                    "call_tool", {"name": "get_page_info", "arguments": {"tab": opened["tab"]}}
                )
            )
            out["tabs"] = _sc(await c.call_tool("list_tabs", {}))

            # batch with a $ reference, and a screenshot file as a resource.
            out["batch"] = _sc(
                await c.call_tool(
                    "batch",
                    {
                        "steps": [
                            {"tool": "list_tabs", "arguments": {}},
                            {"tool": "get_page_info", "arguments": {"tab": "$0.tabs.0.id"}},
                        ]
                    },
                )
            )
            shot = await c.call_tool("screenshot", {"save_path": "/tmp/zd-live-shot.png"})
            out["shot_types"] = [b.type for b in shot.content]
            link = next(b for b in shot.content if b.type == "resource_link")
            res = await c.read_resource(link.uri)
            out["resource_bytes"] = len(getattr(res.contents[0], "blob", "") or "")
        finally:
            await c.call_tool("stop_browser", {})
    return out


def test_live_end_to_end() -> None:
    out = asyncio.run(_scenario())
    page = out["page"]
    log = page["log"]

    assert out["nav"]["document_ready"] is True
    assert {"Go", "Search"} <= set(out["ids"])

    clicks = [e for e in log if e.startswith("click:")]
    assert clicks and all(":true:" in e for e in clicks), log
    assert any(e.startswith("pointerdown:true") for e in log), "a real click has a pointer sequence"
    keys = [e for e in log if e.startswith("keydown:")]
    assert keys and all(":true:" in e for e in keys), log

    assert page["title"] == "clicked"
    assert page["value"] == "hello"
    assert page["zd"] == "undefined", "our id store leaked into the page's world"
    assert page["marked"] == 0
    assert not [e for e in log if e.startswith("attr:")], f"we mutated attributes: {log}"

    assert out["covered"]["code"] == "element_obscured"
    assert "cover" in out["covered"]["error"]
    assert out["stale"]["code"] == "stale_id"

    assert out["second_info"]["title"] == "second"
    assert len(out["tabs"]["tabs"]) == 2
    assert out["batch"]["completed"] == 2

    assert out["shot_types"][:2] == ["text", "image"]
    assert out["resource_bytes"] > 1000
