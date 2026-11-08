# base class for all tool modules
import asyncio
import functools
import inspect
import os
import time
import typing
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer as FastMCP
from pydantic import Field

from zendriver_mcp.dom import Dom, Handle, World, dom_for
from zendriver_mcp.errors import (
    BrowserNotStartedError,
    BrowserUnreachableError,
    Code,
    ElementNotFoundError,
    ToolTimeoutError,
    ZendriverMCPError,
)
from zendriver_mcp.registry import Effect, Reach, ToolSpec, register
from zendriver_mcp.response import Result, error_result
from zendriver_mcp.session import CALL_TAB, BrowserSession, tool_timeout_budget

TAB_DESCRIPTION = (
    "Tab id from list_tabs or new_tab to act on, instead of the active tab. The "
    "active tab does not change. Omit to use the active tab. Example: 'tab_2'"
)
TAB_PARAMETER = inspect.Parameter(
    "tab",
    inspect.Parameter.KEYWORD_ONLY,
    default=None,
    annotation=Annotated[str | None, Field(description=TAB_DESCRIPTION)],
)

# How long a single JavaScript evaluation may take before it is called hung.
# Generous enough for a heavy DOM walk, short enough to stay inside the MCP
# client's own request timeout (30s in this stack's mcp/adapter.json) so the
# caller gets a real message instead of a transport timeout.
JS_TIMEOUT_SECONDS = 20.0


# One definition of the tool budget, shared with the launch path: a Chrome
# launch has to finish inside it or its diagnosis is cancelled before it can be
# reported. Two copies of this number drifting apart is exactly how a dead
# $DISPLAY reached a caller as a bare transport timeout.
_default_tool_timeout = tool_timeout_budget

DEFAULT_TOOL_TIMEOUT = _default_tool_timeout()


