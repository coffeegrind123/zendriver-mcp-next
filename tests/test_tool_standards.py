"""Every tool meets the same bar, so none is the odd one out to a model.

Each tool must have: a title, annotations, an output schema, a description,
and a described parameter for every argument. Errors must be isError results,
never success strings starting "Error".
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from mcp import Client

from zendriver_mcp.registry import SPECS, Effect
from zendriver_mcp.tools import CATALOG

TOOLS = sorted(CATALOG)


@pytest.mark.parametrize("name", TOOLS)
def test_tool_is_complete(name: str) -> None:
    tool = CATALOG[name]
    assert tool.title, "no title"
    assert tool.annotations is not None, "no annotations"
    assert tool.output_schema is not None, "no output schema"
    assert (tool.description or "").strip(), "no description"
    for pname, spec in (tool.parameters.get("properties") or {}).items():
        assert spec.get("description"), f"parameter {pname!r} has no description"
    spec = SPECS[name]
    ann = tool.annotations
    assert ann.read_only_hint == (spec.effect is Effect.READ)
    if not ann.read_only_hint:
        assert ann.destructive_hint == (spec.effect is Effect.DESTRUCTIVE)


def test_no_tool_returns_an_error_string() -> None:
    """A failure returned as text looks like success to every client."""
    offenders = []
    for path in sorted(Path("zendriver_mcp/tools").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Return) and isinstance(
                node.value, (ast.Constant, ast.JoinedStr)
            ):
                text = ast.unparse(node.value)
                if text.lstrip("f'\"").startswith(("Error", "error:")):
                    offenders.append(f"{path}:{node.lineno}: {text[:60]}")
    assert not offenders, "\n".join(offenders)


def test_no_selector_is_interpolated_into_javascript() -> None:
    """Element access goes through zendriver_mcp/dom.py, never an f-string with a selector."""
    offenders = []
    for path in sorted(Path("zendriver_mcp/tools").glob("*.py")):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if "querySelector" in line and ("{safe" in line or "{selector" in line):
                offenders.append(f"{path}:{i}: {line.strip()[:80]}")
    assert not offenders, "\n".join(offenders)


def test_no_dom_marker_attribute_anywhere() -> None:
    hits = [
        str(p)
        for p in Path("zendriver_mcp").rglob("*")
        if p.is_file() and p.suffix in {".py", ".js"} and "data-zendriver-id" in p.read_text()
    ]
    assert not hits, hits


def test_page_tools_take_a_tab_and_browser_tools_do_not() -> None:
    for name, tool in CATALOG.items():
        has_tab = "tab" in (tool.parameters.get("properties") or {})
        assert has_tab == SPECS[name].page_scoped, name


def test_every_listed_tool_round_trips_over_both_protocols() -> None:
    from zendriver_mcp.tools import mcp

    async def run(mode: str) -> set[str]:
        async with Client(mcp, mode=mode) as c:
            return {t.name for t in (await c.list_tools()).tools}

    assert asyncio.run(run("legacy")) == asyncio.run(run("2026-07-28"))
