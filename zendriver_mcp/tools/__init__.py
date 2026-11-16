# tools package - modular browser automation tools for Zendriver MCP server
import os
from typing import Any

from mcp.server.mcpserver import MCPServer as FastMCP

from zendriver_mcp import __version__
from zendriver_mcp.instructions import INSTRUCTIONS
from zendriver_mcp.resources import install_resources
from zendriver_mcp.tools.accessibility import AccessibilityTools
from zendriver_mcp.tools.base import ToolBase
from zendriver_mcp.tools.browser import BrowserTools
from zendriver_mcp.tools.catalog import Surface, install_catalog
from zendriver_mcp.tools.content import ContentTools
from zendriver_mcp.tools.cookies import CookieTools
from zendriver_mcp.tools.devtools import DevToolsTools
from zendriver_mcp.tools.dialogs import DialogTools
from zendriver_mcp.tools.downloads import DownloadTools
from zendriver_mcp.tools.elements import ElementTools
from zendriver_mcp.tools.emulation import EmulationTools
from zendriver_mcp.tools.forms import FormTools
from zendriver_mcp.tools.humanlike import HumanInputTools
from zendriver_mcp.tools.interception import InterceptionTools
from zendriver_mcp.tools.lighthouse import LighthouseTools
from zendriver_mcp.tools.logging import LoggingTools
from zendriver_mcp.tools.navigation import NavigationTools
from zendriver_mcp.tools.network_control import NetworkControlTools
from zendriver_mcp.tools.permissions import PermissionsTools
from zendriver_mcp.tools.proxy import ProxyTools
from zendriver_mcp.tools.query import QueryTools
from zendriver_mcp.tools.screencast import ScreencastTools
from zendriver_mcp.tools.stealth import StealthTools
from zendriver_mcp.tools.storage import StorageTools
from zendriver_mcp.tools.tabs import TabTools
from zendriver_mcp.tools.utils import UtilityTools

# initialize the MCP server
mcp = FastMCP(
    "zendriver",
    title="Zendriver browser",
    description="A real, headed Chrome driven over CDP, built to look like a person's browser.",
    instructions=INSTRUCTIONS,
    version=__version__,
    website_url="https://github.com/coffeegrind123/zendriver-mcp-next",
)

# Register every module, capturing the tool names each adds so profiles and the
# catalogue can group them without a hand-kept map that would drift.
_MODULES = [
    ("browser", BrowserTools),
    ("navigation", NavigationTools),
    ("tabs", TabTools),
    ("elements", ElementTools),
    ("query", QueryTools),
    ("content", ContentTools),
    ("storage", StorageTools),
    ("logging", LoggingTools),
    ("forms", FormTools),
    ("utils", UtilityTools),
    ("stealth", StealthTools),
    ("humaninput", HumanInputTools),
    ("emulation", EmulationTools),
    ("devtools", DevToolsTools),
    ("lighthouse", LighthouseTools),
    ("screencast", ScreencastTools),
    ("accessibility", AccessibilityTools),
    ("cookies", CookieTools),
    ("network", NetworkControlTools),
    ("permissions", PermissionsTools),
    ("proxy", ProxyTools),
    ("interception", InterceptionTools),
    ("dialogs", DialogTools),
    ("downloads", DownloadTools),
]

_TOOL_GROUPS: dict[str, list[str]] = {}
_INSTANCES: dict[str, Any] = {}
for _group, _cls in _MODULES:
    _before = set(mcp._tool_manager._tools)
    _INSTANCES[_group] = _cls(mcp)
    _TOOL_GROUPS[_group] = sorted(set(mcp._tool_manager._tools) - _before)


def _every_tool(server: FastMCP, catalog: dict[str, Any]) -> list[Any]:
    """The listed tools plus those reached through call_tool and describe_tool."""
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    tools.update(catalog)
    return list(tools.values())


def _declare_explicit_required(tools: list[Any]) -> None:
    """Emit an explicit "required" array on every tool schema.

    pydantic omits "required" entirely when every parameter has a default, so a
    model cannot tell "nothing is mandatory" apart from "this schema forgot to
    say". The empty array states it outright. Do not remove: it cannot be
    expressed from the function signatures.
    """
    for tool in tools:
        params = tool.parameters
        if isinstance(params, dict) and params.get("type") == "object" and "required" not in params:
            params["required"] = []


def _compact(node: Any) -> Any:
    """Drop schema bytes a model cannot use, in place; returns the node.

    - "title" annotations: pydantic adds one per parameter and model ("Filter
      Type" for filter_type), repeating the name. Only string values are
      removed, so a property that is itself called "title" stays.
    - anyOf [X, null] with default null, pydantic's form for an optional
      parameter, becomes plain X: optional means "may be omitted". Arguments
      are still validated by the pydantic model, which accepts an explicit null,
      and results are dumped with exclude_none, so null never appears there.

    Measured 2026-11-15 (mcpgrade): 8.7 KB of the full surface's 69.6 KB of
    input schemas, and the anyOf form fails client checks the plain one passes.
    """
    if isinstance(node, list):
        for item in node:
            _compact(item)
        return node
    if not isinstance(node, dict):
        return node
    if isinstance(node.get("title"), str):
        del node["title"]
    alts = node.get("anyOf")
    if isinstance(alts, list) and {"type": "null"} in alts:
        rest = [a for a in alts if a != {"type": "null"}]
        if len(rest) == 1 and isinstance(rest[0], dict):
            del node["anyOf"]
            for k, v in rest[0].items():
                node.setdefault(k, v)
        else:
            node["anyOf"] = rest
    if "default" in node and node["default"] is None:
        del node["default"]
    for key, value in list(node.items()):
        if key == "properties" and isinstance(value, dict):
            for prop in value.values():
                _compact(prop)
        elif isinstance(value, dict | list):
            _compact(value)
    return node


