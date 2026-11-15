"""only_changed: what an action did to the page, without the whole tree again.

Ids name one element for its whole life, so comparing two trees by id is
exact. Fields that move with scrolling (offscreen, region) are not changes.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from zendriver_mcp.tools.content import ContentTools, delta, signatures


def test_new_changed_and_removed() -> None:
    before = signatures(
        [
            {"id": 1, "t": "btn", "l": "Save"},
            {"id": 2, "t": "in", "l": "Name", "v": "a"},
            {"id": 3, "t": "link", "l": "Help"},
        ]
    )
    after = signatures(
        [
            {"id": 1, "t": "btn", "l": "Save"},
            {"id": 2, "t": "in", "l": "Name", "v": "ab"},
            {"id": 4, "t": "btn", "l": "Undo"},
        ]
    )
    changed, removed = delta(before, after)
    assert changed == {2, 4}
    assert removed == [3]


def test_scrolling_is_not_a_change() -> None:
    before = signatures([{"id": 1, "t": "btn", "l": "Buy", "r": "main", "off": True}])
    after = signatures([{"id": 1, "t": "btn", "l": "Buy", "r": "hdr"}])
    assert delta(before, after) == (set(), [])


def _tools(trees: list[list[dict[str, Any]]], keys: list[str]) -> ContentTools:
    tools = ContentTools.__new__(ContentTools)
    tools._baselines = {}
    walks = iter(trees)
    pages = iter(keys)

    async def walk(**_kwargs: Any) -> tuple[list[dict[str, Any]], list[Any]]:
        return json.loads(json.dumps(next(walks))), []

    async def page_key() -> str:
        return next(pages)

    tools._walk = walk  # type: ignore[method-assign]
    tools._page_key = page_key  # type: ignore[method-assign]
    return tools


def test_first_call_returns_the_full_tree_and_says_why() -> None:
    tools = _tools([[{"id": 1, "t": "btn", "l": "A"}]], ["doc1"])
    out = asyncio.run(tools.get_interaction_tree(only_changed=True))
    assert [e["id"] for e in out.elements] == [1]
    assert out.removed is None
    assert "full tree" in out.summary


def test_second_call_returns_only_the_difference() -> None:
    tools = _tools(
        [
            [{"id": 1, "t": "btn", "l": "Open"}, {"id": 2, "t": "link", "l": "Docs"}],
            [{"id": 1, "t": "btn", "l": "Close"}, {"id": 5, "t": "btn", "l": "OK"}],
        ],
        ["doc1", "doc1"],
    )
    asyncio.run(tools.get_interaction_tree())
    out = asyncio.run(tools.get_interaction_tree(only_changed=True))
    assert sorted(e["id"] for e in out.elements) == [1, 5]
    assert out.removed == [2]


def test_a_new_page_returns_the_full_tree() -> None:
    tools = _tools(
        [[{"id": 1, "t": "btn", "l": "A"}], [{"id": 9, "t": "btn", "l": "B"}]], ["doc1", "doc2"]
    )
    asyncio.run(tools.get_interaction_tree())
    out = asyncio.run(tools.get_interaction_tree(only_changed=True))
    assert [e["id"] for e in out.elements] == [9]
    assert out.removed is None
