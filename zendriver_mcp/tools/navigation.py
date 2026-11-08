# navigation tools - navigate, back, forward, reload, page info
from typing import Annotated

from pydantic import Field

from zendriver_mcp.registry import Effect, Reach
from zendriver_mcp.response import Result
from zendriver_mcp.tools.base import ToolBase


class NavigateResult(Result):
    url: str = Field(description="The URL requested.")
    final_url: str | None = Field(
        default=None, description="Where the tab ended up, after redirects."
    )
    settled: bool = Field(description="Whether the page reached network idle within `settle`.")
    document_ready: bool = Field(description="Whether document.readyState reached 'complete'.")
    elapsed: float = Field(description="Seconds spent settling.")
    requests: int | None = Field(default=None, description="Network requests seen so far.")


class PageInfo(Result):
    url: str = Field(description="Current URL, or 'unknown'.")
    title: str = Field(description="Current title, or 'unknown'.")
    tab: str | None = Field(default=None, description="Id of the tab described.")


class NavigationTools(ToolBase):
    """tools for page navigation"""

    GROUP = "navigation"

    def _register_tools(self) -> None:
        """register navigation tools"""
        self._register(self.navigate, Effect.WRITE, "Navigate to URL", reach=Reach.WEB)
        self._register(self.go_back, Effect.WRITE, "Go back", reach=Reach.WEB)
        self._register(self.go_forward, Effect.WRITE, "Go forward", reach=Reach.WEB)
        self._register(
            self.reload_page, Effect.WRITE, "Reload page", reach=Reach.WEB, idempotent=True
        )
        self._register(self.get_page_info, Effect.READ, "Page URL and title")

    async def navigate(
        self,
        url: Annotated[
            str,
            Field(
                description="Absolute URL to load, including the scheme. Relative paths and bare hostnames are not resolved. Example: 'https://example.com/pricing'"
            ),
        ],
        settle: Annotated[
            float,
            Field(
                description="Seconds to keep waiting for the network to go quiet after the page commits, so the very next read sees rendered content. 0 returns immediately. Example: 10.0"
            ),
        ] = 10.0,
    ) -> NavigateResult:
        """Navigate the active tab to a URL, then wait for it to finish rendering.

        Use this before any element query, click, or content read — those tools
        fail if no page is loaded. Call start_browser first if the browser is not
        running. You do NOT need a separate wait after this: the settle step is
        built in, so get_text_content straight afterwards sees the loaded page.
        Returns the URL plus how the settle ended, e.g. 'network idle after 0.6s'
        or 'still active after 10.0s' — the latter is normal for a page that
        polls or streams, and the content is usually readable anyway.
        """
        tab = await self.session.navigate(url)
        if settle <= 0:
            return NavigateResult(
                summary=f"Navigated to {url} (no settle requested)",
                url=url,
                settled=False,
                document_ready=False,
                elapsed=0.0,
            )

        # Document readiness FIRST, then network idle. Idle alone cannot tell
        # "the page finished" from "the page has not started" — both look like a
        # request count that is not moving — and getting that wrong returns a
        # page whose `document.body` is still null.
        ready, ready_elapsed = await self.session.wait_for_document_ready(timeout=settle)
        remaining = max(0.0, settle - ready_elapsed)
        if not ready:
            return NavigateResult(
                summary=(
                    f"Navigated to {url} (document still loading after {ready_elapsed:.1f}s — "
                    "content may be incomplete)"
                ),
                url=url,
                final_url=getattr(tab, "url", None),
                settled=False,
                document_ready=False,
                elapsed=ready_elapsed,
            )

        idle, elapsed, count = await self.session.wait_for_network_idle(timeout=remaining)
        total = ready_elapsed + elapsed
        state = (
            f"network idle after {total:.1f}s"
            if idle
            else f"network still active after {total:.1f}s"
        )
        return NavigateResult(
            summary=f"Navigated to {url} ({state}, {count} requests)",
            url=url,
            final_url=getattr(tab, "url", None),
            settled=idle,
            document_ready=True,
            elapsed=round(total, 2),
            requests=count,
        )

    async def go_back(self) -> Result:
        """Go back one entry in the active tab's history, as the browser back button does.

        Only useful after at least one navigation in this tab. Returns a
        confirmation string; it does not report the resulting URL, so follow with
        get_page_info if you need to know where you landed.
        """
        await self.session.page.back()
        return Result(summary="Navigated back")

    async def go_forward(self) -> Result:
        """Go forward one entry in the active tab's history, as the browser forward button does.

        Only meaningful after go_back. Returns a confirmation string; use
        get_page_info if you need the resulting URL.
        """
        await self.session.page.forward()
        return Result(summary="Navigated forward")

    async def reload_page(self) -> Result:
        """Reload the active tab at its current URL.

        Use to pick up server-side changes or to retry a page that rendered
        incompletely. Element IDs from get_interaction_tree do not survive a
        reload — re-run it afterwards. Returns a confirmation string.
        """
        await self.session.page.reload()
        return Result(summary="Page reloaded")

    async def get_page_info(self) -> PageInfo:
        """Get the active tab's current URL and title.

        Use to confirm where a navigation, redirect, or history move actually
        landed. Returns the URL and title, each 'unknown' if the page has not
        reported it yet, and the tab's id.
        """
        page = self.session.page
        # The target's cached title and URL lag behind the document (a new tab
        # reports its URL as its title until Chrome's next target update), so
        # ask the document itself, from the isolated world the page cannot see.
        live = None
        try:
            live = await self.run_js("({t: document.title, u: location.href})", timeout=5)
        except Exception:
            pass
        url = (live or {}).get("u") or getattr(page, "url", None) or "unknown"
        title = (live or {}).get("t") or getattr(page, "title", None) or "unknown"
        return PageInfo(
            summary=f"URL: {url}\nTitle: {title}",
            url=url,
            title=title,
            tab=self.session.tab_id_of(page),
        )
