# page content tools - get html, get text, scroll
import asyncio
import json
from typing import Annotated, Any, Literal

from pydantic import Field
from zendriver.core.connection import ProtocolException

from zendriver_mcp.errors import ZendriverMCPError
from zendriver_mcp.registry import Effect
from zendriver_mcp.response import Result
from zendriver_mcp.tools._markdown_js import MARKDOWN_FN
from zendriver_mcp.tools.base import JS_TIMEOUT_SECONDS, ToolBase

# Null-safe: `document.body.innerText` throws while the body does not exist yet.
_BODY_TEXT_JS = "document.body ? document.body.innerText : ''"
_ELEMENT_TEXT_FN = "function(el) { return el.innerText || el.textContent || ''; }"


class Page(Result):
    """One slice of a long text, with what is needed to ask for the next."""

    offset: int = Field(description="First character of this slice.")
    end: int = Field(description="One past the last character of this slice.")
    total: int = Field(description="Length of the whole text.")
    next_offset: int | None = Field(
        default=None, description="Offset to request next; absent on the last slice."
    )
    text: str = Field(description="The slice itself.")

    def header(self) -> str:
        h = f"[chars {self.offset}-{self.end} of {self.total}]"
        if self.next_offset is not None:
            h += f" (next: offset={self.next_offset})"
        return h

    def render(self) -> str:
        return f"{self.header()}\n{self.text}"


class TextPage(Page):
    diagnosis: str | None = Field(
        default=None,
        description="Only on an empty result: why the page has no visible text, and what to do.",
    )

    def render(self) -> str:
        out = super().render()
        return f"{out}\n{self.diagnosis}" if self.diagnosis else out


class InteractionTree(Result):
    elements: list[dict[str, Any]] = Field(
        description=(
            "Interactive elements: id (pass to click/type_text), t type (btn, link, in, "
            "chk, rad, sel, tab, mnu, el), l label, r region (hdr, nav, main, side, ftr, "
            "dlg), and when relevant f frame number (absent in the main page), h link "
            "target, it input type, v value, ck checked, req required, dis disabled, "
            "off offscreen, hid hidden (a file input behind a styled button)."
        )
    )
    total: int = Field(description="How many interactive elements the page has.")
    truncated: bool = Field(description="Whether `limit` cut the list short.")
    removed: list[int] | None = Field(
        default=None,
        description="With only_changed: ids of elements gone since the previous tree.",
    )
    frames: list[dict[str, Any]] | None = Field(
        default=None,
        description="The iframes walked besides the main page, as {f, url}; absent when there are none.",
    )

    def render(self) -> str:
        payload = json.dumps(self.elements, separators=(",", ":"), ensure_ascii=False)
        lines = []
        if self.frames:
            lines.append(
                "[frames: " + ", ".join(f"f{fr['f']}={fr['url']}" for fr in self.frames) + "]"
            )
        if self.truncated:
            lines.append(
                f"[showing {len(self.elements)} of {self.total} elements; raise limit for more]"
            )
        lines.append(payload)
        return "\n".join(lines)


# Fields that move with scrolling rather than with the page: an element that
# scrolled into view has not changed.
_UNSTABLE = ("id", "off", "r")


def signatures(elements: list[dict[str, Any]]) -> dict[int, str]:
    """Each element's id mapped to what it shows, for comparing two trees."""
    out: dict[int, str] = {}
    for e in elements:
        eid = e.get("id")
        if isinstance(eid, int):
            stable = {k: v for k, v in e.items() if k not in _UNSTABLE}
            out[eid] = json.dumps(stable, sort_keys=True, ensure_ascii=False)
    return out


def delta(before: dict[int, str], after: dict[int, str]) -> tuple[set[int], list[int]]:
    """Ids new or changed in ``after``, and ids gone from it, oldest first.

    Ids name one element for its whole life, so this is exact: a re-rendered
    element is "removed" plus "new", an edited one is "changed".
    """
    changed = {i for i, sig in after.items() if before.get(i) != sig}
    removed = sorted(i for i in before if i not in after)
    return changed, removed


class ScrollResult(Result):
    target: str | None = Field(default=None, description="Selector scrolled to, if any.")
    scroll_y: float | None = Field(default=None, description="Vertical scroll position afterwards.")