def _autostart_enabled() -> bool:
    return os.environ.get("ZENDRIVER_MCP_AUTOSTART_BROWSER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# Opt-in: open the browser on the first tool that needs one, instead of
# returning "Browser not started. Call start_browser first."
#
# Worth having because the alternative costs a client either a wasted turn or a
# tool schema. start_browser's schema is 2.3 KB (~570 tokens) — the largest on
# the server — so a client that only wants the common browse loop otherwise pays
# that just to satisfy a precondition it has no decision to make about.
#
# Off by default: a client that manages its own lifecycle (headless, a proxy, a
# persistent profile) must be able to call start_browser with its own arguments
# before anything else launches Chrome with defaults.
AUTOSTART_BROWSER = _autostart_enabled()

# One lock for the process: two concurrent tool calls on a cold session would
# otherwise both see "no browser" and race two Chrome launches.
_autostart_lock = asyncio.Lock()


def _timeout_error(name: str, budget: float, elapsed: float, exc: BaseException) -> BaseException:
    """Tell "the tool ran too long" apart from "something inside it timed out".

    They arrive as the same exception type and mean opposite things. zendriver
    signals a MISS as a timeout — `find(text=...)` raises
    `asyncio.TimeoutError("Timeout (10s) waiting for any element with text: ...")`
    when nothing matches — so reporting every asyncio.TimeoutError as the tool's
    own budget turns "no element matched" into "the tool was too slow".

    Observed: a click that returned in 10.4s reported "exceeded its 25s time
    budget". The caller read that as a slow browser and spent several turns
    retrying, guessing URLs and falling back to curl. The element simply was not
    there, which zendriver had said clearly and this had thrown away.

    Elapsed time is what separates them: the outer wait_for cannot fire early.
    """
    if elapsed < budget * 0.9:
        message = str(exc).strip() or "an internal operation timed out"
        return ZendriverMCPError(
            f"{message} (this is not the tool's {budget:.0f}s budget — it returned "
            f"after {elapsed:.1f}s, so the operation itself gave up)",
            code=Code.OPERATION_TIMEOUT,
        )
    return ToolTimeoutError(name, budget)


class ToolBase(ABC):
    """base class providing shared functionality for all tool modules"""

    def __init__(self, mcp: FastMCP):
        self._mcp = mcp
        self._session = BrowserSession.get_instance()
        # Auto-register the tool's session-reset hook if it defines one.
        # Stateful tools (interception rules, trace buffers, screencast
        # handles, accessibility uid caches) implement ``_reset_state``;
        # this saves every such subclass from repeating the registration.
        reset = getattr(self, "_reset_state", None)
        if callable(reset):
            self._session.register_reset_callback(reset)
        self._register_tools()

    # ------------------------------------------------------------------
    # registration

    @abstractmethod
    def _register_tools(self) -> None:
        """override in subclasses to register tools with mcp"""

    def _register(
        self,
        fn: "Callable[..., Coroutine[Any, Any, Any]]",
        effect: Effect,
        title: str,
        *,
        reach: Reach = Reach.LOCAL,
        idempotent: bool = False,
        page_scoped: bool = True,
        timeout: float | None = None,
    ) -> None:
        """Register ``fn`` as a tool with its annotations, budget and error model.

        Every registered tool:

        * runs under a time budget, so one hung CDP call can never freeze the
          whole MCP session (DEFAULT_TOOL_TIMEOUT, or ``timeout``);
        * reports every failure as an ``isError`` result with
          ``{error, code, hint}`` (zendriver_mcp/response.py), never as a success string;
        * returns a :class:`Result`, sent as structuredContent plus text;
        * if ``page_scoped``, takes an optional ``tab`` argument that runs this
          one call against another tab without changing the active one.
        """
        budget = float(timeout) if timeout is not None else DEFAULT_TOOL_TIMEOUT
        name = fn.__name__
        group = getattr(self, "GROUP", type(self).__name__)
        spec = ToolSpec(
            name=name,
            title=title,
            group=group,
            effect=effect,
            reach=reach,
            idempotent=idempotent,
            page_scoped=page_scoped,
            timeout=budget,
        )

        # Resolve string annotations against the TOOL's module now. The wrapper
        # lives in this module, and both the SDK's schema builder and its Context
        # detection evaluate annotations against the wrapper's globals, where a
        # tool module's result models are not defined.
        hints = typing.get_type_hints(fn, include_extras=True)
        sig = inspect.signature(fn, eval_str=True)
        params = list(sig.parameters.values())
        takes_tab = page_scoped and "tab" not in sig.parameters
        if takes_tab:
            params.append(TAB_PARAMETER)
            hints["tab"] = TAB_PARAMETER.annotation
        sig = sig.replace(parameters=params)

        @functools.wraps(fn)
        async def guarded(*args: Any, **kwargs: Any) -> Any:
            tab_id = kwargs.pop("tab", None) if takes_tab else None
            token = None
            try:
                if tab_id is not None:
                    token = CALL_TAB.set(self._session.tab_for_call(tab_id))
                result = await self._run_budgeted(fn, name, budget, args, kwargs)
            except Exception as exc:
                return error_result(exc)
            finally:
                if token is not None:
                    CALL_TAB.reset(token)
            if isinstance(result, Result):
                return result.to_call_result()
            return result

        guarded.__signature__ = sig  # type: ignore[attr-defined]
        guarded.__annotations__ = hints
        # functools.wraps sets __wrapped__, which inspect would follow back to the
        # unbound method and expose `self` as a parameter.
        if hasattr(guarded, "__wrapped__"):
            delattr(guarded, "__wrapped__")
        guarded.__zendriver_timeout__ = budget  # type: ignore[attr-defined]

        doc = inspect.getdoc(fn) or ""
        first = " ".join(doc.split("\n\n", 1)[0].split())
        register(
            ToolSpec(**{**spec.__dict__, "summary": first})  # frozen: rebuild with the summary
        )
        self._mcp.tool(title=title, annotations=spec.annotations())(guarded)

    async def _run_budgeted(
        self,
        fn: "Callable[..., Coroutine[Any, Any, Any]]",
        name: str,
        budget: float,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Run one call under its budget, recovering a dead or wedged browser."""
        started = time.monotonic()
        try:
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=budget)
        except asyncio.TimeoutError as exc:
            err = _timeout_error(name, budget, time.monotonic() - started, exc)
            # A wedged Chrome shows up HERE and nowhere else. The recovery
            # below only runs from `except Exception`, so a browser that hangs
            # instead of exiting never reaches it: is_dead() is
            # `browser.stopped`, and a hung process has not stopped. Measured
            # 2026-09-02 -- a crashpad ptrace deadlock left the DevTools port
            # accepting connections and answering nothing for 22 hours while
            # every call returned this same timeout and nothing self-healed.
            # Probing the endpoint is what tells "slow" from "gone".
            if isinstance(err, ToolTimeoutError):
                if await self._session.discard_if_unreachable():
                    raise BrowserUnreachableError(name, budget, AUTOSTART_BROWSER) from exc
            raise err from exc
        except BrowserNotStartedError:
            # Retried once, and only once: if the browser still is not there
            # after a start, something is wrong with the launch and the
            # caller needs to see that error rather than a loop.
            if not AUTOSTART_BROWSER or name == "start_browser":
                raise
            async with _autostart_lock:
                if not self._session.is_running():
                    await self._session.start()
            return await self._retry(fn, name, budget, args, kwargs)
        except Exception:
            # Chrome died underneath a live session — crash, OOM kill, a user
            # closing the window. The session still holds a Browser object,
            # so this arrives as a websocket error rather than
            # BrowserNotStartedError, and every later call fails the same way
            # until someone restarts it by hand.
            #
            # is_dead() is the whole guard: an ordinary tool failure with a
            # healthy browser is re-raised untouched.
            if not AUTOSTART_BROWSER or name == "start_browser":
                raise
            if not self._session.is_dead():
                raise
            async with _autostart_lock:
                if self._session.is_dead():
                    self._session.discard()
                if not self._session.is_running():
                    await self._session.start()
            return await self._retry(fn, name, budget, args, kwargs)

    async def _retry(
        self,
        fn: "Callable[..., Coroutine[Any, Any, Any]]",
        name: str,
        budget: float,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        retried = time.monotonic()
        try:
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=budget)
        except asyncio.TimeoutError as exc:
            raise _timeout_error(name, budget, time.monotonic() - retried, exc) from exc

    # ------------------------------------------------------------------
    # shared state

    @property
    def session(self) -> BrowserSession:
        """get browser session instance"""
        return self._session

    @property
    def mcp(self) -> FastMCP:
        """get mcp server instance"""
        return self._mcp

    @property
    def dom(self) -> Dom:
        """Element helpers for the tab this call acts on (zendriver_mcp/dom.py)."""
        return dom_for(self._session.page)

    # ------------------------------------------------------------------
    # element access: always through zendriver_mcp/dom.py, never by interpolating a
    # selector into page JavaScript

    async def resolve(self, selector: str) -> Handle:
        """A CSS selector or a get_interaction_tree id, resolved to an element."""
        return await self.dom.resolve(selector)

    async def element_call(self, selector: "str | Handle", function: str, *args: Any) -> Any:
        """Run ``function(el, ...args)`` on the element, in the isolated world.

        Arguments travel as CDP values, so no string escaping is ever needed.
        """
        return await asyncio.wait_for(
            self.dom.call(selector, function, *args), timeout=JS_TIMEOUT_SECONDS
        )

    async def handle_by_text(self, text: str) -> Handle:
        """The best visible match for ``text``, found by zendriver's DOM search.

        zendriver searches with CDP's DOM.performSearch, which runs no page
        JavaScript; the match is then re-resolved into our isolated world.
        """
        elem = await self._session.page.find(text, best_match=True)
        if elem is None:
            raise ElementNotFoundError(f"text={text!r}")
        return await self.dom.handle_for_node(elem.backend_node_id, f"text={text!r}")

    async def run_js(
        self,
        script: str,
        timeout: float = JS_TIMEOUT_SECONDS,
        world: World = World.ISOLATED,
    ) -> Any:
        """Evaluate JavaScript and return its JSON value, refusing to hang forever.

        Defaults to the isolated world: it sees the same DOM but not the page's
        globals, and the page cannot see it. Pass ``World.MAIN`` only for code
        that must read or change page globals.

        `page.evaluate()` can stop returning altogether. Observed on a real tab
        that had been left mid-navigation: `get_page_info` answered in 0.4s (it
        reads cached attributes and makes no CDP round trip) while
        `get_text_content` never came back at all, because every JS evaluation
        against that tab hung. A bound turns that into a sentence naming the
        real problem.
        """
        try:
            return await asyncio.wait_for(self.dom.evaluate(script, world), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ToolTimeoutError("run_js", timeout) from exc

    async def wait_for_condition(
        self, check_fn: Callable, timeout: float, poll_interval: float = 0.5
    ) -> bool:
        """wait for a condition to be true within timeout"""
        start = time.time()
        while time.time() - start < timeout:
            if await check_fn():
                return True
            await self._session.page.wait(poll_interval)
        return False

    @staticmethod
    def truncate(text: str, max_length: int, suffix: str = "\n... (truncated)") -> str:
        """truncate text if it exceeds max length"""
        if len(text) > max_length:
            return text[:max_length] + suffix
        return text
