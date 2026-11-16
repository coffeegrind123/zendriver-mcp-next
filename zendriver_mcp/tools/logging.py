# logging tools - network and console log management
from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import BaseModel, Field
from zendriver import cdp

from zendriver_mcp.artifacts import publish, resolve_artifact_path
from zendriver_mcp.errors import Code, InvalidArgumentError, ZendriverMCPError
from zendriver_mcp.registry import Effect
from zendriver_mcp.response import Artifact, Result
from zendriver_mcp.session import CALL_TAB
from zendriver_mcp.tools.base import ToolBase

# How often a waiting tool reports progress. Often enough that a client's
# progress bar moves, rarely enough not to flood a stdio pipe.
PROGRESS_INTERVAL = 1.0


class NetworkEntry(BaseModel):
    request_id: str | None = Field(
        default=None, description="Id for get_response_body, which returns this response's body."
    )
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
    size: int | None = Field(
        default=None, description="Bytes received over the network, once the body has arrived."
    )


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


class ResponseBody(Result):
    url: str = Field(description="Request URL.")
    status: int = Field(description="HTTP status.")
    mime_type: str | None = Field(default=None, description="Response MIME type.")
    headers: dict[str, Any] = Field(description="Response headers.")
    request_headers: dict[str, Any] = Field(description="Request headers as sent.")
    post_data: str | None = Field(
        default=None,
        description="The request body, for a request that sent one (cut at 10000 chars).",
    )
    binary: bool = Field(description="Whether the body is binary; if so it is in files, not text.")
    offset: int = Field(description="First character of this slice of the text.")
    end: int = Field(description="One past the last character of this slice.")
    total: int = Field(description="Length of the whole text, or bytes when binary.")
    next_offset: int | None = Field(
        default=None, description="Offset to request next; absent on the last slice."
    )
    text: str | None = Field(default=None, description="The text body, this slice of it.")
    files: list[Artifact] | None = Field(
        default=None, description="The body saved as a file: always for binary, or with save_path."
    )


# Whether a status counts as a failure for errors_only: HTTP errors, and
# requests that never got a response (0).
def _is_error(entry: dict[str, Any]) -> bool:
    status = entry.get("status")
    return not isinstance(status, int | float) or status == 0 or status >= 400


# MIME types whose bodies are text even when Chrome hands them over as base64.
_TEXTUAL = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "+json",
    "+xml",
)

