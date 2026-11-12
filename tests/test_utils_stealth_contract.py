"""Contract checks for the utils, stealth, emulation, permissions and proxy tools.

Builds a fresh server from just these modules and holds every tool to the
shared standard: a title, honest annotations, an output schema, and failures
that come back as isError with {error, code, hint} rather than success text.
No browser is needed.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from zendriver_mcp.tools.emulation import EmulationTools
from zendriver_mcp.tools.permissions import PermissionsTools
from zendriver_mcp.tools.proxy import ProxyTools
from zendriver_mcp.tools.stealth import StealthTools
from zendriver_mcp.tools.utils import UtilityTools

MODULES = (UtilityTools, StealthTools, EmulationTools, PermissionsTools, ProxyTools)


def _server() -> MCPServer:
    server = MCPServer("contract")
    for module in MODULES:
        module(server)
    return server


async def _list() -> list:
    async with Client(_server(), mode="2026-07-28") as client:
        return (await client.list_tools()).tools


async def _call(name: str, args: dict):
    async with Client(_server(), mode="2026-07-28") as client:
        return await client.call_tool(name, args)


def test_every_tool_has_title_annotations_and_output_schema() -> None:
    tools = asyncio.run(_list())
    assert len(tools) == 25
    for t in tools:
        assert t.title, t.name
        assert t.annotations is not None and t.annotations.read_only_hint is not None, t.name
        assert t.output_schema and "summary" in t.output_schema["properties"], t.name


def test_listings_are_read_only_and_setters_idempotent_writes() -> None:
    by_name = {t.name: t.annotations for t in asyncio.run(_list())}
    for name in ("list_devices", "list_network_profiles", "list_permission_names", "screenshot"):
        assert by_name[name].read_only_hint, name
    for name in ("set_viewport", "set_user_agent", "set_timezone", "grant_permissions"):
        a = by_name[name]
        assert not a.read_only_hint and a.destructive_hint is False and a.idempotent_hint, name
    for name in ("configure_proxy", "clear_proxy"):
        assert by_name[name].destructive_hint, name


@pytest.mark.parametrize(
    ("tool", "args", "code"),
    [
        ("screenshot", {}, "page_not_loaded"),
        # Statement blocks with `return` are accepted (run as an async body);
        # with no page the failure is the missing page, not the syntax.
        ("execute_javascript", {"script": "return 1"}, "page_not_loaded"),
        ("wait", {"seconds": -1}, "invalid_argument"),
        ("set_device", {"profile": "iphone15pro"}, "invalid_argument"),
        ("set_network_conditions", {"profile": "slow3g"}, "invalid_argument"),
        ("grant_permissions", {"permissions": ["geolocaton"]}, "invalid_argument"),
        ("configure_proxy", {"proxy_url": ""}, "invalid_argument"),
    ],
)
def test_failures_are_structured_errors(tool: str, args: dict, code: str) -> None:
    result = asyncio.run(_call(tool, args))
    assert result.is_error, result
    assert result.structured_content["code"] == code
    assert result.structured_content["error"]


def test_unknown_names_get_a_did_you_mean() -> None:
    result = asyncio.run(_call("set_device", {"profile": "iphone15pro"}))
    assert "iphone-15-pro" in (result.structured_content.get("hint") or "")
    result = asyncio.run(_call("grant_permissions", {"permissions": ["geolocaton"]}))
    assert "geolocation" in (result.structured_content.get("hint") or "")


def test_listings_return_structured_content() -> None:
    result = asyncio.run(_call("list_devices", {}))
    assert not result.is_error
    keys = {d["key"] for d in result.structured_content["devices"]}
    assert "pixel-8" in keys
    result = asyncio.run(_call("list_network_profiles", {}))
    assert {p["name"] for p in result.structured_content["profiles"]} >= {"offline", "4g"}
