# tab management tools - new tab, list, switch, close
import asyncio
from typing import Annotated

from pydantic import BaseModel, Field
from zendriver import cdp

from zendriver_mcp.errors import InvalidArgumentError
from zendriver_mcp.registry import Effect, Reach
from zendriver_mcp.response import Result
from zendriver_mcp.tools.base import ToolBase
from zendriver_mcp.tools.browser import TabInfo


class TabResult(Result):
    tab: str = Field(description="Id of the tab acted on.")
    url: str | None = Field(default=None, description="Its URL, when known.")


class OpenedTab(BaseModel):
    url: str = Field(description="The URL asked for.")
    tab: str | None = Field(default=None, description="Id of the tab it opened in.")
    error: str | None = Field(
        default=None,
        description="Why it failed: no tab, or a tab showing Chrome's error page (tab is then set).",
    )


class OpenedTabs(Result):
    tabs: list[OpenedTab] = Field(description="One entry per URL, in the order given.")
    opened: int = Field(description="How many opened and loaded.")


# At most this many URLs per open_tabs call, and in flight at once.
MAX_TABS_PER_CALL = 20
MAX_CONCURRENCY = 8


class TabList(Result):
    tabs: list[TabInfo] = Field(description="Open tabs this client can see.")


class TabTools(ToolBase):
    """tools for multi-tab management"""

    GROUP = "tabs"

    def _register_tools(self) -> None:
        """register tab management tools"""
        self._register(self.new_tab, Effect.WRITE, "Open tab", reach=Reach.WEB, page_scoped=False)
        self._register(
            self.open_tabs, Effect.WRITE, "Open several tabs", reach=Reach.WEB, page_scoped=False
        )
        self._register(self.list_tabs, Effect.READ, "List tabs", page_scoped=False)
        self._register(
            self.switch_tab, Effect.WRITE, "Switch tab", idempotent=True, page_scoped=False
        )
        self._register(self.close_tab, Effect.DESTRUCTIVE, "Close tab", page_scoped=False)

    async def new_tab(
        self,
        url: Annotated[
            str | None,
            Field(
                description="Absolute URL to open in the new tab, including the scheme. Omit to open a blank tab. Example: 'https://example.com'"
            ),
        ] = None,
    ) -> TabResult:
        """Open a new browser tab and make it the active tab.

        Every subsequent tool acts on this new tab until switch_tab is called.
        Returns a confirmation containing the new tab's id, which is what
        switch_tab and close_tab take.
        """
        tab_id, tab = await self.session.create_tab(url)
        self.session.page = tab
        return TabResult(
            summary=f"Opened new tab: {tab_id}" + (f" at {url}" if url else ""),
            tab=tab_id,
            url=getattr(tab, "url", None) or url,
        )

    async def open_tabs(
        self,
        urls: Annotated[
            list[str],
            Field(
                description=f"Absolute URLs to open, each in its own new tab; at most {MAX_TABS_PER_CALL}. Example: ['https://example.com/a', 'https://example.com/b']"
            ),
        ],
        concurrency: Annotated[
            int,
            Field(description=f"How many load at once, 1 to {MAX_CONCURRENCY}. Example: 4"),
        ] = 4,
    ) -> OpenedTabs:
        """Open several URLs in new tabs at once, loading them in parallel.

        Read each with a tool's tab argument; the active tab does not change.
        A URL that fails is reported per entry and does not stop the others. Returns each URL's tab id or error, in the order given.
        """
        if not urls:
            raise InvalidArgumentError("Pass at least one URL.")
        if len(urls) > MAX_TABS_PER_CALL:
            raise InvalidArgumentError(
                f"{len(urls)} URLs; at most {MAX_TABS_PER_CALL} per call.",
                hint="Split them over several calls.",
            )
        gate = asyncio.Semaphore(max(1, min(concurrency, MAX_CONCURRENCY)))

        async def one(url: str) -> OpenedTab:
            async with gate:
                try:
                    tab_id, tab = await self.session.create_tab(url)
                except Exception as exc:  # one bad URL must not sink the rest
                    return OpenedTab(url=url, error=str(exc).splitlines()[0][:300])
                # A refused or unresolvable URL still opens a tab, on Chrome's
                # error page, which the frame tree marks with unreachable_url.
                try:
                    tree = await tab.send(cdp.page.get_frame_tree())
                except Exception:
                    return OpenedTab(url=url, tab=tab_id)
                if tree.frame.unreachable_url:
                    return OpenedTab(url=url, tab=tab_id, error="the page could not be loaded")
                return OpenedTab(url=url, tab=tab_id)

        results = await asyncio.gather(*(one(u) for u in urls))
        opened = sum(1 for r in results if r.tab and not r.error)
        failed = len(results) - opened
        return OpenedTabs(
            summary=f"Opened {opened} of {len(urls)} tab(s)"
            + (f"; {failed} failed" if failed else ""),
            tabs=list(results),
            opened=opened,
        )

    async def list_tabs(self) -> TabList:
        """List every open tab with its id and current URL.

        Use to discover tab ids before switch_tab or close_tab, or to find a tab
        opened by the page itself (target=_blank, popups get ids here too).
        Returns every tab with its id, URL and whether it is the active one.
        """
        tabs = self.session.get_all_tabs() if self.session.is_running() else {}
        active = self.session.active_tab_id() if tabs else None
        if not tabs:
            return TabList(summary="No tabs open", tabs=[])
        return TabList(
            summary=f"{len(tabs)} open tab(s), active {active}",
            tabs=[TabInfo(id=t, url=u, active=t == active) for t, u in tabs.items()],
        )

    async def switch_tab(
        self,
        tab_id: Annotated[
            str,
            Field(
                description="Id of the tab to activate, exactly as returned by list_tabs or new_tab. Not a URL and not a positional index."
            ),
        ],
    ) -> TabResult:
        """Make an existing tab the active one, so later tools act on it.

        Get valid ids from list_tabs. Returns a confirmation naming the tab
        switched to.
        """
        tab = await self.session.switch_tab(tab_id)
        return TabResult(summary=f"Switched to {tab_id}", tab=tab_id, url=getattr(tab, "url", None))

    async def close_tab(
        self,
        tab_id: Annotated[
            str,
            Field(
                description="Id of the tab to close, exactly as returned by list_tabs or new_tab. Not a URL and not a positional index."
            ),
        ],
    ) -> TabResult:
        """Close one tab by id, leaving the browser and other tabs running.

        Use stop_browser to end the whole session instead. Returns a confirmation
        naming the closed tab.
        """
        await self.session.close_tab(tab_id)
        return TabResult(summary=f"Closed tab: {tab_id}", tab=tab_id)
