"""localStorage restored for origins that no tab has open yet.

``DOMStorage.setDOMStorageItem`` needs a frame of the origin to be open
("Frame not found for the given storage id", measured 2026-11-15), so
import_session can write the open origins directly but not the rest. For those,
a document-start script in our isolated world writes the items when a document
of that origin is created, before the page's own scripts run, so the page finds
them as if they had always been there. Isolated-world scripts are invisible to
the page.

Each origin is restored once: once a load of it is seen holding the items, it
is dropped from the script, so a site that later clears its storage (a logout)
is not undone on its next load.
"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlsplit

import zendriver as zd
from zendriver import cdp

from zendriver_mcp.dom import WORLD_NAME

logger = logging.getLogger(__name__)

# How long after a navigation to check that the script wrote the items.
_VERIFY_DELAY = 0.5


def origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return None


class PendingStorage:
    def __init__(self) -> None:
        self.pending: dict[str, dict[str, str]] = {}
        self._scripts: dict[int, tuple[zd.Tab, str]] = {}

    def reset(self) -> None:
        self.pending.clear()
        self._scripts.clear()

    def _source(self) -> str:
        data = json.dumps(self.pending)
        return (
            "(() => { const P = " + data + "; const items = P[location.origin];"
            " if (!items) return;"
            " try { for (const [k, v] of Object.entries(items)) localStorage.setItem(k, v); }"
            " catch (e) {} })()"
        )

    async def install(self, tab: zd.Tab) -> None:
        """(Re)register the document-start script on ``tab`` for what is still pending."""
        old = self._scripts.pop(id(tab), None)
        if old is not None:
            try:
                await tab.send(
                    cdp.page.remove_script_to_evaluate_on_new_document(
                        identifier=cdp.page.ScriptIdentifier(old[1])
                    )
                )
            except Exception:
                logger.debug("could not remove restore script", exc_info=True)
        if not self.pending:
            return
        ident = await tab.send(
            cdp.page.add_script_to_evaluate_on_new_document(
                source=self._source(), world_name=WORLD_NAME
            )
        )
        self._scripts[id(tab)] = (tab, str(ident))

    async def install_all(self, tabs: list[zd.Tab]) -> None:
        for tab in tabs:
            try:
                await self.install(tab)
            except Exception:
                logger.debug("could not install restore script", exc_info=True)

    async def on_navigated(self, event: cdp.page.FrameNavigated, *, tab: zd.Tab) -> None:
        origin = origin_of(event.frame.url)
        if origin is None or origin not in self.pending:
            return
        # Checked from a task: this handler runs on zendriver's receive loop.
        asyncio.get_running_loop().create_task(self._verify(origin, tab))

    async def _verify(self, origin: str, tab: zd.Tab) -> None:
        await asyncio.sleep(_VERIFY_DELAY)
        wanted = self.pending.get(origin)
        if wanted is None:
            return
        try:
            items = await tab.send(
                cdp.dom_storage.get_dom_storage_items(
                    storage_id=cdp.dom_storage.StorageId(
                        is_local_storage=True, security_origin=origin
                    )
                )
            )
        except Exception:
            return  # the frame went away; the next load will try again
        have = {k: v for k, v in items}
        if all(have.get(k) == v for k, v in wanted.items()):
            self.pending.pop(origin, None)
            await self.install_all([t for t, _ in self._scripts.values()])
