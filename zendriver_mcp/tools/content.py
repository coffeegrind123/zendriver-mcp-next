# page content tools - get html, get text, scroll
import json
from typing import Annotated, Any, Literal

from pydantic import Field

from zendriver_mcp.registry import Effect
from zendriver_mcp.response import Result
from zendriver_mcp.tools.base import ToolBase


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
            "dlg), and when relevant h link target, it input type, v value, ck checked, "
            "dis disabled, off offscreen."
        )
    )
    total: int = Field(description="How many interactive elements the page has.")
    truncated: bool = Field(description="Whether `limit` cut the list short.")

    def render(self) -> str:
        payload = json.dumps(self.elements, separators=(",", ":"), ensure_ascii=False)
        if self.truncated:
            return (
                f"[showing {len(self.elements)} of {self.total} elements; raise limit for more]"
                f"\n{payload}"
            )
        return payload


class ScrollResult(Result):
    target: str | None = Field(default=None, description="Selector scrolled to, if any.")
    scroll_y: float | None = Field(default=None, description="Vertical scroll position afterwards.")


class ContentTools(ToolBase):
    """tools for page content and scrolling"""

    GROUP = "content"

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
    ) -> TextPage:
        """Get the page's visible text, without any HTML markup.

        Use for reading or extracting page copy. Text hidden by CSS is excluded,
        and no selectors are returned, so use get_interaction_tree if you intend
        to interact. Same pagination contract as get_content. An empty result
        carries a diagnosis saying whether the page is still loading, has no
        body, keeps its text in iframes or shadow roots, or really is blank.
        """
        # Null-safe on purpose. `document.body.innerText` THROWS when the body
        # does not exist yet, and a thrown CDP exception reaches the caller as a
        # stack trace plus a parameter dump — which says nothing about the page
        # and sends them looking for a bad argument. Observed for real:
        # `TypeError: Cannot read properties of null (reading 'innerText')` on a
        # page that had simply not finished loading. Returning empty lets the
        # diagnosis below say which of the possible causes it actually is.
        text = await self.run_js("document.body ? document.body.innerText : ''")
        page = self._paginate(str(text or ""), max_chars, offset)
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
    ) -> InteractionTree:
        """List the page's interactive elements, each with a short numeric id.

        The cheapest way to see what can be clicked or typed into: it walks the
        DOM including shadow roots for buttons, links, and inputs. The numeric ids
        it assigns are accepted directly by click, type_text, and the other
        element tools in place of a CSS selector. An id stays valid while its
        element exists, survives later walks, and expires on navigation; an
        expired one fails with stale_id. The walk runs in an isolated world and
        leaves no trace in the page. Returns the elements (capped at limit),
        the total, and whether the cap cut the list short.

        Pass links=true to get each link's target as `h` — a path when it is
        same-origin, a full URL otherwise. Following a link then means a navigate
        instead of a click-and-hope, which matters when the link text is
        unhelpful or missing. It is off by default because it roughly doubles the
        output; measured at +116% on a 125-link search page. Nothing is emitted
        for javascript: handlers, bare "#" anchors, or links back to the current
        page, none of which can take you anywhere.
        """
        tree = await self._walk()
        # The walker always collects link targets; whether the CALLER pays for
        # them is decided here, because the cost is in the returned JSON rather
        # than in the walk. Non-dict entries cannot come from our walker, but it
        # is JavaScript and validates nothing, so they are dropped, not trusted.
        elements = [dict(e) for e in tree if isinstance(e, dict)]
        if not links:
            for element in elements:
                element.pop("h", None)
        limit = max(1, limit)
        total = len(elements)
        shown = elements[:limit]
        return InteractionTree(
            summary=f"{total} interactive element(s)"
            + (f", showing {limit}" if total > limit else ""),
            elements=shown,
            total=total,
            truncated=total > limit,
        )

    async def _walk(self) -> list[Any]:
        return await self.dom.walk()

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
        page = self.session.page
        if direction == "down":
            await page.scroll_down(amount)
        else:
            await page.scroll_up(amount)
        y = await self.run_js("window.scrollY")
        return ScrollResult(summary=f"Scrolled {direction} {amount}px (scrollY={y})", scroll_y=y)

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