class ContentTools(ToolBase):
    """tools for page content and scrolling"""

    GROUP = "content"

    # The last tree per tab, for only_changed: tab -> (main document, signatures).
    _baselines: dict[int, tuple[str | None, dict[int, str]]] = {}

    def _reset_state(self) -> None:
        self._baselines.clear()

    def _register_tools(self) -> None:
        """register content tools"""
        self._register(self.get_content, Effect.READ, "Page HTML")
        self._register(self.get_text_content, Effect.READ, "Page text")
        self._register(self.get_interaction_tree, Effect.READ, "Interactive elements")
        self._register(self.scroll, Effect.WRITE, "Scroll page")
        self._register(self.scroll_to_element, Effect.WRITE, "Scroll to element", idempotent=True)

    async def get_content(
        self,
        max_chars: Annotated[
            int,
            Field(
                description="Maximum characters of HTML to return in this call. Pass a larger value or page with offset to read more. Example: 10000"
            ),
        ] = 10000,
        offset: Annotated[
            int,
            Field(
                description="Character offset to start from, for paging through a large page. Example: 0"
            ),
        ] = 0,
    ) -> Page:
        """Get the page's full rendered HTML, including markup and attributes.

        Use when you need selectors, attributes, or hidden markup. Prefer
        get_text_content when you only want readable text, and
        get_interaction_tree when you want something to click — both are far
        cheaper. Read through CDP's DOM domain, so no page JavaScript runs.
        Returns the requested slice with offset, end, total and next_offset
        (absent on the last slice); the text form starts with a
        '[chars X-Y of TOTAL]' header.
        """
        content = await self.session.page.get_content()
        page = self._paginate(content, max_chars, offset)
        return Page(summary=page.header(), **page.model_dump(exclude={"summary"}))

    async def get_text_content(
        self,
        max_chars: Annotated[
            int,
            Field(
                description="Maximum characters of text to return in this call. Pass a larger value or page with offset to read more. Example: 10000"
            ),
        ] = 10000,
        offset: Annotated[
            int,
            Field(
                description="Character offset to start from, for paging through long text. Example: 0"
            ),
        ] = 0,
        format: Annotated[
            Literal["text", "markdown"],
            Field(
                description="'markdown' keeps headings, lists, links, tables and code, from the page's main content. Example: 'markdown'"
            ),
        ] = "text",
        selector: Annotated[
            str | None,
            Field(description="Read only this element (selector or tree id). Example: 'article'"),
        ] = None,
    ) -> TextPage:
        """Get the page's visible text, without any HTML markup.

        Use for reading or extracting page copy. Text hidden by CSS is excluded,
        and no selectors are returned, so use get_interaction_tree if you intend
        to interact. Same pagination contract as get_content. An empty result
        carries a diagnosis saying whether the page is still loading, has no
        body, keeps its text in shadow roots, or really is blank. Text inside
        iframes follows, each under a "--- frame N (url) ---" line.
        Returns one page of text plus the offset to continue from.
        """
        # Null-safe on purpose. `document.body.innerText` THROWS when the body
        # does not exist yet, and a thrown CDP exception reaches the caller as a
        # stack trace plus a parameter dump — which says nothing about the page
        # and sends them looking for a bad argument. Observed for real:
        # `TypeError: Cannot read properties of null (reading 'innerText')` on a
        # page that had simply not finished loading. Returning empty lets the
        # diagnosis below say which of the possible causes it actually is.
        if selector is not None:
            handle = await self.resolve(selector)
            fn = MARKDOWN_FN if format == "markdown" else _ELEMENT_TEXT_FN
            text = str(await self.element_call(handle, fn) or "")
            page = self._paginate(text, max_chars, offset)
            return TextPage(summary=page.header(), **page.model_dump(exclude={"summary"}))
        if format == "markdown":
            text = str(await self.element_call(await self.resolve(":root"), MARKDOWN_FN) or "")
        else:
            text = await self.run_js(_BODY_TEXT_JS)
        text = str(text or "") + await self._frame_text(format)
        page = self._paginate(text, max_chars, offset)
        fields = page.model_dump(exclude={"summary"})
        if text:
            return TextPage(summary=page.header(), **fields)
        # An empty result is the one answer this tool cannot leave ambiguous.
        # "[chars 0-0 of 0]" reads as "this page has no text", and a caller that
        # believes it goes looking for a consent wall or a different source. The
        # usual cause is that nothing has rendered yet, which is a completely
        # different fix, so say which one it is.
        diagnosis = await self._empty_text_diagnosis()
        return TextPage(summary=f"{page.header()} {diagnosis}", diagnosis=diagnosis, **fields)

    async def _frame_text(self, format: str = "text") -> str:
        """The visible text of each rendered iframe, under a header naming it.

        Frames are numbered as get_interaction_tree numbers them. A frame that
        fails to answer (it navigated mid-read, or hangs) is skipped rather than
        failing the whole read.
        """
        parts: list[str] = []
        try:
            frames = await self.dom.frames()
        except ProtocolException:
            return ""
        for frame in frames[1:]:
            try:
                if not await self.dom.frame_shown(frame):
                    continue
                if format == "markdown":
                    read = self.dom.call(await self.dom.root(frame), MARKDOWN_FN)
                else:
                    read = self.dom.evaluate_in(frame, _BODY_TEXT_JS)
                text = await asyncio.wait_for(read, timeout=JS_TIMEOUT_SECONDS)
            except (ZendriverMCPError, ProtocolException, TimeoutError, asyncio.TimeoutError):
                continue
            if text and str(text).strip():
                parts.append(f"\n\n--- frame {frame.number} ({frame.url}) ---\n{text}")
        return "".join(parts)

    async def _empty_text_diagnosis(self) -> str:
        """Explain an empty innerText: not-yet-rendered, no body, or genuinely blank."""
        try:
            info = await self.run_js("""
                (function() {
                    return {
                        readyState: document.readyState,
                        url: location.href,
                        hasBody: !!document.body,
                        htmlChars: (document.documentElement
                            ? document.documentElement.outerHTML.length : 0),
                    };
                })()
            """)
        except Exception as exc:  # the diagnosis must never replace the result with an error
            return f"(no visible text; could not diagnose: {exc})"

        if not isinstance(info, dict):
            return "(no visible text; page did not answer a diagnostic query)"

        ready = info.get("readyState", "unknown")
        html_chars = info.get("htmlChars", 0)
        detail = f"readyState={ready}, html={html_chars} chars, url={info.get('url', 'unknown')}"
        if ready != "complete":
            return (
                f"(no visible text yet — the page is still loading: {detail}. "
                "Call wait_for_network or navigate with a larger settle, then read again.)"
            )
        if not info.get("hasBody"):
            return f"(no visible text — the document has no body element: {detail})"
        if html_chars > 2000:
            return (
                f"(no visible text, but the page has markup: {detail}. "
                "The text may be in an iframe, a shadow root, or hidden by CSS — "
                "try get_content or get_interaction_tree.)"
            )
        return f"(no visible text — the page really is blank: {detail})"

    @staticmethod
    def _paginate(text: str, max_chars: int, offset: int) -> Page:
        """Slice ``text`` and say where it sits, so agents can page deliberately."""
        max_chars = max(1, max_chars)
        total = len(text)
        offset = min(max(0, offset), total)
        chunk = text[offset : offset + max_chars]
        end = offset + len(chunk)
        page = Page(
            summary="",
            offset=offset,
            end=end,
            total=total,
            next_offset=end if end < total else None,
            text=chunk,
        )
        page.summary = page.header()
        return page

    async def get_interaction_tree(
        self,
        limit: Annotated[
            int,
            Field(
                description="Maximum number of interactive elements to return. Raise it on dense pages; a note reports when the cap was hit. Example: 150"
            ),
        ] = 150,
        links: Annotated[
            bool,
            Field(
                description="Include each link's target as 'h' (a path when same-origin). Off by default because it roughly doubles the output; turn it on when you need to follow or identify a link. Example: true"
            ),
        ] = False,
        only_changed: Annotated[
            bool,
            Field(
                description="Only what changed since this tab's last tree: new or changed elements, plus removed ids. Example: true"
            ),
        ] = False,
        closed_shadow: Annotated[
            bool,
            Field(
                description="Also walk closed shadow roots; roughly doubles the walk's time. Example: true"
            ),
        ] = False,
    ) -> InteractionTree:
        """List the page's interactive elements, each with a short numeric id.

        The cheapest way to see what can be clicked or typed into: it walks the
        DOM, open shadow roots and iframes for buttons, links, and inputs; an
        element inside an iframe carries its frame number as f. The numeric ids
        it assigns are accepted directly by click, type_text, and the other
        element tools in place of a CSS selector. An id stays valid while its
        element exists, survives later walks, and expires on navigation; an
        expired one fails with stale_id. The walk runs in an isolated world and
        leaves no trace in the page. Returns the elements (capped at limit),
        the total, and whether the cap cut the list short.

        only_changed=true after an action shows just its effect (exact, since
        ids are stable); a new page gets the full tree.

        links=true adds each link's target as `h` (a path when same-origin),
        so a link can be followed with navigate; off by default because it
        roughly doubles the output. javascript:, bare "#" and same-page links
        get none.
        """
        tree, frames = await self._walk(closed_shadow=closed_shadow)
        # The walker always collects link targets; whether the CALLER pays for
        # them is decided here, because the cost is in the returned JSON rather
        # than in the walk. Non-dict entries cannot come from our walker, but it
        # is JavaScript and validates nothing, so they are dropped, not trusted.
        elements = [dict(e) for e in tree if isinstance(e, dict)]
        if not links:
            for element in elements:
                element.pop("h", None)

        # Every walk becomes the baseline for the next only_changed call.
        page_key = await self._page_key()
        current = signatures(elements)
        previous = self._baselines.get(self._baseline_slot())
        self._baselines[self._baseline_slot()] = (page_key, current)
        removed: list[int] | None = None
        note = ""
        if only_changed:
            if previous is not None and previous[0] == page_key:
                changed, removed = delta(previous[1], current)
                elements = [e for e in elements if e.get("id") in changed]
                note = (
                    f"; {len(elements)} new or changed, {len(removed)} removed since the last tree"
                )
            else:
                note = "; full tree: no earlier tree of this page to compare with"

        limit = max(1, limit)
        total = len(elements)
        shown = elements[:limit]
        return InteractionTree(
            summary=f"{total} interactive element(s)"
            + (f", showing {limit}" if total > limit else "")
            + note,
            elements=shown,
            total=total,
            truncated=total > limit,
            frames=frames or None,
            removed=removed,
        )

    def _baseline_slot(self) -> int:
        try:
            return id(self.session.page)
        except Exception:
            return 0

    async def _page_key(self) -> str | None:
        """The main document's identity: a new one means a new page, not a change."""
        try:
            return (await self.dom.main_frame()).loader_id
        except Exception:
            return None

    async def _walk(self, *, closed_shadow: bool = False) -> tuple[list[Any], list[dict[str, Any]]]:
        return await self.dom.walk(closed_shadow=closed_shadow)

    async def scroll(
        self,
        direction: Annotated[
            Literal["down", "up"],
            Field(description="Direction to scroll the page. Example: 'down'"),
        ] = "down",
        amount: Annotated[
            int, Field(description="Distance to scroll in CSS pixels. Example: 500")
        ] = 500,
    ) -> ScrollResult:
        """Scroll the page vertically by a pixel amount.

        Use to reach content below the fold or to trigger lazy loading. To reach
        one known element instead, scroll_to_element is more reliable. Scrolls
        with a synthesized wheel gesture, as a user would. Returns the vertical
        scroll position afterwards.
        """
        y = await self.dom.scroll_by(amount if direction == "down" else -amount)
        return ScrollResult(summary=f"Scrolled {direction} {amount}px (scrollY={y:g})", scroll_y=y)

    async def scroll_to_element(
        self,
        selector: Annotated[
            str,
            Field(
                description="CSS selector, or a numeric id from get_interaction_tree, of the element to scroll into view. Example: '#checkout-button'"
            ),
        ],
    ) -> ScrollResult:
        """Scroll the page until one element is centred in the viewport.

        Use to bring something below the fold into view or to trigger lazy
        loading at a known point. click scrolls on its own, so this is not
        needed before clicking. Fails with element_not_found when the selector
        matches nothing.
        """
        y = await self.element_call(
            selector,
            "function(el) { el.scrollIntoView({block: 'center', inline: 'nearest'});"
            " return window.scrollY; }",
        )
        return ScrollResult(summary=f"Scrolled to: {selector}", target=selector, scroll_y=y)
