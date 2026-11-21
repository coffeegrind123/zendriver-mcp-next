"""Element access that a page cannot observe.

Every tool reaches the DOM through this module. Three rules, each closing a
detection vector the old code had:

1. **Our JavaScript runs in an isolated world**, not the page's. Isolated worlds
   share the DOM but not the JS heap, so a page that wraps
   ``document.querySelector`` or ``Element.prototype.getBoundingClientRect`` to
   spot automation sees none of our calls, and none of our globals.
   ``World.MAIN`` is still available for what genuinely needs page globals
   (``execute_javascript``, fingerprint overrides).

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
the SAME execution context id, so the id Map persists between calls.

A cached context id is only trusted for the document it was made in (the
frame's loader id). Measured 2026-11-15: after a cross-site navigation the new
renderer process numbers its contexts from 1 again, and the old id resolved to
a DIFFERENT live context in the new page (an extension's content-script world),
so "the id still answers" does not mean "it is still our world".

Frames (measured 2026-11-15, Chrome 157):

- Same-process iframes are in the tab's own ``Page.getFrameTree``; an isolated
  world is made per frame from the tab's session, and ``DOM.getContentQuads``
  already answers in top-level viewport coordinates.
- Out-of-process iframes (cross-site) are separate targets, reached through
  ``Tab.get_frames()``. Their quads are relative to the iframe's own viewport,
  so the owning ``<iframe>``'s content box is added at each such level.
- Trusted input always goes to the top-level tab: a click at the corrected
  point, and ``Input.insertText`` after ``DOM.focus`` in the frame's session,
  both arrive in the frame with ``isTrusted`` true.

Element ids are tab-wide: ``Dom`` maps each one to (frame, document, id in
that frame's world), and never reuses a number, so an id from a page that has
since navigated is stale instead of naming some new element.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import zendriver as zd
from zendriver import cdp
from zendriver.core.connection import ProtocolException

from zendriver_mcp.errors import (
    AmbiguousError,
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

# Whether `el` is `root` or inside it, crossing shadow boundaries.
_CONTAINS_JS = """
  const inside = (root, n) => {
    for (; n; n = n.parentNode || (n instanceof ShadowRoot ? n.host : null)) {
      if (n === root) return true;
    }
    return false;
  };
