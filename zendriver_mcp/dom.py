"""Element access that a page cannot observe.

Every tool reaches the DOM through this module. Three rules, each closing a
detection vector the old code had:

1. **Our JavaScript runs in an isolated world**, not the page's. Isolated worlds
   share the DOM but not the JS heap, so a page that wraps
   ``document.querySelector`` or ``Element.prototype.getBoundingClientRect`` to
   spot automation sees none of our calls, and none of our globals.
   ``World.MAIN`` is still available for what genuinely needs page globals
   (``execute_js``, fingerprint overrides).

2. **Element ids live in that isolated world**, in a Map, never as a DOM
   attribute. The old walker stamped a ``data-*`` id attribute on every element,
   which any ``MutationObserver`` or attribute selector
   could read.

3. **Clicks are trusted input events** (CDP ``Input.dispatchMouseEvent``), not
   ``el.click()``. zendriver's ``Element.click()`` produces ``isTrusted:false``
   with no pointer/mousedown/mouseup sequence, and its ``flash()`` inserts a
   keyframes rule and a red ``div`` into the page.

Measured on Chrome 150 (2026-11-08) before relying on it:
``Page.createIsolatedWorld`` with the same world name on the same frame returns
the SAME execution context id, so the id Map persists between calls; after a
navigation the context is gone and CDP answers "Cannot find context with
specified id", which is exactly when ids should expire.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import zendriver as zd
from zendriver import cdp
from zendriver.core.connection import ProtocolException

from zendriver_mcp.errors import (
    Code,
    ElementHiddenError,
    ElementNotFoundError,
    StaleIdError,
    ZendriverMCPError,
)

# Shown in DevTools' context picker only; pages cannot enumerate worlds. Named
# like an ordinary extension world rather than after this project.
WORLD_NAME = "__isolated_content"

WALKER_JS = (Path(__file__).parent / "static" / "js" / "dom_walker.js").read_text(encoding="utf-8")

# Installed into the isolated world on first use. `byId` holds the elements the
# walker numbered; `idOf` hands out ids that are stable for an element's life,
# so an id from an earlier walk still works while its element exists, and a
# number is never reused for a different element.
_STORE_JS = """
(() => {
  if (!globalThis.__zd) {
    const s = { next: 1, byId: new Map(), ids: new WeakMap() };
    s.idOf = (el) => {
      let i = s.ids.get(el);
      if (i === undefined) { i = s.next++; s.ids.set(el, i); }
      s.byId.set(i, el);
      return i;
    };
    s.prune = () => { for (const [i, el] of s.byId) if (!el.isConnected) s.byId.delete(i); };
    s.get = (i) => { const el = s.byId.get(i); return el && el.isConnected ? el : null; };
    globalThis.__zd = s;
  }
  return true;
})()
"""

# MouseEvent.buttons bit for each button while it is held (UI Events spec):
# a real right-button press reports 2, not 1, and pages can read it.
BUTTON_MASK = {"left": 1, "right": 2, "middle": 4, "back": 8, "forward": 16}

_CONTEXT_GONE = ("Cannot find context", "Execution context was destroyed", "No frame with given id")


class World(Enum):
    """Where a script runs."""

    MAIN = "main"
    ISOLATED = "isolated"


@dataclass(frozen=True)
class Handle:
    """A resolved element: its isolated-world object and its DOM node id."""

    object_id: cdp.runtime.RemoteObjectId
    backend_node_id: cdp.dom.BackendNodeId
    label: str


def is_numeric_id(selector: str) -> bool:
    return selector.strip().isdigit()


def _context_gone(exc: BaseException) -> bool:
    return any(s in str(exc) for s in _CONTEXT_GONE)


class Dom:
    """Per-tab isolated world plus element helpers.

    One instance per zendriver Tab, created lazily by :func:`dom_for`. The
    cached context id is dropped whenever CDP says it is gone, and recreated on
    the next call.
    """

    def __init__(self, tab: zd.Tab) -> None:
        self.tab = tab
        self._ctx: cdp.runtime.ExecutionContextId | None = None
        self._lock = asyncio.Lock()

    async def _context(self) -> cdp.runtime.ExecutionContextId:
        async with self._lock:
            if self._ctx is not None:
                return self._ctx
            tree = await self.tab.send(cdp.page.get_frame_tree())
            ctx = await self.tab.send(
                cdp.page.create_isolated_world(
                    frame_id=tree.frame.id_, world_name=WORLD_NAME, grant_univeral_access=False
                )
            )
            await self._raw_eval(_STORE_JS, ctx, by_value=True)
            self._ctx = ctx
            return ctx

    async def _raw_eval(
        self, expression: str, ctx: cdp.runtime.ExecutionContextId | None, *, by_value: bool
    ) -> cdp.runtime.RemoteObject:
        remote, exc = await self.tab.send(
            cdp.runtime.evaluate(
                expression=expression,
                context_id=ctx,
                return_by_value=by_value,
                await_promise=True,
                user_gesture=True,
            )
        )
        if exc is not None:
            raise ZendriverMCPError(f"JavaScript error: {_exception_text(exc)}", code=Code.JS_ERROR)
        return remote

    async def evaluate(self, expression: str, world: World = World.ISOLATED) -> Any:
        """Evaluate an expression and return its JSON value."""
        if world is World.MAIN:
            remote = await self._raw_eval(expression, None, by_value=True)
            return remote.value
        for attempt in (0, 1):
            ctx = await self._context()
            try:
                remote = await self._raw_eval(expression, ctx, by_value=True)
                return remote.value
            except ProtocolException as exc:
                if attempt == 0 and _context_gone(exc):
                    self._ctx = None
                    continue
                raise
        raise AssertionError("unreachable")

    async def walk(self) -> list[dict[str, Any]]:
        """Run the interaction-tree walker in the isolated world."""
        result = await self.evaluate(WALKER_JS)
        return result or []

    async def resolve(self, selector: str) -> Handle:
        """Resolve a CSS selector or a numeric walker id to a handle.

        A numeric id whose world is gone (the page navigated) or whose element
        has left the DOM is a :class:`StaleIdError`, not "not found": the
        recovery differs (re-walk, not re-think the selector).
        """
        sel = selector.strip()
        if is_numeric_id(sel):
            expr = f"globalThis.__zd ? globalThis.__zd.get({int(sel)}) : null"
            missing: ZendriverMCPError = StaleIdError(sel)
        else:
            expr = f"document.querySelector({json.dumps(sel)})"
            missing = ElementNotFoundError(sel)

        for attempt in (0, 1):
            ctx = await self._context()
            try:
                remote, exc = await self.tab.send(
                    cdp.runtime.evaluate(expression=expr, context_id=ctx, return_by_value=False)
                )
            except ProtocolException as e:
                if _context_gone(e):
                    self._ctx = None
                    if is_numeric_id(sel):
                        raise StaleIdError(sel) from e
                    if attempt == 0:
                        continue
                raise
            if exc is not None:
                text = _exception_text(exc)
                if "is not a valid selector" in text or "SyntaxError" in text:
                    raise ZendriverMCPError(
                        f"Invalid CSS selector: {sel}",
                        hint='Quote attribute values and escape special characters, e.g. input[name="q"].',
                    )
                raise ZendriverMCPError(
                    f"JavaScript error resolving {sel}: {text}", code=Code.JS_ERROR
                )
            if remote.object_id is None or remote.subtype == "null":
                raise missing
            node = await self.tab.send(cdp.dom.describe_node(object_id=remote.object_id))
            return Handle(remote.object_id, node.backend_node_id, sel)
        raise missing

    async def handle_for_expression(self, expression: str, label: str) -> Handle:
        """Evaluate an isolated-world expression that yields an element."""
        for attempt in (0, 1):
            ctx = await self._context()
            try:
                remote, exc = await self.tab.send(
                    cdp.runtime.evaluate(
                        expression=expression, context_id=ctx, return_by_value=False
                    )
                )
            except ProtocolException as e:
                if attempt == 0 and _context_gone(e):
                    self._ctx = None
                    continue
                raise
            if exc is not None:
                raise ZendriverMCPError(
                    f"JavaScript error finding {label}: {_exception_text(exc)}", code=Code.JS_ERROR
                )
            if remote.object_id is None or remote.subtype == "null":
                raise ElementNotFoundError(label)
            node = await self.tab.send(cdp.dom.describe_node(object_id=remote.object_id))
            return Handle(remote.object_id, node.backend_node_id, label)
        raise ElementNotFoundError(label)

    async def handle_for_node(self, backend_node_id: cdp.dom.BackendNodeId, label: str) -> Handle:
        """A handle in our isolated world for a node found by other means."""
        ctx = await self._context()
        try:
            remote = await self.tab.send(
                cdp.dom.resolve_node(backend_node_id=backend_node_id, execution_context_id=ctx)
            )
        except ProtocolException as e:
            if _context_gone(e):
                self._ctx = None
                ctx = await self._context()
                remote = await self.tab.send(
                    cdp.dom.resolve_node(backend_node_id=backend_node_id, execution_context_id=ctx)
                )
            else:
                raise ElementNotFoundError(label) from e
        if remote.object_id is None:
            raise ElementNotFoundError(label)
        return Handle(remote.object_id, backend_node_id, label)

    async def id_for(self, handle: Handle) -> int:
        """Number an element the way the walker would, so callers can reuse it."""
        return int(await self.call(handle, "function(el) { return globalThis.__zd.idOf(el); }"))

    async def call(self, selector: str | Handle, function: str, *args: Any) -> Any:
        """Call ``function(el, ...args)`` in the isolated world; JSON in, JSON out."""
        handle = selector if isinstance(selector, Handle) else await self.resolve(selector)
        call_args = [cdp.runtime.CallArgument(object_id=handle.object_id)]
        call_args += [cdp.runtime.CallArgument(value=a) for a in args]
        try:
            remote, exc = await self.tab.send(
                cdp.runtime.call_function_on(
                    function_declaration=function,
                    object_id=handle.object_id,
                    arguments=call_args,
                    return_by_value=True,
                    await_promise=True,
                    user_gesture=True,
                )
            )
        except ProtocolException as e:
            if _context_gone(e):
                self._ctx = None
                if is_numeric_id(handle.label):
                    raise StaleIdError(handle.label) from e
                raise
            raise
        if exc is not None:
            raise ZendriverMCPError(
                f"JavaScript error on {handle.label}: {_exception_text(exc)}", code=Code.JS_ERROR
            )
        return remote.value

    async def center(self, handle: Handle, *, scroll: bool = True) -> tuple[float, float]:
        """Viewport coordinates of the element's first box, scrolled into view.

        Uses CDP's own geometry (``DOM.getContentQuads``) rather than running
        getBoundingClientRect, so a page cannot see the measurement.
        """
        if scroll:
            try:
                await self.tab.send(
                    cdp.dom.scroll_into_view_if_needed(backend_node_id=handle.backend_node_id)
                )
            except ProtocolException:
                pass  # not scrollable (fixed, or detached); the quads say the rest
        try:
            quads = await self.tab.send(
                cdp.dom.get_content_quads(backend_node_id=handle.backend_node_id)
            )
        except ProtocolException as e:
            raise ElementHiddenError(handle.label) from e
        for q in quads or []:
            xs, ys = q[0::2], q[1::2]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            if w > 0 and h > 0:
                return sum(xs) / 4, sum(ys) / 4
        raise ElementHiddenError(handle.label)

    async def obstruction(self, handle: Handle, x: float, y: float) -> str | None:
        """What a click at (x, y) would hit instead of the element, or None.

        Hits on the element, its descendants, its ancestors-within-reason (a
        styled wrapper) and its <label> all count as the element: a custom
        checkbox is usually an invisible <input> under a styled <span> inside
        a <label>, and refusing that click would break most forms. Shadow DOM is
        handled by asking the element's own root, since document.elementFromPoint
        retargets to the outermost host.
        """
        return await self.call(
            handle,
            """function(el, x, y) {
              const root = el.getRootNode();
              const t = (root.elementFromPoint ? root.elementFromPoint(x, y) : null)
                        || document.elementFromPoint(x, y);
              if (!t) return null;
              const related = (a, b) => {
                let n = a;
                while (n) {
                  if (n === b) return true;
                  n = n.parentNode || (n instanceof ShadowRoot ? n.host : null);
                }
                return false;
              };
              if (related(t, el)) return null;
              if (related(el, t) && t !== document.body && t !== document.documentElement) {
                return null;
              }
              const lbl = t.closest && t.closest('label');
              if (lbl && (lbl.control === el || lbl.contains(el))) return null;
              const id = t.id ? '#' + t.id : '';
              const cls = typeof t.className === 'string' && t.className.trim()
                ? '.' + t.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
              return '<' + t.tagName.toLowerCase() + id + cls + '>';
            }""",
            x,
            y,
        )

    async def click(
        self,
        selector: str | Handle,
        *,
        button: str = "left",
        count: int = 1,
        force: bool = False,
    ) -> Handle:
        """A trusted click: move, press, release at the element's centre.

        ``force`` skips the obstruction check and clicks through whatever is on
        top; the page then receives the event wherever it lands, exactly as a
        real user's click would.
        """
        handle = selector if isinstance(selector, Handle) else await self.resolve(selector)
        x, y = await self.center(handle)
        if not force:
            blocker = await self.obstruction(handle, x, y)
            if blocker:
                raise ZendriverMCPError(
                    f"{blocker} covers {handle.label} at ({x:.0f}, {y:.0f}).",
                    code=Code.ELEMENT_OBSCURED,
                    hint="Close the overlay on top (a cookie banner, a modal) or scroll so "
                    "the element is unobstructed. force=true clicks at that point anyway.",
                )
        btn = cdp.input_.MouseButton(button)
        held = BUTTON_MASK.get(button, 1)
        await self.tab.send(cdp.input_.dispatch_mouse_event(type_="mouseMoved", x=x, y=y))
        for n in range(1, count + 1):
            await self.tab.send(
                cdp.input_.dispatch_mouse_event(
                    type_="mousePressed", x=x, y=y, button=btn, click_count=n, buttons=held
                )
            )
            await self.tab.send(
                cdp.input_.dispatch_mouse_event(
                    type_="mouseReleased", x=x, y=y, button=btn, click_count=n, buttons=0
                )
            )
        return handle

    async def focus(self, selector: str | Handle) -> Handle:
        handle = selector if isinstance(selector, Handle) else await self.resolve(selector)
        try:
            await self.tab.send(cdp.dom.focus(backend_node_id=handle.backend_node_id))
        except ProtocolException as e:
            raise ZendriverMCPError(
                f"Cannot focus {handle.label}: {e}",
                hint="Only focusable elements (inputs, buttons, links, [tabindex]) take focus.",
            ) from e
        return handle

    async def set_files(self, selector: str | Handle, files: list[str]) -> Handle:
        handle = selector if isinstance(selector, Handle) else await self.resolve(selector)
        await self.tab.send(
            cdp.dom.set_file_input_files(files=files, backend_node_id=handle.backend_node_id)
        )
        return handle

    async def element(self, selector: str | Handle) -> zd.Element:
        """A zendriver Element for code that still needs one.

        Avoid its ``click()``/``mouse_click()``/``flash()``: see the module docstring.
        """
        handle = selector if isinstance(selector, Handle) else await self.resolve(selector)
        node = await self.tab.send(cdp.dom.describe_node(backend_node_id=handle.backend_node_id))
        return zd.Element(node, self.tab)


def _exception_text(exc: cdp.runtime.ExceptionDetails) -> str:
    if exc.exception is not None and exc.exception.description:
        return exc.exception.description.splitlines()[0]
    return exc.text


_DOMS: dict[int, Dom] = {}


def dom_for(tab: zd.Tab) -> Dom:
    """The Dom helper for a tab, created on first use."""
    key = id(tab)
    d = _DOMS.get(key)
    if d is None or d.tab is not tab:
        d = Dom(tab)
        _DOMS[key] = d
    return d


def forget(tab: zd.Tab | None = None) -> None:
    """Drop cached helpers: one tab's (closed) or all (browser stopped)."""
    if tab is None:
        _DOMS.clear()
    else:
        _DOMS.pop(id(tab), None)