POST_DATA_LIMIT = 10_000


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
        request_id=log.get("request_id"),
        size=log.get("size"),
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
        self._register(self.get_response_body, Effect.READ, "Response body", page_scoped=False)

    async def get_response_body(
        self,
        request_id: Annotated[
            str,
            Field(description="request_id from get_network_logs. Example: '1234.56'"),
        ],
        max_chars: Annotated[
            int,
            Field(
                description="Maximum characters of a text body to return in this call. Example: 20000"
            ),
        ] = 20000,
        offset: Annotated[
            int,
            Field(description="Character offset to start from, for paging a long body. Example: 0"),
        ] = 0,
        save_path: Annotated[
            str | None,
            Field(
                description="Also save the body here (binary always is). Example: '/tmp/data.json'"
            ),
        ] = None,
    ) -> ResponseBody:
        """Get one response's body and headers, and the request's headers and body.

        Reads the API data behind a page directly. Text comes in pages; binary
        is saved as a file. Bodies last while the requesting page is loaded.
        Returns the body, status, MIME type and both sets of headers.
        """
        import base64

        entry = self.session.network_entry(request_id)
        if entry is None:
            raise InvalidArgumentError(
                f"No captured request has id {request_id!r}.",
                hint="Take request_id from get_network_logs; ids from a stopped browser are gone.",
            )
        tab_id = entry.get("tab")
        tab = self.session.tab_for_call(tab_id) if tab_id else self.session.page
        rid = cdp.network.RequestId(request_id)
        try:
            body, encoded = await tab.send(cdp.network.get_response_body(request_id=rid))
        except Exception as exc:  # ProtocolException: evicted, or never finished
            raise ZendriverMCPError(
                f"Chrome no longer has the body of {entry.get('url')}: {exc}",
                hint="Bodies last while the page that fetched them is loaded. Reload or redo "
                "the action, then ask again soon after.",
            ) from exc
        post: str | None = None
        if entry.get("has_post_data"):
            try:
                data, data_b64 = await tab.send(cdp.network.get_request_post_data(request_id=rid))
                post = (
                    base64.b64decode(data).decode("utf-8", errors="replace") if data_b64 else data
                )
            except Exception:  # not kept for this request
                post = None
        mime = entry.get("mime_type") or ""
        raw = base64.b64decode(body) if encoded else body.encode("utf-8")
        textual = not encoded or any(t in mime for t in _TEXTUAL)
        files = None
        if save_path or not textual:
            ext = (mime.split("/")[-1].split(";")[0].split("+")[-1] or "bin")[:8]
            path = resolve_artifact_path(
                save_path or "", default_prefix="response", default_ext=ext
            )
            path.write_bytes(raw)
            files = [publish(path, mime or None)]
        status = int(entry.get("status") or 0)
        url = str(entry.get("url", ""))
        headers: dict[str, Any] = entry.get("headers") or {}
        request_headers: dict[str, Any] = entry.get("request_headers") or {}
        post_data = post[:POST_DATA_LIMIT] if post else None
        if not textual:
            where = files[0].path if files else "?"
            return ResponseBody(
                summary=f"{len(raw)} bytes of {mime or 'binary'} saved to {where}",
                url=url,
                status=status,
                mime_type=mime or None,
                headers=headers,
                request_headers=request_headers,
                post_data=post_data,
                binary=True,
                offset=0,
                end=0,
                total=len(raw),
                files=files,
            )
        text = raw.decode("utf-8", errors="replace")
        max_chars = max(1, max_chars)
        start = min(max(0, offset), len(text))
        end = min(start + max_chars, len(text))
        nxt = end if end < len(text) else None
        return ResponseBody(
            summary=f"[chars {start}-{end} of {len(text)}] {status} {mime}"
            + (f" (next: offset={nxt})" if nxt is not None else ""),
            url=url,
            status=status,
            mime_type=mime or None,
            headers=headers,
            request_headers=request_headers,
            post_data=post_data,
            binary=False,
            offset=start,
            end=end,
            total=len(text),
            next_offset=nxt,
            text=text[start:end],
            files=files,
        )

    async def get_network_logs(
        self,
        limit: Annotated[
            int,
            Field(
                description="Maximum number of most-recent requests to return. Raise it to see more history. Example: 20"
            ),
        ] = 20,
        errors_only: Annotated[
            bool,
            Field(description="Only failures and HTTP errors (status 0 or >= 400). Example: true"),
        ] = False,
        url_contains: Annotated[
            str | None,
            Field(description="Only requests whose URL contains this. Example: '/api/'"),
        ] = None,
        types: Annotated[
            list[str] | None,
            Field(description="Only these resource types. Example: ['XHR', 'Fetch']"),
        ] = None,
    ) -> NetworkLogs:
        """List recent network responses and failures, captured via CDP.

        Use to find the API endpoint behind a rendered value, or to confirm a
        request fired and what it returned. Capture starts when the browser does.
        Entries from every tab are returned, each naming its tab; pass `tab` to
        see only one tab's traffic. Filters narrow it to errors, a URL
        fragment or resource types. Returns entries with request_id (for
        get_response_body), url, method, status, type, MIME type and size,
        oldest first.
        """
        if limit < 1:
            raise InvalidArgumentError("limit must be at least 1")
        only = _call_tab_id(self.session)
        logs = self.session.get_network_logs(1000)
        if only is not None:
            logs = [entry for entry in logs if entry.get("tab") == only]
        if errors_only:
            logs = [entry for entry in logs if _is_error(entry)]
        if url_contains:
            logs = [entry for entry in logs if url_contains in str(entry.get("url", ""))]
        if types:
            wanted = {t.lower() for t in types}
            logs = [
                entry
                for entry in logs
                if (_resource_type(entry.get("type")) or "").lower() in wanted
            ]
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
        traffic — much easier to read than filtering a long history. Returns a confirmation.
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
        Returns whether the network went idle, the seconds waited and the request count.
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
        matched in time. Returns the first matching request and how long it took to appear.
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
