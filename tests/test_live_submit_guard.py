"""The opt-in submit guard, in a real Chrome.

With ZENDRIVER_MCP_SUBMIT_GUARD=1 an agent cannot submit a form or press a
pay/send/delete-style control until a person approves it through
allow_next_submit. Each case below is checked from the page's side: a blocked
action must never reach the page, an allowed one must, and the guard must leave
no trace (no patched prototypes, no attributes).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from tests.live_harness import data_url, live, sc, with_browser
from zendriver_mcp.submit_guard import ENV

pytestmark = live

PAGE = """<!doctype html><title>guard</title>
<script>
window.log = [];
addEventListener('submit', (e) => { e.preventDefault(); log.push('submit:' + e.target.id); }, true);
addEventListener('click', (e) => log.push('click:' + (e.target.id || e.target.tagName)), true);
new MutationObserver((ms) => ms.forEach((m) => m.type === 'attributes' &&
  log.push('attr:' + m.attributeName))).observe(document, {attributes: true, subtree: true});
</script>
<form id="signup" method="post"><input id="email" name="email"><button id="create">Create account</button></form>
<form id="search" role="search"><input id="q" type="search"><button id="go">Go</button></form>
<form id="two"><input id="f1"><input id="f2"></form>
<form id="one"><input id="solo"></form>
<form id="ext"><input id="extfield"></form><button id="extbtn" form="ext">Save</button>
<button id="pay" type="button">Pay now</button>
<button id="menu" type="button">Open menu</button>
<textarea id="msg"></textarea>
"""


async def _scenario(c: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sc(await c.call_tool("navigate", {"url": data_url(PAGE), "settle": 1}))
    os.environ[ENV] = "1"
    try:
        call = lambda name, args: c.call_tool("call_tool", {"name": name, "arguments": args})  # noqa: E731
        res = lambda r: r.structured_content  # noqa: E731
        out["click_submit"] = res(await c.call_tool("click", {"selector": "#create"}))
        out["enter_field"] = res(
            await c.call_tool("press_key", {"key": "Enter", "selector": "#email"})
        )
        out["enter_search"] = res(
            await c.call_tool("press_key", {"key": "Enter", "selector": "#q"})
        )
        out["enter_two"] = res(await c.call_tool("press_key", {"key": "Enter", "selector": "#f1"}))
        out["enter_one"] = res(
            await c.call_tool("press_key", {"key": "Enter", "selector": "#solo"})
        )
        out["enter_ext"] = res(
            await c.call_tool("press_key", {"key": "Enter", "selector": "#extfield"})
        )
        out["click_pay"] = res(await c.call_tool("click", {"text": "Pay now"}))
        out["click_menu"] = res(await c.call_tool("click", {"selector": "#menu"}))
        out["chord"] = res(
            await c.call_tool(
                "press_key", {"key": "Enter", "selector": "#msg", "modifiers": ["Control"]}
            )
        )
        out["plain_enter_textarea"] = res(
            await c.call_tool("press_key", {"key": "Enter", "selector": "#msg"})
        )
        out["submit_form"] = res(await call("submit_form", {"selector": "#signup"}))
        pos = sc(
            await c.call_tool(
                "execute_javascript",
                {
                    "script": "const r = document.getElementById('pay').getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]"
                },
            )
        )["value"]
        out["mouse_pay"] = res(await call("mouse_click", {"x": pos[0], "y": pos[1]}))
        out["log_blocked"] = sc(
            await c.call_tool("execute_javascript", {"script": "return log.slice()"})
        )["value"]

        out["allow"] = sc(await call("allow_next_submit", {}))
        out["click_after_allow"] = res(await c.call_tool("click", {"selector": "#create"}))
        out["click_again"] = res(await c.call_tool("click", {"selector": "#create"}))
        out["page"] = sc(
            await c.call_tool(
                "execute_javascript",
                {
                    "script": "return {log, native: HTMLFormElement.prototype.submit.toString().includes('[native code]') && HTMLFormElement.prototype.requestSubmit.toString().includes('[native code]')}"
                },
            )
        )["value"]
    finally:
        os.environ.pop(ENV, None)
    out["guard_off"] = (await c.call_tool("click", {"selector": "#create"})).structured_content
    return out


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    return asyncio.run(with_browser(_scenario))


def _blocked(r: dict[str, Any]) -> bool:
    return r.get("code") == "submit_blocked"


def test_form_submissions_are_blocked(run: dict[str, Any]) -> None:
    assert _blocked(run["click_submit"]), run["click_submit"]
    assert "Create account" in run["click_submit"]["error"]
    assert _blocked(run["enter_field"])
    assert _blocked(run["enter_one"]), "one field and no button: Enter submits"
    assert _blocked(run["enter_ext"]), "the default button can live outside the form"
    assert _blocked(run["submit_form"])


def test_committing_labels_are_blocked_however_they_are_clicked(run: dict[str, Any]) -> None:
    assert _blocked(run["click_pay"])
    assert _blocked(run["mouse_pay"])
    assert _blocked(run["chord"])


def test_harmless_actions_pass(run: dict[str, Any]) -> None:
    for key in ("enter_search", "enter_two", "click_menu", "plain_enter_textarea"):
        assert not _blocked(run[key]), (key, run[key])


def test_blocked_actions_never_reach_the_page(run: dict[str, Any]) -> None:
    log = run["log_blocked"]
    assert "submit:signup" not in log
    assert "click:pay" not in log and "click:create" not in log
    assert "submit:search" in log, "the search form did submit"


def test_one_approval_lets_one_action_through(run: dict[str, Any]) -> None:
    assert run["allow"]["guard_enabled"] is True
    assert not _blocked(run["click_after_allow"]), run["click_after_allow"]
    assert "submit:signup" in run["page"]["log"]
    assert _blocked(run["click_again"]), "the approval is used up"


def test_the_guard_leaves_no_trace(run: dict[str, Any]) -> None:
    assert run["page"]["native"] is True
    assert not [e for e in run["page"]["log"] if e.startswith("attr:")]


def test_off_by_default(run: dict[str, Any]) -> None:
    assert not _blocked(run["guard_off"] or {}), run["guard_off"]
