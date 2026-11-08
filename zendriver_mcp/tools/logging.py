# logging tools - network and console log management
from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import BaseModel, Field
from zendriver import cdp

from zendriver_mcp.errors import Code, InvalidArgumentError, ZendriverMCPError
from zendriver_mcp.registry import Effect
from zendriver_mcp.response import Result
from zendriver_mcp.session import CALL_TAB
from zendriver_mcp.tools.base import ToolBase

# How often a waiting tool reports progress. Often enough that a client's
# progress bar moves, rarely enough not to flood a stdio pipe.
PROGRESS_INTERVAL = 1.0


class NetworkEntry(BaseModel):
    url: str = Field(description="Request URL.")
    method: str = Field(description="HTTP method.")
    status: int = Field(description="HTTP status; 0 when the request failed outright.")
    status_text: str | None = Field(default=None, description="Status text, or 'FAILED: <reason>'.")
    type: str | None = Field(
        default=None, description="Resource type: Document, XHR, Fetch, Image, ..."
    )
    mime_type: str | None = Field(default=None, description="Response MIME type.")
    timestamp: str | None = Field(default=None, description="When the response arrived (ISO 8601).")
    tab: str | None = Field(default=None, description="Id of the tab that made the request.")


class ConsoleEntry(BaseModel):
    type: str = Field(description="Console method: log, warn, error, info, debug, ...")
    text: str = Field(description="The message, arguments joined by spaces.")
    timestamp: str | None = Field(default=None, description="When it was logged (ISO 8601).")
    tab: str | None = Field(default=None, description="Id of the tab that logged it.")


class NetworkLogs(Result):
    entries: list[NetworkEntry] = Field(description="Most recent last.")
    tab_filter: str | None = Field(
        default=None, description="Tab the entries were limited to, if any."
    )


class ConsoleLogs(Result):
    entries: list[ConsoleEntry] = Field(description="Most recent last.")
    tab_filter: str | None = Field(
        default=None, description="Tab the entries were limited to, if any."
    )


class NetworkWait(Result):
    idle: bool = Field(description="True when the network went quiet; false on timeout.")
    elapsed: float = Field(description="Seconds waited.")
    requests: int = Field(description="Requests captured so far, all tabs.")


class RequestWait(Result):
    request: NetworkEntry = Field(description="The first request that matched.")
    elapsed: float = Field(description="Seconds until it appeared.")


def _call_tab_id(session: Any) -> str | None:
    """The tab id passed as this call's ``tab=``, or None when the caller used the default."""
    tab = CALL_TAB.get()
    return session.tab_id_of(tab) if tab is not None else None


def _resource_type(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw)
    if text.startswith("ResourceType."):
        member = getattr(cdp.network.ResourceType, text.split(".", 1)[1], None)
        if member is not None:
            return str(member.value)
    return text


def _network_entry(log: dict[str, Any]) -> NetworkEntry:
    status = log.get("status")
    return NetworkEntry(
        url=str(log.get("url", "")),
        method=str(log.get("method", "GET")),
        status=int(status) if isinstance(status, int | float) else 0,
        status_text=log.get("status_text"),
        # The session stores str() of CDP's enum ("ResourceType.FETCH"); the
        # protocol's own spelling ("Fetch") is what a caller would match on.
        type=_resource_type(log.get("type")),
        mime_type=log.get("mime_type"),
        timestamp=log.get("timestamp"),
        tab=log.get("tab"),
    )


