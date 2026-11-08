# tab management tools - new tab, list, switch, close
from typing import Annotated

from pydantic import Field

from src.registry import Effect, Reach
from src.response import Result
from src.tools.base import ToolBase
from src.tools.browser import TabInfo


class TabResult(Result):
    tab: str = Field(description="Id of the tab acted on.")
    url: str | None = Field(default=None, description="Its URL, when known.")


class TabList(Result):
    tabs: list[TabInfo] = Field(description="Open tabs this client can see.")


class TabTools(ToolBase):
    """tools for multi-tab management"""

    GROUP = "tabs"

    def _register_tools(self) -> None:
        """register tab management tools"""
        self._register(self.new_tab, Effect.WRITE, "Open tab", reach=Reach.WEB, page_scoped=False)
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
