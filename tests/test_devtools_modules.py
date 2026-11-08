"""accessibility / devtools / lighthouse / screencast: registration and failures.

Each tool must list annotations and an output schema, and with no browser
running every call must come back as a structured isError result, never a
success string or a crash. Built on a fresh MCPServer so the test does not
depend on the rest of the catalogue.
"""

from __future__ import annotations

import asyncio
import shutil

from mcp import Client
from mcp.server.mcpserver import MCPServer

from src.tools.accessibility import AccessibilityTools
from src.tools.devtools import DevToolsTools
from src.tools.lighthouse import LighthouseTools
from src.tools.screencast import ScreencastTools

MODULES = (AccessibilityTools, DevToolsTools, LighthouseTools, ScreencastTools)

EXPECTED = {
    "get_accessibility_snapshot": "read",
    "describe_uid": "read",
    "click_by_uid": "write",
    "start_trace": "write",
    "stop_trace": "write",
    "take_heap_snapshot": "write",
    "run_lighthouse": "write",
    "check_lighthouse_available": "read",
    "start_screencast": "write",
    "stop_screencast": "write",
    "export_screencast_mp4": "write",
    "check_ffmpeg_available": "read",
}


def _server() -> MCPServer:
    m = MCPServer("test")
    for cls in MODULES:
        cls(m)
    return m


async def _listed() -> dict:
    async with Client(_server(), mode="2026-07-28") as c:
        return {t.name: t for t in (await c.list_tools()).tools}


def test_every_tool_has_annotations_and_output_schema() -> None:
    tools = asyncio.run(_listed())
    assert set(tools) == set(EXPECTED)
    for name, tool in tools.items():
        a = tool.annotations
        assert a is not None and a.title, name
        assert tool.output_schema and tool.output_schema.get("type") == "object", name
        assert a.read_only_hint is (EXPECTED[name] == "read"), name
        if EXPECTED[name] == "write":
            assert a.destructive_hint is False, name
        # Context is injected, never part of the input schema.
        assert "ctx" not in tool.input_schema.get("properties", {}), name


async def _calls() -> dict:
    out = {}
    async with Client(_server(), mode="2026-07-28") as c:
        for name, args in [
            ("get_accessibility_snapshot", {}),
            ("describe_uid", {"uid": "ax_nope"}),
            ("click_by_uid", {"uid": "ax_nope"}),
            ("start_trace", {}),
            ("stop_trace", {}),
            ("take_heap_snapshot", {}),
            ("start_screencast", {}),
            ("stop_screencast", {}),
            ("export_screencast_mp4", {"frames_dir": "/nonexistent/dir"}),
        ]:
            out[name] = await c.call_tool(name, args)
    return out


def test_failures_are_structured_errors() -> None:
    results = asyncio.run(_calls())
    for name, r in results.items():
        assert r.is_error, name
        sc = r.structured_content
        assert sc and sc["error"] and sc["code"], (name, sc)
    assert results["describe_uid"].structured_content["code"] == "stale_id"
    assert results["stop_trace"].structured_content["code"] == "conflict"
    assert results["stop_screencast"].structured_content["code"] == "conflict"
    if shutil.which("ffmpeg"):
        assert results["export_screencast_mp4"].structured_content["code"] == "invalid_argument"
    else:
        assert results["export_screencast_mp4"].structured_content["code"] == "not_installed"


async def _availability() -> tuple:
    async with Client(_server(), mode="legacy") as c:
        lh = await c.call_tool("check_lighthouse_available", {})
        ff = await c.call_tool("check_ffmpeg_available", {})
        return lh, ff


def test_availability_checks_report_structured() -> None:
    lh, ff = asyncio.run(_availability())
    assert not lh.is_error and lh.structured_content["available"] == bool(
        shutil.which("lighthouse")
    )
    assert not ff.is_error and ff.structured_content["available"] == bool(shutil.which("ffmpeg"))
