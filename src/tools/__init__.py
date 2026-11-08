# tools package - modular browser automation tools for Zendriver MCP server
import os
from typing import Any

from mcp.server.mcpserver import MCPServer as FastMCP

from src import __version__
from src.instructions import INSTRUCTIONS
from src.resources import install_resources
from src.tools.accessibility import AccessibilityTools
from src.tools.base import ToolBase
from src.tools.browser import BrowserTools
from src.tools.catalog import Surface, install_catalog
from src.tools.content import ContentTools
from src.tools.cookies import CookieTools
from src.tools.devtools import DevToolsTools
from src.tools.elements import ElementTools
from src.tools.emulation import EmulationTools
from src.tools.forms import FormTools
from src.tools.humanlike import HumanInputTools
from src.tools.interception import InterceptionTools
from src.tools.lighthouse import LighthouseTools
from src.tools.logging import LoggingTools
from src.tools.navigation import NavigationTools
from src.tools.network_control import NetworkControlTools
from src.tools.permissions import PermissionsTools
from src.tools.proxy import ProxyTools
from src.tools.query import QueryTools
from src.tools.screencast import ScreencastTools
from src.tools.stealth import StealthTools
from src.tools.storage import StorageTools
from src.tools.tabs import TabTools
from src.tools.utils import UtilityTools

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
]

_TOOL_GROUPS: dict[str, list[str]] = {}
_INSTANCES: dict[str, Any] = {}
for _group, _cls in _MODULES:
    _before = set(mcp._tool_manager._tools)
    _INSTANCES[_group] = _cls(mcp)
    _TOOL_GROUPS[_group] = sorted(set(mcp._tool_manager._tools) - _before)


def _declare_explicit_required(server: FastMCP) -> None:
    """Emit an explicit "required" array on every tool schema.

    pydantic omits "required" entirely when every parameter has a default, so a
    model cannot tell "nothing is mandatory" apart from "this schema forgot to
    say". The empty array states it outright. Do not remove: it cannot be
    expressed from the function signatures.
    """
    for tool in server._tool_manager.list_tools():
        params = tool.parameters
        if isinstance(params, dict) and params.get("type") == "object" and "required" not in params:
            params["required"] = []


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
    "scrape": {"browser", "navigation", "tabs", "content", "query", "logging", "cookies"},
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
_declare_explicit_required(mcp)

__all__ = ["mcp", "CATALOG", "ToolBase"]
