"""Saving and restoring a logged-in state, in a real Chrome.

save_cookies covered cookies only, while many sites keep their login token in
localStorage. save_session saves both; load_session restores localStorage
for open sites at once, and for the others before their own scripts run on the
next load, so the page's very first read finds it.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

import pytest

from tests.live_harness import Sites, live, sc, with_browser

pytestmark = live

# The page records what it found BEFORE writing anything, so the test sees
# whether the restore happened before the page's own scripts.
APP = """<!doctype html><title>app</title><script>
window.found = localStorage.getItem('token');
if (!window.found) { localStorage.setItem('token', 'tok-' + location.host); document.cookie = 'sid=abc; path=/'; }
</script>"""


async def _scenario(c: Any, sites: Sites, path: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    call = lambda name, args: c.call_tool("call_tool", {"name": name, "arguments": args})  # noqa: E731
    sc(await c.call_tool("navigate", {"url": f"{sites.a}/app.html", "settle": 1}))
    sc(
        await c.call_tool(
            "call_tool", {"name": "new_tab", "arguments": {"url": f"{sites.b}/app.html"}}
        )
    )
    out["export"] = sc(await call("save_session", {"file_path": path}))

    # A fresh browser: new temporary profile, nothing stored.
    sc(await c.call_tool("stop_browser", {}))
    sc(await c.call_tool("start_browser", {"window_size": "1200x900"}))
    # Site B is open before the import (restored now); site A is not (restored on load).
    sc(await c.call_tool("navigate", {"url": f"{sites.b}/blank.html", "settle": 1}))
    out["import"] = sc(await call("load_session", {"file_path": path}))
    out["b_now"] = sc(
        await c.call_tool("execute_javascript", {"script": "return localStorage.getItem('token')"})
    )
    sc(await c.call_tool("navigate", {"url": f"{sites.a}/app.html", "settle": 1}))
    out["a_found"] = sc(await c.call_tool("execute_javascript", {"script": "return window.found"}))
    out["a_cookie"] = sc(
        await c.call_tool("execute_javascript", {"script": "return document.cookie"})
    )
    # After the first load the restore is done: a site that clears its storage keeps it cleared.
    await asyncio.sleep(1)
    sc(await c.call_tool("execute_javascript", {"script": "localStorage.clear()"}))
    sc(await c.call_tool("call_tool", {"name": "reload_page", "arguments": {}}))
    await asyncio.sleep(1)
    out["a_after_logout"] = sc(
        await c.call_tool("execute_javascript", {"script": "return window.found"})
    )
    return out


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    sites = Sites({"/app.html": APP, "/blank.html": "<title>blank</title>"})
    fd, path = tempfile.mkstemp(suffix=".json", prefix="session-")
    os.close(fd)
    try:
        return asyncio.run(with_browser(lambda c: _scenario(c, sites, path)))
    finally:
        sites.close()
        os.unlink(path)


def _v(r: Any) -> Any:
    """A script's value; null comes back as has_value=false with no value."""
    return r.get("value") if isinstance(r, dict) else r


def test_export_covers_cookies_and_both_origins(run: dict[str, Any]) -> None:
    exp = run["export"]
    assert exp["cookies"] >= 1
    assert len(exp["origins"]) == 2, exp


def test_an_open_origin_is_restored_at_once(run: dict[str, Any]) -> None:
    assert len(run["import"]["restored"]) == 1
    assert _v(run["b_now"]).startswith("tok-localhost")


def test_another_origin_finds_its_storage_before_its_own_scripts(run: dict[str, Any]) -> None:
    assert len(run["import"]["on_next_load"]) == 1
    assert _v(run["a_found"]).startswith("tok-127.0.0.1"), run["a_found"]
    assert "sid=abc" in _v(run["a_cookie"])


def test_a_restore_happens_once(run: dict[str, Any]) -> None:
    assert _v(run["a_after_logout"]) is None