def _compact_schemas(tools: list[Any]) -> None:
    for tool in tools:
        _compact(tool.parameters)
        if tool.output_schema:
            _compact(tool.output_schema)


def _split_env(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {tok.strip() for tok in raw.replace(",", " ").split() if tok.strip()}


# Named profiles map to sets of tool GROUPS. Selecting a profile or a group list
# narrows the exposed surface; ALLOW/DENY then refine by exact tool name. The
# browser lifecycle group is always kept so a session can start/stop the browser.
_PROFILES: dict[str, set[str]] = {
    "full": set(_TOOL_GROUPS),
    "minimal": {"browser", "navigation", "content", "query", "elements"},
    "browse": {"browser", "navigation", "tabs", "content", "query"},
    "interact": {"browser", "navigation", "tabs", "elements", "query", "content", "forms"},
    "scrape": {
        "browser",
        "navigation",
        "tabs",
        "content",
        "query",
        "logging",
        "cookies",
        "downloads",
    },
    "stealth": {
        "browser",
        "navigation",
        "tabs",
        "elements",
        "query",
        "content",
        "forms",
        "stealth",
        "humaninput",
        "emulation",
    },
    "audit": {
        "browser",
        "navigation",
        "content",
        "query",
        "logging",
        "lighthouse",
        "devtools",
        "accessibility",
    },
    "network": {"browser", "navigation", "logging", "network", "interception", "proxy", "cookies"},
}


def _apply_tool_filter(server: FastMCP) -> None:
    """Narrow the exposed tool surface from env (issue #307: too many tools).

    Narrows which tools EXIST in this process (listed or reachable through
    call_tool); ZENDRIVER_MCP_SURFACE then decides which of them are listed.

    ZENDRIVER_MCP_PROFILE  one of: full, minimal, browse, interact, scrape,
                           stealth, audit, network (gateway = full, catalog surface)
    ZENDRIVER_MCP_GROUPS   comma/space list of groups (see _MODULES)
    ZENDRIVER_MCP_ALLOW    comma/space list of exact tool names to keep
    ZENDRIVER_MCP_DENY     comma/space list of exact tool names to remove
    Unset -> every tool exists.
    """
    profile = os.environ.get("ZENDRIVER_MCP_PROFILE", "").strip().lower()
    groups = _split_env("ZENDRIVER_MCP_GROUPS")
    allow = _split_env("ZENDRIVER_MCP_ALLOW")
    deny = _split_env("ZENDRIVER_MCP_DENY")
    if profile == "gateway" or not (profile or groups or allow or deny):
        return

    all_tools = set(server._tool_manager._tools)
    narrowed = bool(profile and profile != "full") or bool(groups)

    keep: set[str] = set()
    if profile and profile != "full":
        keep |= {
            t for g in _PROFILES.get(profile, set(_TOOL_GROUPS)) for t in _TOOL_GROUPS.get(g, [])
        }
    for g in groups:
        keep |= set(_TOOL_GROUPS.get(g, []))
    if narrowed:
        keep |= set(_TOOL_GROUPS.get("browser", []))  # lifecycle always available
        # A dialog freezes its page under any profile, and the dialog_open error
        # tells the caller to answer it, so the tools that do are always kept.
        keep |= set(_TOOL_GROUPS.get("dialogs", []))
    else:
        keep = set(all_tools)  # ALLOW/DENY alone operate on the full set

    if allow:
        keep = (keep & allow) if narrowed else set(allow)
    if deny:
        keep -= deny
    keep &= all_tools

    for name in sorted(all_tools - keep):
        server._tool_manager.remove_tool(name)


_apply_tool_filter(mcp)


def _surface() -> Surface:
    """Which tools tools/list shows. Fixed for the life of the process.

    ZENDRIVER_MCP_SURFACE=catalog (default) lists the core loop plus the
    search/call/batch/jobs meta-tools; =full lists every tool. The older
    switches ZENDRIVER_MCP_GATEWAY=1 and ZENDRIVER_MCP_PROFILE=gateway mean
    catalog, and are kept so existing configs keep working.
    """
    raw = os.environ.get("ZENDRIVER_MCP_SURFACE", "").strip().lower()
    if raw:
        try:
            return Surface(raw)
        except ValueError:
            raise SystemExit(
                f"ZENDRIVER_MCP_SURFACE={raw!r} is not one of: "
                + ", ".join(m.value for m in Surface)
            ) from None
    return Surface.CATALOG


CATALOG = install_catalog(mcp, _surface())
install_resources(mcp)
_ALL_TOOLS = _every_tool(mcp, CATALOG)
_compact_schemas(_ALL_TOOLS)
_declare_explicit_required(_ALL_TOOLS)

__all__ = ["mcp", "CATALOG", "ToolBase"]