class LoggingTools(ToolBase):
    """tools for network and console log access"""

    GROUP = "logging"

    def _register_tools(self) -> None:
        """register logging tools"""
        self._register(self.get_network_logs, Effect.READ, "Network log")
        self._register(self.get_console_logs, Effect.READ, "Console log")
        self._register(self.clear_logs, Effect.DESTRUCTIVE, "Clear logs", page_scoped=False)
        self._register(self.wait_for_network, Effect.READ, "Wait for network idle")
        self._register(self.wait_for_request, Effect.READ, "Wait for request")

    async def get_network_logs(
        self,
        limit: Annotated[
            int,
            Field(
                description="Maximum number of most-recent requests to return. Raise it to see more history. Example: 20"
            ),
        ] = 20,
    ) -> NetworkLogs:
        """List recent network responses and failures, captured via CDP.

        Use to find the API endpoint behind a rendered value, or to confirm a
        request fired and what it returned. Capture starts when the browser does.
        Entries from every tab are returned, each naming its tab; pass `tab` to
        see only one tab's traffic. Returns entries with url, method, status,
        type and MIME type, oldest first.
        """
        if limit < 1:
            raise InvalidArgumentError("limit must be at least 1")
        only = _call_tab_id(self.session)
        logs = self.session.get_network_logs(1000)
        if only is not None:
            logs = [entry for entry in logs if entry.get("tab") == only]
        entries = [_network_entry(entry) for entry in logs[-limit:]]
        where = f" in {only}" if only else ""
        summary = (
            f"{len(entries)} network entr{'y' if len(entries) == 1 else 'ies'}{where}"
            if entries
            else f"No network logs captured{where}"
        )
        return NetworkLogs(summary=summary, entries=entries, tab_filter=only)

    async def get_console_logs(
        self,
        limit: Annotated[
            int,
            Field(
                description="Maximum number of most-recent console entries to return. Raise it to see more history. Example: 20"
            ),
        ] = 20,
    ) -> ConsoleLogs:
        """List recent browser console output, captured via CDP.

        Use to surface JavaScript errors explaining why a page rendered wrong or
        an interaction did nothing. Entries from every tab are returned, each
        naming its tab; pass `tab` to see only one tab's. Returns entries with
        type (log, warn, error, ...) and text, oldest first.
        """
        if limit < 1:
            raise InvalidArgumentError("limit must be at least 1")
        only = _call_tab_id(self.session)
        logs = self.session.get_console_logs(500)
        if only is not None:
            logs = [entry for entry in logs if entry.get("tab") == only]
        entries = [
            ConsoleEntry(
                type=str(entry.get("type", "log")),
                text=str(entry.get("text", "")),
                timestamp=entry.get("timestamp"),
                tab=entry.get("tab"),
            )
            for entry in logs[-limit:]
        ]
        where = f" in {only}" if only else ""
        summary = (
            f"{len(entries)} console entr{'y' if len(entries) == 1 else 'ies'}{where}"
            if entries
            else f"No console logs captured{where}"
        )
        return ConsoleLogs(summary=summary, entries=entries, tab_filter=only)

    async def clear_logs(self) -> Result:
        """Discard all captured network and console logs, for every tab.

        Call before an action so the logs that follow contain only that action's
        traffic — much easier to read than filtering a long history.
        """
        self.session.clear_logs()
        return Result(summary="Cleared all logs")

    async def wait_for_network(
        self,
        ctx: Context,
        timeout: Annotated[
            float, Field(description="Maximum seconds to wait before giving up. Example: 10.0")
        ] = 10.0,
        idle_time: Annotated[
            float,
            Field(
                description="Seconds with no new request before the network counts as idle. Example: 0.5"
            ),
        ] = 0.5,
    ) -> NetworkWait:
        """Wait until the page stops making network requests.

        Use after a click or an action that triggers API calls, so later reads
        see the settled page. Not needed after navigate, which settles on its
        own. Use wait_for_request instead when you know which request matters.
        Reports progress while waiting. A timeout is not an error, just an
        unsettled page: the result says idle=false.
        """
        if timeout <= 0 or idle_time <= 0:
            raise InvalidArgumentError("timeout and idle_time must be positive")

        waiter = asyncio.ensure_future(self.session.wait_for_network_idle(timeout, idle_time))
        started = time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({waiter}, timeout=PROGRESS_INTERVAL)
                if done:
                    break
                await ctx.report_progress(
                    min(time.monotonic() - started, timeout),
                    timeout,
                    "waiting for the network to go quiet",
                )
            idle, elapsed, count = waiter.result()
        except asyncio.CancelledError:
            waiter.cancel()
            raise

        if idle:
            return NetworkWait(
                summary=f"Network idle after {elapsed:.1f}s ({count} requests captured)",
                idle=True,
                elapsed=round(elapsed, 2),
                requests=count,
            )
        return NetworkWait(
            summary=f"Timeout after {timeout}s - network may still be active ({count} requests captured)",
            idle=False,
            elapsed=round(elapsed, 2),
            requests=count,
        )

    async def wait_for_request(
        self,
        ctx: Context,
        url_pattern: Annotated[
            str,
            Field(
                description="Case-insensitive substring matched anywhere in the request URL. Not a regex and not a glob. Example: '/api/search'"
            ),
        ],
        timeout: Annotated[
            float, Field(description="Maximum seconds to wait before giving up. Example: 30.0")
        ] = 30.0,
        method: Annotated[
            str | None,
            Field(
                description="HTTP method the request must use, matched case-insensitively. Omit to accept any method. Example: 'POST'"
            ),
        ] = None,
    ) -> RequestWait:
        """Wait until one specific network request appears, matched by URL substring.

        More precise than wait_for_network when you know the endpoint that
        matters — a search API, a GraphQL call. Requests already captured count
        too, so a request that finished between your click and this call is
        still found; call clear_logs before the action to wait for a fresh one.
        Pass `tab` to accept only that tab's requests.
        Reports progress while waiting. Fails with code 'timeout' when nothing
        matched in time.
        """
        if not url_pattern:
            raise InvalidArgumentError("url_pattern must not be empty")
        if timeout <= 0:
            raise InvalidArgumentError("timeout must be positive")

        only = _call_tab_id(self.session)
        needle = url_pattern.lower()
        want = method.upper() if method else None
        started = time.monotonic()
        last_report = started

        while True:
            elapsed = time.monotonic() - started
            for entry in self.session.get_network_logs(1000):
                if only is not None and entry.get("tab") != only:
                    continue
                if needle not in str(entry.get("url", "")).lower():
                    continue
                if want and str(entry.get("method", "GET")).upper() != want:
                    continue
                req = _network_entry(entry)
                return RequestWait(
                    summary=f"Found {req.method} {req.url[:100]} ({req.status}) after {elapsed:.1f}s",
                    request=req,
                    elapsed=round(elapsed, 2),
                )

            if elapsed >= timeout:
                raise ZendriverMCPError(
                    f"No request matching '{url_pattern}' after {timeout}s",
                    code=Code.TIMEOUT,
                    hint="Check get_network_logs for what did fire; the request may have "
                    "happened before this wait started, or under a different URL.",
                )
            now = time.monotonic()
            if now - last_report >= PROGRESS_INTERVAL:
                last_report = now
                await ctx.report_progress(elapsed, timeout, f"waiting for {url_pattern}")
            await asyncio.sleep(0.2)
