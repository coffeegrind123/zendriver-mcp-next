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
_declare_explicit_required(mcp)

__all__ = ["mcp", "CATALOG", "ToolBase"]
