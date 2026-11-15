"""JavaScript dialogs, in a real Chrome.

An open alert, confirm or prompt freezes its tab: script evaluation waits, and
so does the input call that opened it. Before dialog handling, the click that
opened an alert, and every tool after it, ran into the 120 s budget and failed
as a generic timeout naming nothing. Now the tool says a dialog is open, at
once, and the dialog can be answered or auto-answered.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from tests.live_harness import data_url, live, sc, with_browser

pytestmark = live

PAGE = """<!doctype html><title>dialogs</title>
<button id="a" onclick="alert('Saved!')">Alert</button>
<button id="c" onclick="document.title = confirm('Delete it?') ? 'yes' : 'no'">Confirm</button>
<button id="p" onclick="document.title = 'name=' + prompt('Your name?', 'anon')">Prompt</button>
"""


async def _timed(c: Any, tool: str, args: dict[str, Any]) -> tuple[Any, float]:
    t = time.monotonic()
    r = await c.call_tool(tool, args)
    return r, time.monotonic() - t


async def _scenario(c: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sc(await c.call_tool("navigate", {"url": data_url(PAGE), "settle": 1}))

    out["click_alert"], out["click_alert_s"] = await _timed(c, "click", {"selector": "#a"})
    out["blocked"], out["blocked_s"] = await _timed(c, "get_text_content", {})
    out["answer"] = sc(await c.call_tool("call_tool", {"name": "handle_dialog", "arguments": {}}))
    out["after"] = sc(await c.call_tool("execute_javascript", {"script": "return 1+1"}))

    await c.call_tool("click", {"selector": "#c"})
    sc(await c.call_tool("call_tool", {"name": "handle_dialog", "arguments": {"accept": False}}))
    out["confirm_title"] = sc(
        await c.call_tool("execute_javascript", {"script": "return document.title"})
    )

    await c.call_tool("click", {"selector": "#p"})
    sc(
        await c.call_tool(
            "call_tool",
            {"name": "handle_dialog", "arguments": {"accept": True, "prompt_text": "Ada"}},
        )
    )
    out["prompt_title"] = sc(
        await c.call_tool("execute_javascript", {"script": "return document.title"})
    )

    sc(
        await c.call_tool(
            "call_tool", {"name": "set_dialog_policy", "arguments": {"policy": "accept"}}
        )
    )
    out["auto_click"] = await c.call_tool("click", {"selector": "#c"})
    await asyncio.sleep(0.3)
    out["auto_title"] = sc(
        await c.call_tool("execute_javascript", {"script": "return document.title"})
    )
    out["log"] = sc(await c.call_tool("call_tool", {"name": "list_dialogs", "arguments": {}}))
    out["no_dialog"] = (
        await c.call_tool("call_tool", {"name": "handle_dialog", "arguments": {}})
    ).structured_content
    return out


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    return asyncio.run(with_browser(_scenario))


def _value(r: Any) -> Any:
    return r["value"] if isinstance(r, dict) and "value" in r else r


def test_the_click_that_opens_a_dialog_says_so_at_once(run: dict[str, Any]) -> None:
    r = run["click_alert"]
    assert r.is_error
    assert r.structured_content["code"] == "dialog_open"
    assert "Saved!" in r.structured_content["error"]
    assert run["click_alert_s"] < 10


def test_tools_on_a_frozen_tab_fail_fast_naming_the_dialog(run: dict[str, Any]) -> None:
    r = run["blocked"]
    assert r.is_error and r.structured_content["code"] == "dialog_open", r.content
    assert "alert" in r.structured_content["error"] and "Saved!" in r.structured_content["error"]
    assert run["blocked_s"] < 5
    assert "handle_dialog" in r.structured_content["hint"]


def test_answering_unfreezes_the_tab(run: dict[str, Any]) -> None:
    assert run["answer"]["type"] == "alert"
    assert _value(run["after"]) == 2


def test_confirm_and_prompt_answers_reach_the_page(run: dict[str, Any]) -> None:
    assert _value(run["confirm_title"]) == "no"
    assert _value(run["prompt_title"]) == "name=Ada"


def test_policy_answers_new_dialogs_and_logs_them(run: dict[str, Any]) -> None:
    assert not run["auto_click"].is_error, run["auto_click"].content
    assert _value(run["auto_title"]) == "yes"
    log = run["log"]
    assert log["policy"] == "accept"
    outcomes = [d["outcome"] for d in log["dialogs"]]
    assert outcomes == ["accepted", "dismissed", "accepted with 'Ada'", "auto-accepted"], outcomes


def test_answering_with_nothing_open_is_an_error(run: dict[str, Any]) -> None:
    assert run["no_dialog"]["code"] == "invalid_argument"
