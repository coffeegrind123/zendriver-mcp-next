"""Files the browser downloads, captured for the session.

Chrome saves each download under its GUID in a directory of ours
(``Browser.setDownloadBehavior`` with ``allowAndName``) and reports progress as
browser-level events; on completion the file is renamed to the name the page
suggested and published as a ``browser://artifacts/`` resource. Nothing about
this is visible to the page.

Measured 2026-11-15 on headed Chrome 157: DownloadWillBegin carries the
suggested filename, DownloadProgress ends in ``completed``, and the file lands
as ``<dir>/<guid>``.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import zendriver as zd
from zendriver import cdp

from zendriver_mcp import artifacts

logger = logging.getLogger(__name__)

# Downloads kept in the session's list.
LOG_SIZE = 100


@dataclass
class Download:
    guid: str
    url: str
    suggested: str
    started: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    state: str = "inProgress"
    received: int = 0
    total: int = 0
    path: str | None = None
    uri: str | None = None
    # Whether wait_for_download has handed this one out already.
    reported: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "filename": self.suggested,
            "state": self.state,
            "received": self.received,
            "total": self.total,
            "path": self.path,
            "uri": self.uri,
            "started": self.started,
        }


class Downloads:
    def __init__(self) -> None:
        self.dir: Path | None = None
        self.items: dict[str, Download] = {}
        self._changed = asyncio.Event()

    def reset(self) -> None:
        self.items.clear()
        self.dir = None

    async def attach(self, browser: zd.Browser) -> None:
        """Route the browser's downloads into a directory of ours, with events."""
        self.dir = Path(tempfile.mkdtemp(prefix="zendriver-mcp-downloads-"))
        conn = browser.connection
        if conn is None:
            return
        conn.add_handler(cdp.browser.DownloadWillBegin, self._on_begin)
        conn.add_handler(cdp.browser.DownloadProgress, self._on_progress)
        await conn.send(
            cdp.browser.set_download_behavior(
                behavior="allowAndName", download_path=str(self.dir), events_enabled=True
            )
        )

    async def _on_begin(self, event: cdp.browser.DownloadWillBegin) -> None:
        self.items[event.guid] = Download(
            guid=event.guid, url=event.url, suggested=event.suggested_filename or event.guid
        )
        for guid in list(self.items)[:-LOG_SIZE]:
            del self.items[guid]
        self._poke()

    async def _on_progress(self, event: cdp.browser.DownloadProgress) -> None:
        item = self.items.get(event.guid)
        if item is None:
            return
        item.received = int(event.received_bytes)
        item.total = int(event.total_bytes)
        item.state = str(event.state.value if hasattr(event.state, "value") else event.state)
        if item.state == "completed" and self.dir is not None:
            self._finish(item)
        if item.state != "inProgress":
            self._poke()

    def _finish(self, item: Download) -> None:
        assert self.dir is not None
        src = self.dir / item.guid
        name = Path(item.suggested).name or item.guid
        dest = self.dir / name
        n = 1
        while dest.exists():
            n += 1
            dest = self.dir / f"{Path(name).stem}-{n}{Path(name).suffix}"
        try:
            src.rename(dest)
        except OSError:
            logger.debug("could not rename download %s", src, exc_info=True)
            dest = src
        item.path = str(dest)
        item.uri = artifacts.publish(dest).uri

    def _poke(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def next_unreported(self, url_contains: str | None) -> Download | None:
        for item in self.items.values():
            if item.reported or item.state == "inProgress":
                continue
            if url_contains and url_contains not in item.url and url_contains not in item.suggested:
                continue
            return item
        return None

    async def changed(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._changed.wait(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            pass
