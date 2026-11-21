"""Regions and labels in get_interaction_tree, against real pages in a real Chrome.

Regions used to fall back to a guess from position when an element had no
landmark ancestor: within 80 px of the top was "hdr", within 200 px of the left
edge "side". On a table layout (Hacker News) that tagged story titles "side",
their domains "main" and the first story "hdr", which is noise an agent pays to
read and is misled by. Forms were not regions at all, and no region said which
nav, form or dialog it was.

Links whose only content is an element carrying the name (a vote arrow `<div
title="upvote">`, a logo `<img alt>`) were listed as "[a]".
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.live_harness import data_url, live, sc, with_browser

pytestmark = live

GIF = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

PAGES = {
    "table_layout": """
<table><tr><td><a href="/a">Top left link</a></td><td><a href="/b">Top right link</a></td></tr>
<tr><td style="height:300px"><a href="/c">Lower left link</a></td></tr></table>""",
    "landmarks": """
<header><a href="/home">Home</a>
  <form aria-label="Site search"><input name="q"></form></header>
<nav aria-label="Primary"><a href="/docs">Docs</a></nav>
<nav><a href="/other">Unnamed nav link</a></nav>
<main><form name="login"><input name="user"><button>Log in</button></form>
  <form id="signup"><input name="email"></form>
  <form><input name="plain"></form>
  <a href="/more">Main link</a></main>
<aside><a href="/side">Aside link</a></aside>
<footer><a href="/legal">Legal</a></footer>
<div role="dialog" aria-labelledby="dt"><h2 id="dt">Cookie consent</h2><button>Accept</button></div>""",
    "child_labels": f"""
<a href="/vote"><div title="upvote" style="width:10px;height:10px;background:#888"></div></a>
<a href="/home"><img alt="Logo" src="{GIF}" width="20" height="20"></a>
<button><span aria-label="Close dialog" style="display:inline-block;width:10px;height:10px"></span></button>""",
    # Wikipedia's "Main menu": a transparent checkbox laid over its visible,
    # aria-hidden <label>. A person clicks what they see and toggles it.
    "transparent_control": """
<input type="checkbox" id="mm" role="button" aria-label="Main menu"
  style="position:absolute;left:10px;top:10px;width:30px;height:30px;margin:0;opacity:0;z-index:2"
  onchange="window.toggled = this.checked">
<label for="mm" aria-hidden="true"
  style="position:absolute;left:10px;top:10px;width:30px;height:30px;background:#ccc"></label>
<input type="checkbox" id="lonely" aria-label="No label" style="opacity:0">
<div style="opacity:0;margin-top:60px"><button>Ghost</button></div>""",
}


async def _scenario(c: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, body in PAGES.items():
        sc(await c.call_tool("navigate", {"url": data_url(body), "settle": 1}))
        out[name] = sc(await c.call_tool("get_interaction_tree", {}))["elements"]
    menu = next((e for e in out["transparent_control"] if e["l"] == "Main menu"), None)
    if menu:
        sc(await c.call_tool("click", {"selector": str(menu["id"])}))
        out["toggled"] = sc(
            await c.call_tool("execute_javascript", {"script": "return window.toggled"})
        )["value"]
    return out


@pytest.fixture(scope="module")
def walked() -> dict[str, Any]:
    return asyncio.run(with_browser(_scenario))


def _by_label(walked: dict[str, Any], page: str) -> dict[str, dict[str, Any]]:
    return {e["l"]: e for e in walked[page]}


def test_no_region_is_guessed_from_position(walked: dict[str, Any]) -> None:
    tree = walked["table_layout"]
    assert len(tree) == 3, tree
    assert all("r" not in e for e in tree), tree


def test_landmark_regions(walked: dict[str, Any]) -> None:
    by = _by_label(walked, "landmarks")
    assert by["Home"]["r"] == "hdr"
    assert by["Docs"]["r"] == "nav:Primary"
    assert by["Unnamed nav link"]["r"] == "nav"
    assert by["Main link"]["r"] == "main"
    assert by["Aside link"]["r"] == "side"
    assert by["Legal"]["r"] == "ftr"


def test_forms_are_regions_named_by_label_name_or_id(walked: dict[str, Any]) -> None:
    by = _by_label(walked, "landmarks")
    # The nearest landmark wins: a search form inside the header is the form.
    assert by["q"]["r"] == "form:Site search"
    assert by["user"]["r"] == "form:login"
    assert by["Log in"]["r"] == "form:login"
    assert by["email"]["r"] == "form:signup"
    assert by["plain"]["r"] == "form"


def test_dialog_region_is_named_by_labelledby(walked: dict[str, Any]) -> None:
    assert _by_label(walked, "landmarks")["Accept"]["r"] == "dlg:Cookie consent"


def test_link_named_by_a_child_title_or_alt(walked: dict[str, Any]) -> None:
    labels = set(_by_label(walked, "child_labels"))
    assert "upvote" in labels
    assert "Logo" in labels
    assert "Close dialog" in labels
    assert "[a]" not in labels


def test_transparent_control_over_its_visible_label_is_listed(walked: dict[str, Any]) -> None:
    labels = set(_by_label(walked, "transparent_control"))
    assert "Main menu" in labels
    assert walked.get("toggled") is True


def test_other_transparent_things_stay_out(walked: dict[str, Any]) -> None:
    labels = set(_by_label(walked, "transparent_control"))
    assert "Ghost" not in labels
    assert "No label" not in labels
