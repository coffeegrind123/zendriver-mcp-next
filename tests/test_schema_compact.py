"""Tool schemas carry no bytes the model cannot use.

pydantic adds a "title" to every parameter and model ("Filter Type" for
filter_type), and writes each optional parameter as anyOf [X, null] with
default null. Neither tells a model anything the name and the required list do
not, and on the full surface they were 8.7 KB of 69.6 KB of input schemas
(measured 2026-11-15, mcpgrade). The anyOf form also fails client checks that
the plain form passes. Optional now means "may be omitted", never "send null".
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from zendriver_mcp.tools import CATALOG, mcp


def _walk(node: Any, path: str = ""):  # type: ignore[no-untyped-def]
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def _schemas() -> list[tuple[str, str, Any]]:
    """Every tool: the listed ones and those reached through call_tool/describe_tool."""
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    tools.update(CATALOG)
    out = []
    for t in tools.values():
        out.append((t.name, "input", t.parameters))
        if t.output_schema:
            out.append((t.name, "output", t.output_schema))
    return out


def test_no_title_annotations() -> None:
    bad = [
        f"{name} {side}{path}"
        for name, side, schema in _schemas()
        for path, node in _walk(schema)
        if isinstance(node.get("title"), str)
    ]
    assert not bad, bad[:10]


def test_no_null_alternatives() -> None:
    bad = [
        f"{name} {side}{path}"
        for name, side, schema in _schemas()
        for path, node in _walk(schema)
        if node.get("type") == "null" or node.get("default", 0) is None
    ]
    assert not bad, bad[:10]


def test_a_property_named_title_is_kept() -> None:
    page_info = next(s for n, side, s in _schemas() if n == "get_page_info" and side == "output")
    assert "title" in page_info["properties"]


def test_optional_parameters_keep_their_type_and_description() -> None:
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    sel = tools["click"].input_schema["properties"]["selector"]
    assert sel["type"] == "string" and sel["description"], json.dumps(sel)
    assert "selector" not in tools["click"].input_schema.get("required", [])


def test_every_tool_states_its_required_list() -> None:
    missing = [n for n, side, s in _schemas() if side == "input" and "required" not in s]
    assert not missing, missing
