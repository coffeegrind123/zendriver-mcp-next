"""Downloads, in a real Chrome.

Before download capture a file the page offered went to Chrome's default
directory, invisible to the agent and to a remote client. Now each one is saved
under its suggested name in a directory of ours and handed back as a file and a
resource.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.live_harness import Sites, live, sc, with_browser

pytestmark = live

PAGES = {
    "/p.html": '<a id="d" href="/f.csv" download="report.csv">Get</a>'
    '<a id="d2" href="/f.csv" download="report.csv">Again</a>',
    "/f.csv": "a,b\n1,2\n",
}


async def _scenario(c: Any, sites: Sites) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sc(await c.call_tool("navigate", {"url": f"{sites.a}/p.html", "settle": 1}))
    sc(await c.call_tool("click", {"selector": "#d"}))
    # Let it finish before asking: a fast download must not be missed.
    await asyncio.sleep(1.5)
    first = await c.call_tool(
        "call_tool", {"name": "wait_for_download", "arguments": {"timeout": 10}}
    )
    out["first"] = sc(first)
    out["first_types"] = [b.type for b in first.content]
    sc(await c.call_tool("click", {"selector": "#d2"}))
    out["second"] = sc(
        await c.call_tool("call_tool", {"name": "wait_for_download", "arguments": {"timeout": 10}})
    )
    out["none_left"] = (
        await c.call_tool("call_tool", {"name": "wait_for_download", "arguments": {"timeout": 1}})
    ).structured_content
    out["list"] = sc(await c.call_tool("call_tool", {"name": "list_downloads", "arguments": {}}))
    return out


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    sites = Sites(PAGES)
    try:
        return asyncio.run(with_browser(lambda c: _scenario(c, sites)))
    finally:
        sites.close()


def test_a_finished_download_is_returned_with_its_name_and_content(run: dict[str, Any]) -> None:
    first = run["first"]
    assert first["state"] == "completed"
    assert first["filename"] == "report.csv"
    path = Path(first["path"])
    assert path.name == "report.csv"
    assert path.read_text() == "a,b\n1,2\n"
    assert "resource_link" in run["first_types"]


def test_a_second_file_of_the_same_name_does_not_overwrite_the_first(run: dict[str, Any]) -> None:
    assert Path(run["second"]["path"]).name == "report-2.csv"
    assert Path(run["first"]["path"]).exists()


def test_waiting_with_nothing_new_times_out_and_says_so(run: dict[str, Any]) -> None:
    assert run["none_left"]["code"] == "timeout"


def test_list_shows_both(run: dict[str, Any]) -> None:
    states = [d["state"] for d in run["list"]["downloads"]]
    assert states == ["completed", "completed"]
