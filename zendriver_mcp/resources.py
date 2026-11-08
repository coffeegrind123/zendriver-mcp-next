"""Resources, completions and prompts.

Resources are what a client can read without calling a tool:

  browser://artifacts/{name}          a file a tool wrote (screenshot, trace,
                                      heap snapshot, video, cookie export)
  browser://tabs                      open tabs this client can see
  browser://tabs/{tab}/info           URL and title of one tab
  browser://tabs/{tab}/console        its console messages
  browser://tabs/{tab}/network        its network log

``{tab}`` and ``{name}`` complete from what exists. Opening, closing or adopting
a tab publishes ``resources/list_changed`` to ``subscriptions/listen`` streams.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.shared.subscriptions import ResourcesListChanged
from mcp_types import Completion, ResourceTemplateReference

from zendriver_mcp import artifacts
from zendriver_mcp.session import BrowserSession

JSON = "application/json"
LOG_LIMIT = 500


def _session() -> BrowserSession:
    return BrowserSession.get_instance()


def _tabs() -> dict[str, str]:
    s = _session()
    return s.get_all_tabs() if s.is_running() else {}


def _tab_entries(entries: list[dict[str, Any]], tab: str) -> str:
    _session().tab_for_call(tab)  # not found / not yours -> error, not an empty list
    return json.dumps([e for e in entries if e.get("tab") == tab][-LOG_LIMIT:])


def install_resources(mcp: MCPServer) -> None:
    @mcp.resource(
        "browser://artifacts/{name}",
        title="Artifact",
        description="A file a tool wrote in this session: screenshot, trace, heap snapshot, video, export.",
    )
    def artifact(name: str) -> bytes:
        found = artifacts.lookup(name)
        if found is None:
            raise ValueError(f"No artifact named {name}; tool results list their files' URIs.")
        path, _mime = found
        return path.read_bytes()

    @mcp.resource(
        "browser://tabs",
        title="Open tabs",
        description="Open tabs this client can see, as {id: url}.",
        mime_type=JSON,
    )
    def tabs() -> str:
        return json.dumps(_tabs())

    @mcp.resource(
        "browser://tabs/{tab}/info",
        title="Tab info",
        description="URL and title of one tab.",
        mime_type=JSON,
    )
    def tab_info(tab: str) -> str:
        t = _session().tab_for_call(tab)
        return json.dumps(
            {"id": tab, "url": getattr(t, "url", None), "title": getattr(t, "title", None)}
        )

    @mcp.resource(
        "browser://tabs/{tab}/console",
        title="Tab console",
        description=f"The last {LOG_LIMIT} console messages from one tab.",
        mime_type=JSON,
    )
    def tab_console(tab: str) -> str:
        return _tab_entries(_session().get_console_logs(limit=10_000), tab)

    @mcp.resource(
        "browser://tabs/{tab}/network",
        title="Tab network log",
        description=f"The last {LOG_LIMIT} network responses and failures from one tab.",
        mime_type=JSON,
    )
    def tab_network(tab: str) -> str:
        return _tab_entries(_session().get_network_logs(limit=10_000), tab)

    @mcp.completion()
    async def complete(ref: Any, argument: Any, context: Any) -> Completion | None:
        if not isinstance(ref, ResourceTemplateReference):
            return None
        prefix = (argument.value or "").lower()
        if argument.name == "tab":
            values = list(_tabs())
        elif argument.name == "name":
            values = [n for n, _p, _m in artifacts.listing()]
        else:
            return None
        hits = sorted(v for v in values if v.lower().startswith(prefix))
        return Completion(values=hits[:100], total=len(hits), has_more=len(hits) > 100)

    bus = mcp._subscriptions

    def tabs_changed() -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(bus.publish(ResourcesListChanged()))

    _session().add_tab_listener(tabs_changed)

    _install_prompts(mcp)


def _install_prompts(mcp: MCPServer) -> None:
    @mcp.prompt(title="Read a page", description="Open a URL and return its readable text.")
    def read_page(url: str) -> str:
        return (
            f"Open {url} with navigate (it waits for the page to settle), then read it with "
            "get_text_content, paging with offset while next_offset is set. If the text is "
            "empty or a challenge page, take a screenshot and say what you see."
        )

    @mcp.prompt(
        title="Fill and submit a form", description="Fill a form from field values and submit it."
    )
    def fill_form_task(url: str, fields: str) -> str:
        return (
            f"Open {url}. Run get_interaction_tree to find the form's inputs. Fill these "
            f"values: {fields}. Use type_text per input (clear_input first if it has a value), "
            "select_option for <select>, then click the submit button. Afterwards confirm the "
            "outcome with get_page_info and get_text_content, and report any validation errors."
        )

    @mcp.prompt(
        title="Audit a page",
        description="Security headers, performance and accessibility of one page.",
    )
    def audit_page(url: str) -> str:
        return (
            f"Open {url}. Run run_security_audit, then start run_lighthouse with jobs "
            "(action='start') since it takes a minute, and meanwhile take "
            "get_accessibility_snapshot. Wait for the job, then report the top findings of each "
            "with concrete fixes."
        )