"""
_CONTAINS_FN = "function(root, el) {" + _CONTAINS_JS + " return inside(root, el); }"

# The walker's local ids, of those given, whose elements sit inside `root`.
_WITHIN_FN = (
    "function(root, ids) {" + _CONTAINS_JS + " const S = globalThis.__zd;"
    " return ids.filter((i) => { const el = S && S.get(i); return !!el && inside(root, el); }); }"
)

# MouseEvent.buttons bit for each button while it is held (UI Events spec):
# a real right-button press reports 2, not 1, and pages can read it.
BUTTON_MASK = {"left": 1, "right": 2, "middle": 4, "back": 8, "forward": 16}

_WALK_GROUP = "zd-walk"

# Resolves after two animation frames, or 250 ms if the tab is not drawing.
_TWO_FRAMES_JS = (
    "new Promise((r) => { let done = false; const fin = () => { if (!done) { done = true; r(true); } };"
    " requestAnimationFrame(() => requestAnimationFrame(fin)); setTimeout(fin, 250); })"
)

# Wheel gestures: pixels per second, and how the end of a scroll is detected
# (the position unchanged across one poll).
_SCROLL_SPEED = 3000
_SETTLE_SECONDS = 0.05
_SETTLE_POLLS = 40

_CONTEXT_GONE = ("Cannot find context", "Execution context was destroyed", "No frame with given id")

# What a click at (x, y) would hit instead of `el`, or null. Hits on the
# element, its descendants, its ancestors-within-reason (a styled wrapper) and
# its <label> all count as the element: a custom checkbox is usually an
# invisible <input> under a styled <span> inside a <label>, and refusing that
# click would break most forms. Shadow DOM is handled by asking the element's
# own root, since document.elementFromPoint retargets to the outermost host.
# (x, y) are in the element's own frame's viewport.
_OBSTRUCTION_FN = """function(el, x, y) {
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
}"""


class World(Enum):
    """Where a script runs."""

    MAIN = "main"
    ISOLATED = "isolated"


@dataclass(eq=False)
class Frame:
    """One document in a tab: the main frame, or an iframe at any depth.

    ``session`` is the CDP target the frame's document lives in: the tab itself,
    or for a cross-site iframe its own out-of-process target. ``number`` is what
    the interaction tree shows as ``f`` (0 for the main frame).
    """

    session: zd.Tab
    frame_id: str
    loader_id: str
    url: str
    parent: Frame | None = None
    parent_id: str | None = None
    # The root frame of an out-of-process target: its session's coordinates are
    # relative to this frame's own viewport, not the tab's.
    process_root: bool = False
    number: int = 0
    children: list[Frame] = field(default_factory=list)

    @property
    def is_main(self) -> bool:
        return self.parent is None and not self.process_root


@dataclass(frozen=True)
class Handle:
    """A resolved element: its isolated-world object, its DOM node, and its frame."""

    object_id: cdp.runtime.RemoteObjectId
    backend_node_id: cdp.dom.BackendNodeId
    label: str
    frame: Frame


def is_numeric_id(selector: str) -> bool:
    return selector.strip().isdigit()


def _context_gone(exc: BaseException) -> bool:
    return any(s in str(exc) for s in _CONTEXT_GONE)


def _frame(session: zd.Tab, f: cdp.page.Frame, parent: Frame | None) -> Frame:
    return Frame(session, str(f.id_), str(f.loader_id), f.url + (f.url_fragment or ""), parent)


def _flatten(
    tree: cdp.page.FrameTree, session: zd.Tab, parent: Frame | None, out: list[Frame]
) -> Frame:
    frame = _frame(session, tree.frame, parent)
    if parent is not None:
        parent.children.append(frame)
    out.append(frame)
    for child in tree.child_frames or []:
        _flatten(child, session, frame, out)
    return frame


class Dom:
    """Isolated worlds and element helpers for one tab and all of its frames.

    One instance per zendriver Tab, created lazily by :func:`dom_for`.
    """

    def __init__(self, tab: zd.Tab) -> None:
        self.tab = tab
        self._lock = asyncio.Lock()
        # frame id -> (loader id the world was made in, its context id)
        self._worlds: dict[str, tuple[str, cdp.runtime.ExecutionContextId]] = {}
        # tab-wide element ids: id -> (frame id, loader id, id in that world)
        self._ids: dict[int, tuple[str, str, int]] = {}
        self._gids: dict[tuple[str, str, int], int] = {}
        self._next_id = 1
        # frame numbers shown as `f`, stable while the main document stays
        self._numbers: dict[str, int] = {}
        self._next_number = 1
        self._main_loader: str | None = None

    # ------------------------------------------------------------------
    # frames

    async def main_frame(self) -> Frame:
        tree = await self.tab.send(cdp.page.get_frame_tree())
        return _frame(self.tab, tree.frame, None)

    async def frames(self) -> list[Frame]:
        """Every frame in the tab, main first, parents before their children."""
        out: list[Frame] = []
        tree = await self.tab.send(cdp.page.get_frame_tree())
        _flatten(tree, self.tab, None, out)
        try:
            remotes = await self.tab.get_frames()
        except ProtocolException:
            remotes = []
        by_id = {f.frame_id: f for f in out}
        for target in remotes:
            try:
                sub = await target.send(cdp.page.get_frame_tree())
            except ProtocolException:
                continue  # the iframe went away between listing and asking
            parent_id = str(target.target.parent_frame_id) if target.target else None
            parent = by_id.get(parent_id) if parent_id else None
            start = len(out)
            root = _flatten(sub, target, parent, out)
            root.process_root = True
            root.parent_id = parent_id
            for f in out[start:]:
                by_id[f.frame_id] = f
        self._renumber(out)
        return out

    def _renumber(self, frames: list[Frame]) -> None:
        main = frames[0]
        if main.loader_id != self._main_loader:
            # A new main document: every frame and id of the old one is gone.
            self._main_loader = main.loader_id
            self._numbers.clear()
            self._next_number = 1
        live = {(f.frame_id, f.loader_id) for f in frames}
        for gid, (fid, loader, _local) in list(self._ids.items()):
            if (fid, loader) not in live:
                del self._ids[gid]
                del self._gids[(fid, loader, _local)]
        for fid in [k for k in self._worlds if k not in {f.frame_id for f in frames}]:
            del self._worlds[fid]
        for f in frames[1:]:
            n = self._numbers.get(f.frame_id)
            if n is None:
                n = self._numbers[f.frame_id] = self._next_number
                self._next_number += 1
            f.number = n

    async def _owner(self, frame: Frame) -> tuple[zd.Tab, cdp.dom.BackendNodeId]:
        """The <iframe> element that holds ``frame``, and the session it lives in."""
        assert frame.parent is not None
        session = frame.parent.session
        owner = await session.send(
            cdp.dom.get_frame_owner(frame_id=cdp.page.FrameId(frame.frame_id))
        )
        return session, owner[0]

    async def _owner_box(self, frame: Frame) -> tuple[float, float, float, float] | None:
        """The owning <iframe>'s content box (x, y, w, h) in its session's coordinates."""
        try:
            session, backend = await self._owner(frame)
            model = await session.send(cdp.dom.get_box_model(backend_node_id=backend))
        except ProtocolException:
            return None
        c = model.content
        return c[0], c[1], float(model.width), float(model.height)

    async def _session_offset(self, frame: Frame) -> tuple[float, float]:
        """What to add to coordinates from ``frame.session`` to get tab coordinates."""
        root = frame
        while not root.process_root and root.parent is not None:
            root = root.parent
        if not root.process_root:
            return 0.0, 0.0
        return await self.viewport_origin(root)

    async def viewport_origin(self, frame: Frame) -> tuple[float, float]:
        """The tab coordinates of ``frame``'s own viewport origin."""
        if frame.parent is None:
            return 0.0, 0.0
        box = await self._owner_box(frame)
        if box is None:
            raise ElementHiddenError(f"frame {frame.number} ({frame.url})")
        ox, oy = await self._session_offset(frame.parent)
        return ox + box[0], oy + box[1]

    async def frame_shown(self, frame: Frame) -> bool:
        """Whether the frame's <iframe> has a rendered box (zero-size tracker frames do not)."""
        if frame.parent is None:
            return True
        box = await self._owner_box(frame)
        return box is not None and box[2] > 0 and box[3] > 0

    # ------------------------------------------------------------------
    # isolated worlds

    async def _context(self, frame: Frame) -> cdp.runtime.ExecutionContextId:
        async with self._lock:
            cached = self._worlds.get(frame.frame_id)
            if cached is not None and cached[0] == frame.loader_id:
                return cached[1]
            ctx = await frame.session.send(
                cdp.page.create_isolated_world(
                    frame_id=cdp.page.FrameId(frame.frame_id),
                    world_name=WORLD_NAME,
                    grant_univeral_access=False,
                )
            )
            await self._raw_eval(frame.session, _STORE_JS, ctx, by_value=True)
            self._worlds[frame.frame_id] = (frame.loader_id, ctx)
            return ctx

    def _forget_world(self, frame: Frame) -> None:
        self._worlds.pop(frame.frame_id, None)

    async def _raw_eval(
        self,
        session: zd.Tab,
        expression: str,
        ctx: cdp.runtime.ExecutionContextId | None,
        *,
        by_value: bool,
    ) -> cdp.runtime.RemoteObject:
        remote, exc = await session.send(
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
        """Evaluate an expression in the main frame and return its JSON value."""
        if world is World.MAIN:
            remote = await self._raw_eval(self.tab, expression, None, by_value=True)
            return remote.value
        for attempt in (0, 1):
            frame = await self.main_frame()
            ctx = await self._context(frame)
            try:
                remote = await self._raw_eval(frame.session, expression, ctx, by_value=True)
                return remote.value
            except ProtocolException as exc:
                if attempt == 0 and _context_gone(exc):
                    self._forget_world(frame)
                    continue
                raise
        raise AssertionError("unreachable")

    async def evaluate_in(self, frame: Frame, expression: str) -> Any:
        """Evaluate an expression in ``frame``'s isolated world and return its JSON value."""
        for attempt in (0, 1):
            ctx = await self._context(frame)
            try:
                remote = await self._raw_eval(frame.session, expression, ctx, by_value=True)
                return remote.value
            except ProtocolException as exc:
                if attempt == 0 and _context_gone(exc):
                    self._forget_world(frame)
                    continue
                raise
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------
    # tab-wide element ids

    def global_id(self, frame: Frame, local: int) -> int:
        """The tab-wide id for element ``local`` of ``frame``'s world."""
        key = (frame.frame_id, frame.loader_id, int(local))
        gid = self._gids.get(key)
        if gid is None:
            gid = self._next_id
            self._next_id += 1
            self._gids[key] = gid
            self._ids[gid] = key
        return gid

    def globalize(self, frame: Frame, value: Any) -> Any:
        """Rewrite every integer ``id`` in a script's result into a tab-wide id.

        Scripts number elements with their own world's ``__zd.idOf``; only the
        tab-wide number may leave this module.
        """
        if isinstance(value, list):
            return [self.globalize(frame, v) for v in value]
        if isinstance(value, dict):
            return {
                k: (
                    self.global_id(frame, v)
                    if k == "id" and isinstance(v, int) and not isinstance(v, bool)
                    else self.globalize(frame, v)
                )
                for k, v in value.items()
            }
        return value

    # ------------------------------------------------------------------
    # the interaction-tree walk

    async def walk(
        self, *, closed_shadow: bool = False
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Run the interaction-tree walker in every rendered frame.

        Returns the elements (frame elements carry ``f``) and the frames walked
        besides the main one, as ``{"f", "url"}``. ``closed_shadow`` also walks
        closed shadow roots, found through ``DOM.getDocument``: that serialises
        the whole DOM and roughly doubles the walk (measured 2026-11-15: +85 ms
        on github.com, +316 ms on a Wikipedia article, +1.8 s on 25k nodes), so
        it is asked for, not default.
        """
        frames = await self.frames()
        closed = await self._closed_roots(frames) if closed_shadow else {}
        elements: list[dict[str, Any]] = []
        listed: list[dict[str, Any]] = []
        hidden: set[str] = set()
        for frame in frames:
            if frame.parent is not None and (
                frame.parent.frame_id in hidden or not await self.frame_shown(frame)
            ):
                hidden.add(frame.frame_id)
                continue
            try:
                raw = await self._walk_frame(frame, closed.get(frame.frame_id, []))
            except (ProtocolException, ZendriverMCPError):
                if frame.is_main:
                    raise
                continue  # the frame navigated or went away mid-walk
            origin = _origin_of(frame.url)
            for e in raw:
                e["id"] = self.global_id(frame, e["id"])
                if isinstance(e.get("rid"), int):
                    e["rid"] = self.global_id(frame, e["rid"])
                if frame.number:
                    e["f"] = frame.number
                    # The walker shortens same-origin links to a path; relative
                    # to a frame that is a different site, that path would be
                    # read against the wrong origin.
                    h = e.get("h")
                    if isinstance(h, str) and h.startswith("/") and origin:
                        e["h"] = origin + h
            elements.extend(raw)
            if frame.number:
                listed.append({"f": frame.number, "url": frame.url})
        return elements, listed

    async def scope(
        self, root: Handle, elements: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], set[int]]:
        """Keep the walked elements that sit inside ``root``.

        Inside means in ``root``'s composed subtree (shadow roots included), or
        in an iframe whose <iframe> element is. Returns the kept elements and
        the frame numbers whose documents are inside. The test runs in our
        isolated world on the walker's own element store, so the page sees
        nothing.
        """
        frames = await self.frames()
        by_id = {f.frame_id: f for f in frames}
        inside_frames: set[str] = set()
        owner_inside: dict[str, bool] = {}
        for f in frames:
            child = f
            while child.parent is not None and child.parent.frame_id != root.frame.frame_id:
                child = child.parent
            if child.parent is None:
                continue
            if child.frame_id not in owner_inside:
                owner_inside[child.frame_id] = await self._owner_within(root, child)
            if owner_inside[child.frame_id]:
                inside_frames.add(f.frame_id)

        local: dict[int, int] = {}
        for e in elements:
            key = self._ids.get(e["id"])
            if key is not None and key[0] == root.frame.frame_id:
                local[key[2]] = e["id"]
        kept_local = await self.call(root, _WITHIN_FN, sorted(local)) if local else []
        kept = {local[i] for i in kept_local if i in local}

        out = []
        for e in elements:
            key = self._ids.get(e["id"])
            if e["id"] in kept or (key is not None and key[0] in inside_frames):
                out.append(e)
        numbers = {by_id[i].number for i in inside_frames if i in by_id}
        return out, numbers

    async def map_ids(self, gids: list[int], function: str, *args: Any) -> dict[int, Any]:
        """Run ``function(localIds, ...args)`` once per frame; map its results back.

        The function gets the frame-local ids of the given tab-wide ids, in the
        frame's isolated world, and returns one value per id in order. Ids whose
        document is gone are left out of the result.
        """
        groups: dict[tuple[str, str], list[int]] = {}
        for gid in gids:
            key = self._ids.get(gid)
            if key is not None:
                groups.setdefault((key[0], key[1]), []).append(gid)
        frames = {(f.frame_id, f.loader_id): f for f in await self.frames()}
        out: dict[int, Any] = {}
        for fkey, members in groups.items():
            frame = frames.get(fkey)
            if frame is None:
                continue
            locals_ = [self._ids[g][2] for g in members]
            try:
                ctx = await self._context(frame)
                remote, exc = await frame.session.send(
                    cdp.runtime.call_function_on(
                        function_declaration=function,
                        execution_context_id=ctx,
                        arguments=[cdp.runtime.CallArgument(value=v) for v in (locals_, *args)],
                        return_by_value=True,
                    )
                )
            except ProtocolException:
                continue
            if exc is not None or not isinstance(remote.value, list):
                continue
            out.update(zip(members, remote.value, strict=False))
        return out

    async def _owner_within(self, root: Handle, child: Frame) -> bool:
        """Whether ``child``'s <iframe> element sits inside ``root``."""
        try:
            _session, backend = await self._owner(child)
            ctx = await self._context(root.frame)
            owner = await root.frame.session.send(
                cdp.dom.resolve_node(backend_node_id=backend, execution_context_id=ctx)
            )
            if owner.object_id is None:
                return False
            remote, exc = await root.frame.session.send(
                cdp.runtime.call_function_on(
                    function_declaration=_CONTAINS_FN,
                    object_id=root.object_id,
                    arguments=[
                        cdp.runtime.CallArgument(object_id=root.object_id),
                        cdp.runtime.CallArgument(object_id=owner.object_id),
                    ],
                    return_by_value=True,
                )
            )
        except ProtocolException:
            return False
        return exc is None and remote.value is True

    async def _walk_frame(
        self, frame: Frame, closed: list[cdp.dom.BackendNodeId]
    ) -> list[dict[str, Any]]:
        for attempt in (0, 1):
            ctx = await self._context(frame)
            try:
                roots = await self._resolve_all(frame, ctx, closed)
                remote, exc = await frame.session.send(
                    cdp.runtime.call_function_on(
                        function_declaration=WALKER_JS,
                        arguments=[cdp.runtime.CallArgument(object_id=r) for r in roots],
                        execution_context_id=ctx,
                        return_by_value=True,
                    )
                )
            except ProtocolException as e:
                if attempt == 0 and _context_gone(e):
                    self._forget_world(frame)
                    continue
                raise
            finally:
                await self._release(frame.session, _WALK_GROUP)
            if exc is not None:
                raise ZendriverMCPError(
                    f"JavaScript error: {_exception_text(exc)}", code=Code.JS_ERROR
                )
            return [e for e in (remote.value or []) if isinstance(e, dict)]
        raise AssertionError("unreachable")

    async def _closed_roots(self, frames: list[Frame]) -> dict[str, list[cdp.dom.BackendNodeId]]:
        """Closed shadow roots per frame id, from each session's CDP DOM tree.

        Script cannot reach a closed root (``el.shadowRoot`` is null), but CDP
        lists it, and resolving it into our world needs nothing from the page.
        One ``DOM.getDocument`` per session covers its same-process frames,
        whose documents appear as ``content_document``.
        """
        out: dict[str, list[cdp.dom.BackendNodeId]] = {}
        roots = [f for f in frames if f.parent is None or f.process_root]
        for root in roots:
            try:
                doc = await root.session.send(cdp.dom.get_document(depth=-1, pierce=True))
            except ProtocolException:
                continue
            stack: list[tuple[cdp.dom.Node, str]] = [(doc, root.frame_id)]
            while stack:
                node, fid = stack.pop()
                for sr in node.shadow_roots or []:
                    if sr.shadow_root_type == cdp.dom.ShadowRootType.CLOSED:
                        out.setdefault(fid, []).append(sr.backend_node_id)
                    stack.append((sr, fid))
                for child in node.children or []:
                    stack.append((child, fid))
                if node.content_document is not None:
                    child_fid = node.content_document.frame_id or node.frame_id
                    if child_fid is not None:
                        stack.append((node.content_document, str(child_fid)))
        return out

    async def _resolve_all(
        self,
        frame: Frame,
        ctx: cdp.runtime.ExecutionContextId,
        nodes: list[cdp.dom.BackendNodeId],
    ) -> list[cdp.runtime.RemoteObjectId]:
        out: list[cdp.runtime.RemoteObjectId] = []
        for backend_id in nodes:
            try:
                remote = await frame.session.send(
                    cdp.dom.resolve_node(
                        backend_node_id=backend_id,
                        execution_context_id=ctx,
                        object_group=_WALK_GROUP,
                    )
                )
            except ProtocolException:
                continue  # detached between the two calls
            if remote.object_id is not None:
                out.append(remote.object_id)
        return out

    async def _release(self, session: zd.Tab, group: str) -> None:
        try:
            await session.send(cdp.runtime.release_object_group(object_group=group))
        except ProtocolException:
            pass  # the context is already gone, and its objects with it

    # ------------------------------------------------------------------
    # resolving elements

    async def resolve(self, selector: str) -> Handle:
        """Resolve a CSS selector or a tab-wide id to a handle.

        A CSS selector is tried in the main frame first; if nothing matches
        there, in every other frame, and a match in exactly one frame wins.
        A numeric id whose document is gone (the page or frame navigated) or
        whose element has left the DOM is a :class:`StaleIdError`, not "not
        found": the recovery differs (re-walk, not re-think the selector).
        """
        sel = selector.strip()
        if is_numeric_id(sel):
            return await self._resolve_id(int(sel), sel)

        main = await self.main_frame()
        found = await self._query(main, sel)
        if found is not None:
            return found
        hits: list[Handle] = []
        for frame in (await self.frames())[1:]:
            try:
                h = await self._query(frame, sel)
            except ProtocolException:
                continue
            if h is not None:
                hits.append(h)
        if len(hits) == 1:
            return hits[0]
        if hits:
            where = ", ".join(f"frame {h.frame.number} ({h.frame.url})" for h in hits)
            raise AmbiguousError(
                f"{sel} is not in the main page, and matches in {len(hits)} frames: {where}."
            )
        raise ElementNotFoundError(sel)

    async def _query(self, frame: Frame, sel: str) -> Handle | None:
        expr = f"document.querySelector({json.dumps(sel)})"
        for attempt in (0, 1):
            ctx = await self._context(frame)
            try:
                remote, exc = await frame.session.send(
                    cdp.runtime.evaluate(expression=expr, context_id=ctx, return_by_value=False)
                )
            except ProtocolException as e:
                if attempt == 0 and _context_gone(e):
                    self._forget_world(frame)
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
                return None
            return await self._handle(frame, remote.object_id, sel)
        return None

    async def focused(self) -> Handle | None:
        """The element with keyboard focus, in whichever frame has it, or None.

        Follows focus into shadow roots; the body or an <iframe> holding focus
        is not an element a key acts on, so frames are asked in turn.
        """
        expr = (
            "(() => { if (!document.hasFocus()) return null;"
            " let a = document.activeElement;"
            " while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;"
            " if (!a || a === document.body || a.tagName === 'IFRAME' || a.tagName === 'FRAME')"
            " return null; return a; })()"
        )
        for frame in await self.frames():
            try:
                ctx = await self._context(frame)
                remote, exc = await frame.session.send(
                    cdp.runtime.evaluate(expression=expr, context_id=ctx, return_by_value=False)
                )
            except ProtocolException:
                continue
            if exc is None and remote.object_id is not None and remote.subtype != "null":
                return await self._handle(frame, remote.object_id, "focused element")
        return None

    async def at_point(self, x: float, y: float) -> Handle | None:
        """The element the browser hit-tests at tab point (x, y), inside iframes too."""
        frames = await self.frames()
        frame = frames[0]
        lx, ly = x, y
        for _ in range(8):  # iframe nesting depth
            try:
                backend, frame_id, _node = await frame.session.send(
                    cdp.dom.get_node_for_location(
                        x=int(lx), y=int(ly), include_user_agent_shadow_dom=False
                    )
                )
            except ProtocolException:
                return None
            owner_of = None
            for child in frames:
                if child.parent is None or child.parent.session is not frame.session:
                    continue
                try:
                    _s, owner = await self._owner(child)
                except ProtocolException:
                    continue
                if owner == backend:
                    owner_of = child
                    break
            if owner_of is None or not owner_of.process_root:
                target = next((f for f in frames if f.frame_id == str(frame_id)), frame)
                ctx = await self._context(target)
                try:
                    remote = await target.session.send(
                        cdp.dom.resolve_node(backend_node_id=backend, execution_context_id=ctx)
                    )
                except ProtocolException:
                    return None
                if remote.object_id is None:
                    return None
                return Handle(remote.object_id, backend, f"element at ({x:.0f}, {y:.0f})", target)
            # An out-of-process iframe: ask its own session, in its coordinates.
            ox, oy = await self._session_offset(owner_of)
            frame, lx, ly = owner_of, x - ox, y - oy
        return None

    async def root(self, frame: Frame) -> Handle:
        """The document element of ``frame``, for scripts that search a whole document."""
        found = await self._query(frame, ":root")
        if found is None:
            raise ElementNotFoundError(f":root of frame {frame.number}")
        return found

    async def _resolve_id(self, gid: int, label: str) -> Handle:
        key = self._ids.get(gid)
        if key is None:
            raise StaleIdError(label)
        frame_id, loader_id, local = key
        main = await self.main_frame()
        candidates = [main] if main.frame_id == frame_id else await self.frames()
        frame = next((f for f in candidates if f.frame_id == frame_id), None)
        if frame is None or frame.loader_id != loader_id:
            raise StaleIdError(label)
        expr = f"globalThis.__zd ? globalThis.__zd.get({int(local)}) : null"
        ctx = await self._context(frame)
        try:
            remote, exc = await frame.session.send(
                cdp.runtime.evaluate(expression=expr, context_id=ctx, return_by_value=False)
            )
        except ProtocolException as e:
            if _context_gone(e):
                self._forget_world(frame)
                raise StaleIdError(label) from e
            raise
        if exc is not None or remote.object_id is None or remote.subtype == "null":
            raise StaleIdError(label)
        return await self._handle(frame, remote.object_id, label)

    async def _handle(
        self, frame: Frame, object_id: cdp.runtime.RemoteObjectId, label: str
    ) -> Handle:
        node = await frame.session.send(cdp.dom.describe_node(object_id=object_id))
        return Handle(object_id, node.backend_node_id, label, frame)

    async def resolve_local(self, frame: Frame, local: int, label: str | None = None) -> Handle:
        """The element a script in ``frame``'s world numbered ``local``."""
        gid = self.global_id(frame, local)
        return await self._resolve_id(gid, label or str(gid))

    async def handle_for_expression(self, expression: str, label: str) -> Handle:
        """Evaluate an isolated-world expression, in the main frame, that yields an element."""
        for attempt in (0, 1):
            frame = await self.main_frame()
            ctx = await self._context(frame)
            try:
                remote, exc = await frame.session.send(
                    cdp.runtime.evaluate(
                        expression=expression, context_id=ctx, return_by_value=False
                    )
                )
            except ProtocolException as e:
                if attempt == 0 and _context_gone(e):
                    self._forget_world(frame)
                    continue
                raise
            if exc is not None:
                raise ZendriverMCPError(
                    f"JavaScript error finding {label}: {_exception_text(exc)}", code=Code.JS_ERROR
                )
            if remote.object_id is None or remote.subtype == "null":
                raise ElementNotFoundError(label)
            return await self._handle(frame, remote.object_id, label)
        raise ElementNotFoundError(label)

    async def handle_for_node(self, backend_node_id: cdp.dom.BackendNodeId, label: str) -> Handle:
        """A handle in our isolated world for a node of the tab's own session.

        The node may sit in the main frame or in a same-process iframe; it is
        resolved into whichever frame's world accepts it.
        """
        frames = [f for f in await self.frames() if f.session is self.tab]
        for frame in frames:
            ctx = await self._context(frame)
            try:
                remote = await self.tab.send(
                    cdp.dom.resolve_node(backend_node_id=backend_node_id, execution_context_id=ctx)
                )
            except ProtocolException:
                continue
            if remote.object_id is not None:
                return Handle(remote.object_id, backend_node_id, label, frame)
        raise ElementNotFoundError(label)

    async def id_for(self, handle: Handle) -> int:
        """Number an element the way the walker would, so callers can reuse it."""
        local = await self.call(handle, "function(el) { return globalThis.__zd.idOf(el); }")
        return self.global_id(handle.frame, int(local))

    async def call(self, selector: str | Handle, function: str, *args: Any) -> Any:
        """Call ``function(el, ...args)`` in the element's isolated world; JSON in, JSON out.

        Element ids in the result are rewritten to tab-wide ids.
        """
        handle = selector if isinstance(selector, Handle) else await self.resolve(selector)
        call_args = [cdp.runtime.CallArgument(object_id=handle.object_id)]
        call_args += [cdp.runtime.CallArgument(value=a) for a in args]
        try:
            remote, exc = await handle.frame.session.send(
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
                self._forget_world(handle.frame)
                if is_numeric_id(handle.label):
                    raise StaleIdError(handle.label) from e
                raise
            raise
        if exc is not None:
            raise ZendriverMCPError(
                f"JavaScript error on {handle.label}: {_exception_text(exc)}", code=Code.JS_ERROR
            )
        return self.globalize(handle.frame, remote.value)

    # ------------------------------------------------------------------
    # geometry and trusted input

    async def center(self, handle: Handle, *, scroll: bool = True) -> tuple[float, float]:
        """Tab viewport coordinates of the element's first box, scrolled into view.

        Uses CDP's own geometry (``DOM.getContentQuads``) rather than running
        getBoundingClientRect, so a page cannot see the measurement. For an
        element in an iframe, the <iframe>s holding it are scrolled into view
        first, outermost first.
        """
        frame = handle.frame
        if scroll:
            chain: list[Frame] = []
            f: Frame | None = frame
            while f is not None and f.parent is not None:
                chain.append(f)
                f = f.parent
            for fr in reversed(chain):
                try:
                    session, owner = await self._owner(fr)
                    await session.send(cdp.dom.scroll_into_view_if_needed(backend_node_id=owner))
                except ProtocolException:
                    pass
            try:
                await frame.session.send(
                    cdp.dom.scroll_into_view_if_needed(backend_node_id=handle.backend_node_id)
                )
            except ProtocolException:
                pass  # not scrollable (fixed, or detached); the quads say the rest
            if frame.parent is not None:
                await self._presented(frame)
        try:
            quads = await frame.session.send(
                cdp.dom.get_content_quads(backend_node_id=handle.backend_node_id)
            )
        except ProtocolException as e:
            raise ElementHiddenError(handle.label) from e
        dx, dy = await self._session_offset(frame)
        for q in quads or []:
            xs, ys = q[0::2], q[1::2]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            if w > 0 and h > 0:
                return sum(xs) / 4 + dx, sum(ys) / 4 + dy
        raise ElementHiddenError(handle.label)

    async def _presented(self, frame: Frame) -> None:
        """Wait until the tab and ``frame`` have drawn a frame since the last scroll.

        Chrome routes input to an out-of-process iframe by hit-test data that
        the compositor updates only when it draws. Measured on CI (2026-11-15):
        a click dispatched straight after scrolling such an iframe into view
        landed on the <iframe> element in the parent page, at the correct
        point, and never reached the frame. Two animation frames in each
        document (from our isolated world) means a new frame has been drawn;
        the timeout covers a tab that is not drawing at all.
        """
        main = await self.main_frame()
        for f in (main, frame):
            try:
                await self.evaluate_in(f, _TWO_FRAMES_JS)
            except (ProtocolException, ZendriverMCPError):
                pass  # a frame navigating away: the click will say what happened

    async def obstruction(self, handle: Handle, x: float, y: float) -> str | None:
        """What a click at tab point (x, y) would hit instead of the element, or None.

        Checked in the element's own frame, then at each enclosing level, where
        the hit must be the <iframe> that holds it: a cookie banner over an
        embedded form covers the iframe, not the field.
        """
        frame = handle.frame
        ox, oy = await self.viewport_origin(frame)
        blocker = await self.call(handle, _OBSTRUCTION_FN, x - ox, y - oy)
        if blocker:
            return str(blocker)
        while frame.parent is not None:
            parent = frame.parent
            session, owner = await self._owner(frame)
            ctx = await self._context(parent)
            remote = await session.send(
                cdp.dom.resolve_node(backend_node_id=owner, execution_context_id=ctx)
            )
            if remote.object_id is None:
                return None
            owner_handle = Handle(
                remote.object_id, owner, f"<iframe> of frame {frame.number}", parent
            )
            px, py = await self.viewport_origin(parent)
            blocker = await self.call(owner_handle, _OBSTRUCTION_FN, x - px, y - py)
            if blocker:
                return str(blocker)
            frame = parent
        return None

    async def click(
        self,
        selector: str | Handle,
        *,
        button: str = "left",
        count: int = 1,
        force: bool = False,
        modifiers: int = 0,
    ) -> Handle:
        """A trusted click: move, press, release at the element's centre.

        ``modifiers`` is CDP's bit field (Alt 1, Control 2, Meta 4, Shift 8),
        held for the press and release: a Control-click adds an option to a
        multi-select list, as it does for a person.

        ``force`` skips the obstruction check and clicks through whatever is on
        top; the page then receives the event wherever it lands, exactly as a
        real user's click would. Input always goes to the top-level tab, which
        routes it into whichever frame is under the point.
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
                    type_="mousePressed",
                    x=x,
                    y=y,
                    button=btn,
                    click_count=n,
                    buttons=held,
                    modifiers=modifiers or None,
                )
            )
            await self.tab.send(
                cdp.input_.dispatch_mouse_event(
                    type_="mouseReleased",
                    x=x,
                    y=y,
                    button=btn,
                    click_count=n,
                    buttons=0,
                    modifiers=modifiers or None,
                )
            )
        return handle

    async def viewport(self) -> tuple[float, float, float, float, float]:
        """The tab's scroll position, viewport size and content height, in CSS pixels.

        From ``Page.getLayoutMetrics``: the browser's numbers, no page script.
        Returns (scroll_x, scroll_y, width, height, content_height).
        """
        metrics = await self.tab.send(cdp.page.get_layout_metrics())
        lv, content = metrics[3], metrics[5]
        return (
            float(lv.page_x),
            float(lv.page_y),
            float(lv.client_width),
            float(lv.client_height),
            float(content.height),
        )

    async def scroll_by(self, dy: float) -> float:
        """Scroll the tab vertically by ``dy`` CSS pixels with a trusted wheel gesture.

        The gesture starts at the viewport's centre, as a person's would, and
        this returns once the scroll position has settled, with the new
        position. Negative ``dy`` scrolls up.
        """
        _x, y0, w, h, _content = await self.viewport()
        if dy:
            await self.tab.send(
                cdp.input_.synthesize_scroll_gesture(
                    x=w / 2,
                    y=h / 2,
                    y_distance=-dy,
                    x_overscroll=0,
                    y_overscroll=0,
                    prevent_fling=True,
                    speed=_SCROLL_SPEED,
                    repeat_delay_ms=0,
                )
            )
        last = y0
        for _ in range(_SETTLE_POLLS):
            await asyncio.sleep(_SETTLE_SECONDS)
            y = (await self.viewport())[1]
            if y == last:
                return y
            last = y
        return last

    async def focus(self, selector: str | Handle) -> Handle:
        handle = selector if isinstance(selector, Handle) else await self.resolve(selector)
        try:
            await handle.frame.session.send(cdp.dom.focus(backend_node_id=handle.backend_node_id))
        except ProtocolException as e:
            raise ZendriverMCPError(
                f"Cannot focus {handle.label}: {e}",
                hint="Only focusable elements (inputs, buttons, links, [tabindex]) take focus.",
            ) from e
        return handle

    async def set_files(self, selector: str | Handle, files: list[str]) -> Handle:
        handle = selector if isinstance(selector, Handle) else await self.resolve(selector)
        await handle.frame.session.send(
            cdp.dom.set_file_input_files(files=files, backend_node_id=handle.backend_node_id)
        )
        return handle

    async def element(self, selector: str | Handle) -> zd.Element:
        """A zendriver Element for code that still needs one.

        Avoid its ``click()``/``mouse_click()``/``flash()``: see the module docstring.
        """
        handle = selector if isinstance(selector, Handle) else await self.resolve(selector)
        node = await handle.frame.session.send(
            cdp.dom.describe_node(backend_node_id=handle.backend_node_id)
        )
        return zd.Element(node, handle.frame.session)


def _origin_of(url: str) -> str | None:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return None


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
