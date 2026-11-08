"""A hung `page.evaluate()` must be a sentence, not silence.

Observed 2026-08-17 on a tab left mid-navigation: `get_page_info` answered in
0.4s — it reads cached attributes and makes no CDP round trip — while
`get_text_content` never returned at all, because every JS evaluation against
that tab hung. The caller saw only its own transport timeout, followed by a dump
of the tool's parameters, which names nothing about the browser and reads as a
bad argument.
"""

from __future__ import annotations

import asyncio

import pytest

from src.errors import ToolTimeoutError
from src.tools.base import JS_TIMEOUT_SECONDS, ToolBase


class _HungPage:
    """A tab whose every CDP command never answers, like the real thing."""

    async def send(self, _cmd):
        await asyncio.Event().wait()


class _FastPage:
    """A tab that answers Runtime.evaluate (and the isolated-world setup)."""

    def __init__(self) -> None:
        self.scripts: list[tuple[str, object]] = []

    async def send(self, cmd):
        request = next(cmd)  # zendriver CDP commands are generators: dict first
        method, params = request["method"], request.get("params", {})
        if method == "Page.getFrameTree":
            reply = {
                "frameTree": {
                    "frame": {
                        "id": "F",
                        "loaderId": "L",
                        "url": "about:blank",
                        "domainAndRegistry": "",
                        "securityOrigin": "null",
                        "mimeType": "text/html",
                        "secureContextType": "InsecureScheme",
                        "crossOriginIsolatedContextType": "NotIsolated",
                        "gatedAPIFeatures": [],
                    }
                }
            }
        elif method == "Page.createIsolatedWorld":
            reply = {"executionContextId": 7}
        elif method == "Runtime.evaluate":
            self.scripts.append((params["expression"], params.get("contextId")))
            reply = {"result": {"type": "string", "value": "ok"}}
        else:  # pragma: no cover - a command this test did not expect
            raise AssertionError(method)
        try:
            cmd.send(reply)
        except StopIteration as stop:
            return stop.value
        raise AssertionError("command did not finish")


class _Session:
    def __init__(self, page) -> None:
        self.page = page


class _Tools(ToolBase):
    """ToolBase is abstract; the smallest concrete thing that has run_js."""

    def _register_tools(self) -> None:  # pragma: no cover - never called here
        pass


def _tools(page) -> ToolBase:
    tools = _Tools.__new__(_Tools)
    tools._session = _Session(page)
    return tools


def test_a_hung_evaluation_raises_a_named_timeout() -> None:
    with pytest.raises(ToolTimeoutError) as caught:
        asyncio.run(_tools(_HungPage()).run_js("document.title", timeout=0.2))
    message = str(caught.value)
    assert "run_js" in message
    assert "time budget" in message


def test_the_budget_is_below_the_clients_request_timeout() -> None:
    # pi's adapter gives up at 30s (mcp/adapter.json requestTimeoutMs). A JS
    # bound at or above that would never be the thing that answers first, which
    # is the entire point of having it.
    assert JS_TIMEOUT_SECONDS < 30.0


def test_an_ordinary_evaluation_runs_in_the_isolated_world() -> None:
    page = _FastPage()
    assert asyncio.run(_tools(page).run_js("document.title")) == "ok"
    # The store bootstrap, then the script, both in the isolated context (7).
    assert page.scripts[-1] == ("document.title", 7)
    assert all(ctx == 7 for _expr, ctx in page.scripts)


def test_main_world_is_opt_in() -> None:
    from src.dom import World

    page = _FastPage()
    assert asyncio.run(_tools(page).run_js("window.x", world=World.MAIN)) == "ok"
    assert page.scripts == [("window.x", None)]
