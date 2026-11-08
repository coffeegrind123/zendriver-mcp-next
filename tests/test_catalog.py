"""The fixed catalogue: what tools/list shows, and the meta-tools behind it."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest
from mcp import Client

from zendriver_mcp.tools import CATALOG, mcp
from zendriver_mcp.tools.catalog import CORE, META, resolve_refs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _listed(env: dict[str, str]) -> list[str]:
    code = "import json;from zendriver_mcp.tools import mcp;print(json.dumps(sorted(mcp._tool_manager._tools)))"
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("ZENDRIVER_MCP_")}
    full_env.update(env)
    out = subprocess.check_output([sys.executable, "-c", code], env=full_env, cwd=ROOT)
    return json.loads(out.decode().strip().splitlines()[-1])


def test_default_lists_core_plus_meta_only() -> None:
    listed = _listed({})
    assert set(listed) == set(CORE) | set(META)


def test_full_surface_lists_everything() -> None:
    listed = _listed({"ZENDRIVER_MCP_SURFACE": "full"})
    assert set(CORE) <= set(listed)
    assert len(listed) == len(CATALOG) + len(META)


@pytest.mark.parametrize(
    "env", [{"ZENDRIVER_MCP_GATEWAY": "1"}, {"ZENDRIVER_MCP_PROFILE": "gateway"}]
)
def test_old_gateway_switches_mean_catalog(env: dict[str, str]) -> None:
    assert set(_listed(env)) == set(CORE) | set(META)


def test_core_tools_carry_always_load() -> None:
    async def run() -> dict[str, dict]:
        async with Client(mcp) as c:
            return {t.name: (t.meta or {}) for t in (await c.list_tools()).tools}

    metas = asyncio.run(run())
    for name in CORE + META:
        assert metas[name].get("anthropic/alwaysLoad") is True, name


async def _call(name: str, args: dict) -> object:
    async with Client(mcp) as c:
        return await c.call_tool(name, args)


def test_search_finds_hidden_tools() -> None:
    r = asyncio.run(_call("search_tools", {"query": "read cookies"}))
    names = [m["name"] for m in r.structured_content["matches"]]
    assert "get_cookies" in names or "list_all_cookies" in names, names


def test_call_tool_reports_unknown_names_with_a_suggestion() -> None:
    r = asyncio.run(_call("call_tool", {"name": "get_cookie", "arguments": {}}))
    assert r.is_error
    assert r.structured_content["code"] == "unknown_tool"
    assert "get_cookies" in r.structured_content["hint"]


def test_call_tool_validates_arguments() -> None:
    r = asyncio.run(_call("call_tool", {"name": "get_element_text", "arguments": {}}))
    assert r.is_error
    assert r.structured_content["code"] == "invalid_argument"
    assert "selector" in r.structured_content["error"]


def test_call_tool_passes_errors_through_as_errors() -> None:
    r = asyncio.run(_call("call_tool", {"name": "get_page_info", "arguments": {}}))
    assert r.is_error
    assert r.structured_content["code"] in {"page_not_loaded", "browser_not_started"}


def test_describe_tool_gives_both_schemas() -> None:
    r = asyncio.run(_call("describe_tool", {"name": "navigate"}))
    sc = r.structured_content
    assert "url" in sc["input_schema"]["properties"]
    assert "settled" in sc["output_schema"]["properties"]
    assert sc["annotations"]["readOnlyHint"] is False


def test_batch_stops_at_the_first_failure_and_says_where() -> None:
    r = asyncio.run(
        _call(
            "batch",
            {
                "steps": [
                    {"tool": "get_browser_status", "arguments": {}},
                    {"tool": "get_page_info", "arguments": {}},
                    {"tool": "get_browser_status", "arguments": {}},
                ]
            },
        )
    )
    sc = r.structured_content
    assert sc["failed_at"] == 1
    assert len(sc["steps"]) == 2
    assert sc["steps"][0]["ok"] and not sc["steps"][1]["ok"]


def test_batch_refuses_unknown_tools_before_running_anything() -> None:
    r = asyncio.run(_call("batch", {"steps": [{"tool": "nope", "arguments": {}}]}))
    assert r.is_error
    assert r.structured_content["code"] == "unknown_tool"


def test_refs_resolve_whole_and_inline() -> None:
    results = [{"tabs": [{"id": "tab_1", "url": "https://a.example/"}]}]
    assert resolve_refs({"tab": "$0.tabs.0.id"}, results) == {"tab": "tab_1"}
    assert resolve_refs("go ${0.tabs.0.url}x", results) == "go https://a.example/x"
    assert resolve_refs({"n": 3, "l": ["$0.tabs.0.id"]}, results) == {"n": 3, "l": ["tab_1"]}


def test_refs_to_missing_fields_or_future_steps_fail_clearly() -> None:
    from zendriver_mcp.errors import InvalidArgumentError

    with pytest.raises(InvalidArgumentError, match="no 'nope'"):
        resolve_refs("$0.nope", [{"a": 1}])
    with pytest.raises(InvalidArgumentError, match="has not run yet"):
        resolve_refs("$3.a", [{"a": 1}])


def test_jobs_run_a_tool_in_the_background() -> None:
    async def run() -> tuple[dict, dict]:
        async with Client(mcp) as c:
            started = await c.call_tool(
                "jobs", {"action": "start", "tool": "get_browser_status", "arguments": {}}
            )
            jid = started.structured_content["jobs"][0]["id"]
            waited = await c.call_tool("jobs", {"action": "wait", "job_id": jid, "timeout": 5})
            return started.structured_content, waited.structured_content

    started, waited = asyncio.run(run())
    assert started["jobs"][0]["state"] == "working"
    job = waited["jobs"][0]
    assert job["state"] == "completed", job
    assert job["result"]["running"] is False


def test_jobs_unknown_id() -> None:
    r = asyncio.run(_call("jobs", {"action": "get", "job_id": "job_nope"}))
    assert r.is_error and r.structured_content["code"] == "job_not_found"
