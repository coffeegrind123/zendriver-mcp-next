"""DevTools-parity tools: performance traces, heap snapshots.

These use the lower-level CDP event streams (``Tracing.dataCollected``,
``HeapProfiler.addHeapSnapshotChunk``) rather than higher-level wrappers, so
the captured data is byte-for-byte what Chrome DevTools would save.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver import MCPServer as FastMCP
from pydantic import Field
from zendriver import cdp

from src.artifacts import publish, resolve_artifact_path
from src.errors import TracingError
from src.registry import Effect
from src.response import FileResult, Result
from src.tools.base import ToolBase

FILE_PATH_DESCRIPTION = (
    "Where to write the file: a path under $HOME, the temp dir, or "
    "ZENDRIVER_MCP_ARTIFACT_ROOT. An existing file there is overwritten. Omit for a "
    "timestamped file in the temp dir. Example: '/tmp/page.json'"
)


class TraceStarted(Result):
    categories: str = Field(description="The CDP category filter in effect.")
    tab: str | None = Field(default=None, description="Id of the tab being traced.")


class TraceSaved(FileResult):
    events: int = Field(description="Number of trace events written.")


class HeapSnapshotSaved(FileResult):
    chunks: int = Field(description="Number of snapshot chunks received.")
    bytes: int = Field(description="Snapshot size in bytes.")


DataHandler = Callable[[cdp.tracing.DataCollected], Awaitable[None]]
CompleteHandler = Callable[[cdp.tracing.TracingComplete], Awaitable[None]]


def _safe_detach(tab: object, event_type: type, handler: object) -> None:
    """Remove ``handler`` from ``tab.handlers[event_type]`` without raising.

    ``tab.handlers.get(x, []).remove(...)`` returns a throwaway list on a
    missing key, so the removal silently no-ops in that case; and calling
    ``.remove()`` on a list that doesn't hold ``handler`` would raise
    ``ValueError``. Both are tolerated here.
    """
    handlers: list = getattr(tab, "handlers", {}).get(event_type)  # type: ignore[assignment]
    if handlers and handler in handlers:
        handlers.remove(handler)


# Roughly matches DevTools' default "Performance" trace profile.
_DEFAULT_CATEGORIES = ",".join(
    [
        "devtools.timeline",
        "v8.execute",
        "disabled-by-default-devtools.timeline",
        "disabled-by-default-devtools.timeline.frame",
        "toplevel",
        "blink.console",
        "blink.user_timing",
        "latencyInfo",
        "disabled-by-default-v8.cpu_profiler",
        "disabled-by-default-devtools.timeline.stack",
    ]
)


class DevToolsTools(ToolBase):
    """Performance trace recording and heap snapshot capture."""

    def __init__(self, mcp: FastMCP) -> None:
        self._trace_events: list[dict] | None = None
        self._trace_complete: asyncio.Event | None = None
        self._trace_handlers: tuple[DataHandler, CompleteHandler] | None = None
        # The tab the trace runs on. stop_trace must end THAT trace, even if the
        # active tab changed in between; ending tracing on another tab fails.
        self._trace_tab: Any = None
        super().__init__(mcp)

    def _reset_state(self) -> None:
        self._trace_events = None
        self._trace_complete = None
        self._trace_handlers = None
        self._trace_tab = None

    GROUP = "devtools"

    def _register_tools(self) -> None:
        self._register(self.start_trace, Effect.WRITE, "Start performance trace")
        # stop_trace waits up to 30s internally for tracingComplete; pad it.
        # Not page-scoped: it always ends the trace on the tab it started on.
        self._register(
            self.stop_trace, Effect.WRITE, "Stop trace and save", page_scoped=False, timeout=120
        )
        # Heap snapshots on large pages stream megabytes of chunks.
        self._register(self.take_heap_snapshot, Effect.WRITE, "Save heap snapshot", timeout=180)

    async def start_trace(
        self,
        categories: Annotated[
            str,
            Field(
                description="Comma-separated CDP trace categories. Empty uses the DevTools Performance panel's default profile. Example: 'devtools.timeline,v8.execute'"
            ),
        ] = "",
    ) -> TraceStarted:
        """Begin recording a performance trace on the current tab.

        Only one trace can run at a time; end it with stop_trace, which writes
        a file DevTools' Performance panel can load. Fails with a conflict
        error if a trace is already running.
        """
        if self._trace_events is not None:
            raise TracingError("A trace is already in progress.", hint="Call stop_trace first.")

        events: list[dict] = []
        complete = asyncio.Event()

        async def on_data(event: cdp.tracing.DataCollected) -> None:
            events.extend(event.value)

        async def on_complete(_: cdp.tracing.TracingComplete) -> None:
            complete.set()

        tab = self.session.page
        tab.add_handler(cdp.tracing.DataCollected, on_data)
        tab.add_handler(cdp.tracing.TracingComplete, on_complete)

        self._trace_events = events
        self._trace_complete = complete
        self._trace_handlers = (on_data, on_complete)
        self._trace_tab = tab

        cats = categories or _DEFAULT_CATEGORIES
        try:
            await tab.send(cdp.tracing.start(categories=cats, transfer_mode="ReportEvents"))
        except Exception:
            _safe_detach(tab, cdp.tracing.DataCollected, on_data)
            _safe_detach(tab, cdp.tracing.TracingComplete, on_complete)
            self._reset_state()
            raise
        tid = self.session.tab_id_of(tab)
        return TraceStarted(summary=f"Trace started on {tid}", categories=cats, tab=tid)

    async def stop_trace(
        self,
        ctx: Context,
        file_path: Annotated[str, Field(description=FILE_PATH_DESCRIPTION)] = "",
    ) -> TraceSaved:
        """Stop the running trace and save it as JSON DevTools can load.

        The file is also published as a browser://artifacts/ resource. The
        output matches what Chrome DevTools' "Load profile" accepts.

        Handlers and state are cleared via ``finally`` so a failed
        ``tracing.end()`` or a late ``tracingComplete`` never leaves the
        tool stuck on "a trace is already in progress" for the rest of the
        process.
        """
        if (
            self._trace_events is None
            or self._trace_complete is None
            or self._trace_handlers is None
        ):
            raise TracingError("No trace in progress. Call start_trace first.")

        events = self._trace_events
        complete = self._trace_complete
        on_data, on_complete = self._trace_handlers
        tab = self._trace_tab or self.session.page

        try:
            await ctx.report_progress(0, 2, "ending trace")
            await tab.send(cdp.tracing.end())
            try:
                await asyncio.wait_for(complete.wait(), timeout=30.0)
            except (TimeoutError, asyncio.TimeoutError) as exc:
                raise TracingError("Trace did not complete within 30s") from exc
        finally:
            # Drop handlers whether we succeeded or failed; either way the
            # tool is no longer mid-trace.
            _safe_detach(tab, cdp.tracing.DataCollected, on_data)
            _safe_detach(tab, cdp.tracing.TracingComplete, on_complete)
            self._reset_state()

        await ctx.report_progress(1, 2, f"writing {len(events)} events")
        target = resolve_artifact_path(
            file_path, default_prefix="zendriver-trace", default_ext="json"
        )
        target.write_text(json.dumps({"traceEvents": events}))
        await ctx.report_progress(2, 2, "saved")
        return TraceSaved(
            summary=f"Trace saved: {target} ({len(events)} events)",
            files=[publish(target, "application/json")],
            events=len(events),
        )

    async def take_heap_snapshot(
        self,
        ctx: Context,
        file_path: Annotated[str, Field(description=FILE_PATH_DESCRIPTION)] = "",
    ) -> HeapSnapshotSaved:
        """Capture a V8 heap snapshot of the page and save it to a file.

        The output is the ``.heapsnapshot`` format Chrome DevTools saves,
        loadable via DevTools > Memory > Load, and is also published as a
        browser://artifacts/ resource. Reports progress while V8 walks the heap.
        """
        tab = self.session.page

        chunks: list[str] = []
        finished = asyncio.Event()
        reports: list[tuple[int, int]] = []

        async def on_chunk(event: cdp.heap_profiler.AddHeapSnapshotChunk) -> None:
            chunks.append(event.chunk)

        async def on_progress(event: cdp.heap_profiler.ReportHeapSnapshotProgress) -> None:
            # Forwarded below from this coroutine's task, not here: zendriver
            # runs handlers in their own tasks, outside the request's context.
            reports.append((event.done, event.total))
            if event.finished:
                finished.set()

        async def forward_progress() -> None:
            sent = 0
            while True:
                await asyncio.sleep(0.25)
                if len(reports) > sent:
                    done, total = reports[-1]
                    sent = len(reports)
                    await ctx.report_progress(done, total or None, "walking the heap")

        tab.add_handler(cdp.heap_profiler.AddHeapSnapshotChunk, on_chunk)
        tab.add_handler(cdp.heap_profiler.ReportHeapSnapshotProgress, on_progress)

        forwarder = asyncio.create_task(forward_progress())
        try:
            await tab.send(cdp.heap_profiler.enable())
            await tab.send(cdp.heap_profiler.take_heap_snapshot(report_progress=True))
            # Once take_heap_snapshot's RPC returns, chunks are all delivered.
            # Wait briefly for the last progress ping if the backend emits one.
            try:
                await asyncio.wait_for(finished.wait(), timeout=2.0)
            except (TimeoutError, asyncio.TimeoutError):
                pass  # some Chrome versions don't emit the final progress event
        finally:
            forwarder.cancel()
            _safe_detach(tab, cdp.heap_profiler.AddHeapSnapshotChunk, on_chunk)
            _safe_detach(tab, cdp.heap_profiler.ReportHeapSnapshotProgress, on_progress)
            # Disable now, from this task. zendriver cannot map "HeapProfiler" to
            # its heap_profiler module (it lowercases to "heapprofiler"), so the
            # domain stays in tab.enabled_domains after our handlers go. The next
            # send() then prunes it in _register_handlers, and two sends doing
            # that concurrently both remove it: the second raises
            # "list.remove(x): x not in list" from an unrelated tool. Measured
            # 2026-11-08: start_screencast after a heap snapshot failed exactly so.
            # This send runs _register_handlers alone, which prunes it once.
            try:
                await tab.send(cdp.heap_profiler.disable())
            except Exception:
                pass  # tab gone or already disabled: nothing left to clean

        target = resolve_artifact_path(
            file_path, default_prefix="zendriver-heap", default_ext="heapsnapshot"
        )
        data = "".join(chunks)
        target.write_text(data)
        return HeapSnapshotSaved(
            summary=f"Heap snapshot saved: {target} ({len(chunks)} chunks)",
            files=[publish(target, "application/json")],
            chunks=len(chunks),
            bytes=target.stat().st_size,
        )
