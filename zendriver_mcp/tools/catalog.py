"""The fixed tool surface: core tools listed directly, the rest one search away.

``tools/list`` returns the same list to every client for the life of the
process (the 2026-07-28 spec forbids varying it per connection, and agents
cannot find tools that come and go). It holds:

* the CORE tools, enough for the ordinary browse loop with no search hop, each
  marked ``_meta["anthropic/alwaysLoad"]`` so Claude Code's tool search keeps
  them loaded;
* five meta-tools that reach everything else:
  ``search_tools`` (find by capability), ``describe_tool`` (full schema),
  ``call_tool`` (invoke one), ``batch`` (invoke several in order, feeding
  results forward), ``jobs`` (run one in the background and poll it).

Every non-core tool stays fully functional behind ``call_tool``: same schema
validation, same error model, same structured result. Set
``ZENDRIVER_MCP_SURFACE=full`` to list all of them directly instead.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from enum import Enum
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp.server.mcpserver import MCPServer as FastMCP
from mcp.server.mcpserver.tools.base import Tool
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, Field

from zendriver_mcp.errors import (
    Code,
    InvalidArgumentError,
    JobNotFoundError,
    UnknownToolError,
    ZendriverMCPError,
)
from zendriver_mcp.registry import SPECS
from zendriver_mcp.response import Result, error_result

ALWAYS_LOAD = {"anthropic/alwaysLoad": True}

# The browse loop: open, go, see, act, read, look. Everything a typical task
# needs before it needs anything specialised.
CORE = [
    "start_browser",
    "stop_browser",
    "get_browser_status",
    "navigate",
    "get_interaction_tree",
    "click",
    "type_text",
    "press_key",
    "get_text_content",
    "screenshot",
    "execute_javascript",
    "wait_for_element",
    "list_tabs",
    "scroll",
]

META = ["search_tools", "describe_tool", "call_tool", "batch", "jobs"]

MAX_BATCH_STEPS = 50
JOB_RETENTION_SECONDS = 3600
MAX_JOB_WAIT_SECONDS = 120

# "$2.tabs.0.id" as a whole argument value, or "${2.url}" inside a string.
_WHOLE_REF = re.compile(r"^\$(\d+)((?:\.[A-Za-z0-9_\-]+)*)$")
_INLINE_REF = re.compile(r"\$\{(\d+)((?:\.[A-Za-z0-9_\-]+)*)\}")


class Surface(Enum):
    CATALOG = "catalog"
    FULL = "full"


# ---------------------------------------------------------------------------
# result models


class ToolHit(BaseModel):
    name: str = Field(description="Tool name, for call_tool / describe_tool.")
    signature: str = Field(description="Parameters as 'a:type, b?:type' (? = optional).")
    summary: str = Field(description="First sentence of the tool's description.")
    group: str = Field(description="Capability group.")
    effect: str = Field(description="read, write or destructive.")
    listed: bool = Field(description="True if the tool is also listed directly.")


class SearchResult(Result):
    matches: list[ToolHit] = Field(description="Best matches first.")


class ToolDescription(Result):
    name: str = Field(description="The tool's exact name, for call_tool.")
    title: str = Field(description="Its human-readable title.")
    description: str = Field(description="What it does, when to use it, and what it returns.")
    input_schema: dict[str, Any] = Field(description="JSON Schema of the arguments.")
    output_schema: dict[str, Any] | None = Field(
        default=None, description="JSON Schema of the result."
    )
    annotations: dict[str, Any] = Field(description="readOnlyHint, destructiveHint, ...")


class StepOutcome(BaseModel):
    index: int = Field(description="Step number, as $N references use it.")
    tool: str = Field(description="The tool the step called.")
    ok: bool = Field(description="Whether the step succeeded.")
    result: dict[str, Any] | None = Field(default=None, description="The step's structured result.")
    text: str | None = Field(
        default=None, description="The step's text, when it had no structured result."
    )
    error: dict[str, Any] | None = Field(
        default=None, description="{error, code, hint} when it failed."
    )
    seconds: float = Field(description="How long the step took.")


class BatchResult(Result):
    steps: list[StepOutcome] = Field(description="Each step's outcome, in order.")
    completed: int = Field(description="Steps that succeeded.")
    failed_at: int | None = Field(
        default=None, description="Index of the step that stopped the batch."
    )


class JobState(Enum):
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobView(BaseModel):
    id: str = Field(description="Job id, for jobs get, wait and cancel.")
    tool: str = Field(description="The tool the job runs.")
    state: str = Field(description="working, completed, failed or cancelled.")
    progress: float | None = Field(
        default=None, description="Progress so far, in the tool's units."
    )
    total: float | None = Field(default=None, description="Progress at completion, when known.")
    message: str | None = Field(default=None, description="Latest progress message.")
    started: float = Field(description="Unix time the job started.")
    finished: float | None = Field(default=None, description="Unix time it finished.")
    result: dict[str, Any] | None = Field(
        default=None, description="Structured result, once completed."
    )
    text: str | None = Field(default=None, description="Text result, once completed.")
    error: dict[str, Any] | None = Field(
        default=None, description="{error, code, hint} if it failed."
    )


class JobsResult(Result):
    jobs: list[JobView] = Field(
        description="The affected jobs: id, state, progress, and result once finished."
    )


# ---------------------------------------------------------------------------
# helpers


def _first_sentence(desc: str) -> str:
    text = " ".join((desc or "").split())
    i = text.find(". ")
    return text[: i + 1] if i != -1 else text


def _param_sig(schema: dict[str, Any]) -> str:
    props = (schema or {}).get("properties", {}) or {}
    required = set((schema or {}).get("required", []) or [])
    parts = []
    for name, spec in props.items():
        typ: Any = spec.get("type")
        if typ is None and "anyOf" in spec:
            typ = "/".join(str(t.get("type", "any")) for t in spec["anyOf"] if isinstance(t, dict))
        if typ is None and "enum" in spec:
            typ = "|".join(map(str, spec["enum"]))
        mark = "" if name in required else "?"
        parts.append(f"{name}{mark}:{typ or 'any'}")
    return ", ".join(parts)


def _score(query: str, name: str, desc: str, group: str) -> int:
    """Dependency-free lexical relevance: name >> group > description."""
    q = query.lower().strip()
    terms = [w for w in re.split(r"[^a-z0-9]+", q) if w]
    if not terms:
        return 0
    name_l, desc_l, grp = name.lower(), (desc or "").lower(), (group or "").lower()
    name_words = set(name_l.split("_"))
    score = 0
    for term in terms:
        if term == name_l:
            score += 100
        if term in name_words:
            score += 40
        elif term in name_l:
            score += 25
        if term in grp:
            score += 15
        if term in desc_l:
            score += 5
        elif len(term) > 4 and term[:-1] in desc_l:  # crude plural/verb stem
            score += 3
    if q.replace(" ", "_") in name_l:
        score += 40
    if q in desc_l:
        score += 8
    return score


def _dig(value: Any, path: str, ref: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise InvalidArgumentError(
                f"Reference {ref} does not resolve: no '{part}' there.",
                hint="References read a previous step's structured result; check its fields "
                "with describe_tool (output_schema).",
            )
    return value


def resolve_refs(value: Any, results: list[dict[str, Any] | None]) -> Any:
    """Replace $N.path references with values from earlier steps' results."""
    if isinstance(value, dict):
        return {k: resolve_refs(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, results) for v in value]
    if not isinstance(value, str):
        return value

    def lookup(idx: str, path: str, ref: str) -> Any:
        i = int(idx)
        if i >= len(results):
            raise InvalidArgumentError(
                f"Reference {ref} points at step {i}, which has not run yet.",
                hint="A step can only refer to steps before it.",
            )
        return _dig(results[i], path, ref)

    whole = _WHOLE_REF.match(value)
    if whole:
        return lookup(whole.group(1), whole.group(2), value)

    def inline(m: re.Match[str]) -> str:
        found = lookup(m.group(1), m.group(2), m.group(0))
        return found if isinstance(found, str) else json.dumps(found)

    return _INLINE_REF.sub(inline, value)


def _outcome_parts(
    res: Any,
) -> tuple[bool, dict[str, Any] | None, str | None, dict[str, Any] | None]:
    """(ok, structured, text, error) from whatever a tool returned."""
    if isinstance(res, CallToolResult):
        text = "".join(getattr(c, "text", "") for c in res.content) or None
        if res.is_error:
            return False, None, None, res.structured_content or {"error": text, "code": "failed"}
        return True, res.structured_content, None if res.structured_content else text, None
    if isinstance(res, Result):
        return True, res.model_dump(mode="json", exclude_none=True), None, None
    if isinstance(res, BaseModel):
        return True, res.model_dump(mode="json"), None, None
    if isinstance(res, dict):
        return True, res, None, None
    return True, None, None if res is None else str(res), None


class _JobContext(Context):
    """A Context for a background job: progress goes into the job record."""

    def __init__(self, job: _Job, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_job", job)

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        job: _Job = object.__getattribute__(self, "_job")
        job.progress, job.total = progress, total
        if message:
            job.message = message


class _Job:
    def __init__(self, tool: str) -> None:
        self.id = "job_" + uuid.uuid4().hex[:10]
        self.tool = tool
        self.state = JobState.WORKING
        self.progress: float | None = None
        self.total: float | None = None
        self.message: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.result: dict[str, Any] | None = None
        self.text: str | None = None
        self.error: dict[str, Any] | None = None
        self.task: asyncio.Task[Any] | None = None
        self.done = asyncio.Event()

    def view(self) -> JobView:
        return JobView(
            id=self.id,
            tool=self.tool,
            state=self.state.value,
            progress=self.progress,
            total=self.total,
            message=self.message,
            started=self.started,
            finished=self.finished,
            result=self.result,
            text=self.text,
            error=self.error,
        )


def _guarded(fn: Any) -> Any:
    """The meta-tools' version of ToolBase._register's wrapper.

    Same contract as every other tool: failures become isError results with
    {error, code, hint} (the SDK would otherwise reduce an unexpected exception
    to "Error executing tool X"), and a Result becomes structured content plus
    text. Annotations are resolved here because this module uses postponed
    annotations and the SDK evaluates them against the wrapper's globals.
    """
    import functools
    import inspect
    import typing

    hints = typing.get_type_hints(fn, include_extras=True)
    sig = inspect.signature(fn, eval_str=True)

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            return error_result(exc)
        if isinstance(result, Result):
            return result.to_call_result()
        return result

    wrapper.__signature__ = sig  # type: ignore[attr-defined]
    wrapper.__annotations__ = hints
    if hasattr(wrapper, "__wrapped__"):
        delattr(wrapper, "__wrapped__")
    return wrapper


# ---------------------------------------------------------------------------
# installation


def install_catalog(mcp: FastMCP, surface: Surface) -> dict[str, Tool]:
    """Install the meta-tools and, for the catalogue surface, unlist non-core tools.

    Returns the catalogue: every tool reachable through call_tool.
    """
    tm = mcp._tool_manager
    catalog: dict[str, Tool] = dict(tm._tools)

    if surface is Surface.CATALOG:
        for name in list(tm._tools):
            if name not in CORE:
                tm.remove_tool(name)
    for name in CORE:
        tool = tm._tools.get(name)
        if tool is not None:
            tool.meta = {**(tool.meta or {}), **ALWAYS_LOAD}

    listed = set(tm._tools)
    jobs: dict[str, _Job] = {}

    async def invoke(name: str, arguments: dict[str, Any], ctx: Context | None) -> Any:
        if name in META:
            raise InvalidArgumentError(
                f"{name} cannot be called through call_tool, batch or jobs.",
                hint="Call it directly.",
            )
        tool = catalog.get(name)
        if tool is None:
            raise UnknownToolError(name, list(catalog))
        if ctx is None:
            ctx = Context(mcp_server=mcp)
        elif ctx._request_context is not None:
            ctx = ctx._nested_invocation()
        try:
            return await tool.run(arguments or {}, ctx)
        except ZendriverMCPError:
            raise
        except Exception as exc:
            # Tool.run wraps argument validation failures in ToolError; the
            # message names the bad fields, which is what the model needs.
            raise ZendriverMCPError(str(exc), code=Code.INVALID_ARGUMENT) from exc

    # -- search_tools -----------------------------------------------------

    async def search_tools(
        query: Annotated[
            str,
            Field(
                description="What you want to do, in plain words. Example: 'read cookies', 'block requests', 'set timezone', 'record video'"
            ),
        ],
        limit: Annotated[
            int, Field(description="Maximum matches to return, best first. Example: 8")
        ] = 8,
        detail: Annotated[
            Literal["names", "signatures", "full"],
            Field(
                description="'names' for names only, 'signatures' for names with parameters and a one-line summary, 'full' to also include each tool's JSON schema. Example: 'signatures'"
            ),
        ] = "signatures",
    ) -> SearchResult:
        """Find a browser tool by capability, among those not listed directly.

        This server lists only the core browse loop (start, navigate, read,
        click, type, screenshot, tabs). Everything else is found here: cookies,
        storage, network logs and interception, request blocking, headers,
        proxies, device and network emulation, geolocation, timezone, locale,
        permissions, accessibility, tracing, heap snapshots, Lighthouse,
        screencasts, Cloudflare challenges, security audits, human-like input,
        shadow DOM. Run a match with call_tool, several with batch, or a slow one
        with jobs.
        """
        ranked = []
        for name, tool in catalog.items():
            spec = SPECS.get(name)
            s = _score(query, name, tool.description or "", spec.group if spec else "")
            if s > 0:
                ranked.append((s, name, tool))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        top = ranked[: max(1, limit)]
        if not top:
            raise ZendriverMCPError(
                f"No tools matched '{query}'.",
                code=Code.UNKNOWN_TOOL,
                hint="Try broader words such as 'storage', 'network', 'input', 'emulate'.",
            )
        hits = []
        for _s, name, tool in top:
            spec = SPECS.get(name)
            hits.append(
                ToolHit(
                    name=name,
                    signature=_param_sig(tool.parameters),
                    summary=_first_sentence(tool.description or ""),
                    group=spec.group if spec else "",
                    effect=spec.effect.value if spec else "",
                    listed=name in listed,
                )
            )
        result = SearchResult(summary=f"{len(hits)} match(es) for '{query}'", matches=hits)
        if detail == "names":
            result.summary += ": " + ", ".join(h.name for h in hits)
        return result

    # -- describe_tool ----------------------------------------------------

    async def describe_tool(
        name: Annotated[
            str, Field(description="Exact tool name from search_tools. Example: 'write_cookie'")
        ],
    ) -> ToolDescription:
        """Show one tool's full description, argument schema, result schema and hints.

        Use when a search_tools line is not enough to call the tool correctly, or
        to learn which result fields a batch $N reference can read. Returns the
        description, input schema, output schema and annotations.
        """
        tool = catalog.get(name)
        if tool is None:
            raise UnknownToolError(name, list(catalog))
        ann = tool.annotations or ToolAnnotations()
        return ToolDescription(
            summary=f"{name}: {_first_sentence(tool.description or '')}",
            name=name,
            title=tool.title or name,
            description=tool.description or "",
            input_schema=tool.parameters,
            output_schema=tool.output_schema,
            annotations=ann.model_dump(by_alias=True, exclude_none=True),
        )

    # -- call_tool --------------------------------------------------------

    async def call_tool(
        name: Annotated[
            str,
            Field(description="Exact tool name from search_tools. Example: 'read_page_cookies'"),
        ],
        arguments: Annotated[
            dict[str, Any],
            Field(
                description='Arguments for that tool, matching its schema. {} for none. e.g. {"url": "https://example.com"}'
            ),
        ] = {},  # noqa: B006
        ctx: Context | None = None,
    ) -> Any:
        """Invoke any tool of this server by name, with the same validation and results as calling it directly.

        Find the name with search_tools (describe_tool for the exact schema).
        Failures come back as errors with a code and a hint, exactly as from the
        tool itself. Its read-only/destructive nature is the called tool's, shown
        in search_tools' `effect`. Returns exactly what the named tool returns.
        """
        return await invoke(name, arguments, ctx)

    # -- batch ------------------------------------------------------------

    async def batch(
        steps: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    'Steps to run in order, each {"tool": name, "arguments": {...}}. A string '
                    "argument '$N.path' is replaced by that field of step N's result "
                    "(0-based; list items by index, e.g. '$0.tabs.1.id'); '${N.path}' "
                    'substitutes inside a longer string. Example: [{"tool": "navigate", '
                    '"arguments": {"url": "https://example.com"}}, {"tool": '
                    '"get_text_content", "arguments": {}}]'
                ),
                max_length=MAX_BATCH_STEPS,
            ),
        ],
        stop_on_error: Annotated[
            bool,
            Field(
                description="Stop at the first failing step (default). false runs every step and reports each outcome. Example: true"
            ),
        ] = True,
        ctx: Context | None = None,
    ) -> BatchResult:
        """Run a batch of several tools in one call, in order, passing results forward.

        Saves a round trip per step for sequences you already know, such as
        navigate, fill a form, submit, read the result. Each step is validated
        when it runs, and nothing is rolled back: a browser cannot undo a click.
        At most 50 steps per call; refer to an earlier step's result as "$N.field".
        Returns every step's outcome; failed_at names the step that stopped it.
        """
        if not steps:
            raise InvalidArgumentError("batch needs at least one step.")
        if len(steps) > MAX_BATCH_STEPS:
            raise InvalidArgumentError(
                f"batch takes at most {MAX_BATCH_STEPS} steps, got {len(steps)}.",
                hint="Split it into several batches.",
            )
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
                raise InvalidArgumentError(
                    f"Step {i} must be an object with a 'tool' name.",
                    hint='Each step is {"tool": "name", "arguments": {...}}.',
                )
            if step["tool"] not in catalog:
                raise UnknownToolError(step["tool"], list(catalog))

        outcomes: list[StepOutcome] = []
        results: list[dict[str, Any] | None] = []
        failed_at: int | None = None
        for i, step in enumerate(steps):
            started = time.monotonic()
            try:
                args = resolve_refs(step.get("arguments") or {}, results)
                res = await invoke(step["tool"], args, ctx)
                ok, structured, text, error = _outcome_parts(res)
            except Exception as exc:
                failure = error_result(exc)
                ok, structured, text, error = False, None, None, failure.structured_content
            outcomes.append(
                StepOutcome(
                    index=i,
                    tool=step["tool"],
                    ok=ok,
                    result=structured,
                    text=text,
                    error=error,
                    seconds=round(time.monotonic() - started, 3),
                )
            )
            results.append(structured)
            if ctx is not None and ctx._request_context is not None:
                try:
                    await ctx.report_progress(i + 1, len(steps), f"step {i} {step['tool']}")
                except Exception:
                    pass
            if not ok and failed_at is None:
                failed_at = i
                if stop_on_error:
                    break

        completed = sum(1 for o in outcomes if o.ok)
        summary = f"{completed}/{len(steps)} step(s) succeeded"
        if failed_at is not None:
            err = outcomes[failed_at].error or {}
            summary += f"; step {failed_at} ({steps[failed_at]['tool']}) failed: {err.get('error')}"
        return BatchResult(
            summary=summary, steps=outcomes, completed=completed, failed_at=failed_at
        )

    # -- jobs -------------------------------------------------------------

    def _prune() -> None:
        cutoff = time.time() - JOB_RETENTION_SECONDS
        for jid in [j for j, job in jobs.items() if job.finished and job.finished < cutoff]:
            del jobs[jid]

    async def _run_job(job: _Job, name: str, arguments: dict[str, Any]) -> None:
        try:
            res = await invoke(name, arguments, _JobContext(job, mcp_server=mcp))
            ok, structured, text, error = _outcome_parts(res)
            job.result, job.text, job.error = structured, text, error
            job.state = JobState.COMPLETED if ok else JobState.FAILED
        except asyncio.CancelledError:
            job.state = JobState.CANCELLED
            raise
        except Exception as exc:
            job.error = error_result(exc).structured_content
            job.state = JobState.FAILED
        finally:
            job.finished = time.time()
            job.done.set()

    async def jobs_tool(
        action: Annotated[
            Literal["start", "get", "wait", "cancel", "list"],
            Field(
                description="'start' runs a tool in the background and returns its job id; 'get' reads a job; 'wait' blocks until it finishes or timeout; 'cancel' stops it; 'list' shows all. Example: 'start'"
            ),
        ],
        tool: Annotated[
            str | None,
            Field(description="For 'start': the tool to run. Example: 'run_lighthouse'"),
        ] = None,
        arguments: Annotated[
            dict[str, Any] | None,
            Field(
                description='For \'start\': its arguments. Example: {"url": "https://example.com"}'
            ),
        ] = None,
        job_id: Annotated[
            str | None,
            Field(
                description="For get/wait/cancel: the id 'start' returned. Example: 'job_3f2a9c01de'"
            ),
        ] = None,
        timeout: Annotated[
            float,
            Field(
                description=f"For 'wait': seconds to block, at most {MAX_JOB_WAIT_SECONDS}. Example: 30"
            ),
        ] = 30.0,
    ) -> JobsResult:
        """Run a slow tool in the background and follow it: start, then wait or get.

        For calls that can outlast a client's request timeout (Lighthouse audits,
        heap snapshots, screencast exports, long waits) or that you want to run
        while doing something else. Progress the tool reports is recorded on the
        job. Finished jobs are kept for an hour. Returns the affected jobs with
        their id, state, progress, and result once finished.
        """
        _prune()
        if action == "start":
            if not tool:
                raise InvalidArgumentError("jobs start needs `tool`.")
            if tool not in catalog:
                raise UnknownToolError(tool, list(catalog))
            if tool in META:
                raise InvalidArgumentError(f"{tool} cannot run as a job.")
            new = _Job(tool)
            jobs[new.id] = new
            new.task = asyncio.create_task(_run_job(new, tool, arguments or {}))
            return JobsResult(summary=f"Started {new.id} running {tool}", jobs=[new.view()])
        if action == "list":
            views = [j.view() for j in jobs.values()]
            return JobsResult(summary=f"{len(views)} job(s)", jobs=views)
        if not job_id:
            raise InvalidArgumentError(f"jobs {action} needs `job_id`.")
        job = jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        if action == "wait":
            limit = min(max(0.0, timeout), MAX_JOB_WAIT_SECONDS)
            try:
                await asyncio.wait_for(job.done.wait(), timeout=limit)
            except asyncio.TimeoutError:
                pass
        elif action == "cancel" and job.task is not None and not job.task.done():
            job.task.cancel()
            try:
                await job.task
            except (asyncio.CancelledError, Exception):
                pass
        v = job.view()
        return JobsResult(summary=f"{job.id} ({job.tool}): {v.state}", jobs=[v])

    jobs_tool.__name__ = "jobs"

    def ann(title: str, *, read_only: bool, destructive: bool | None = None) -> ToolAnnotations:
        return ToolAnnotations(
            title=title,
            read_only_hint=read_only,
            destructive_hint=None if read_only else destructive,
            open_world_hint=not read_only,
        )

    mcp.tool(
        title="Search tools", annotations=ann("Search tools", read_only=True), meta=ALWAYS_LOAD
    )(_guarded(search_tools))
    mcp.tool(
        title="Describe tool", annotations=ann("Describe tool", read_only=True), meta=ALWAYS_LOAD
    )(_guarded(describe_tool))
    # call_tool / batch / jobs can reach destructive tools, so they say so; the
    # real effect of each call is the called tool's (search_tools shows it).
    mcp.tool(
        title="Call tool",
        annotations=ann("Call tool", read_only=False, destructive=True),
        meta=ALWAYS_LOAD,
        structured_output=False,
    )(_guarded(call_tool))
    mcp.tool(
        title="Run steps",
        annotations=ann("Run steps", read_only=False, destructive=True),
        meta=ALWAYS_LOAD,
    )(_guarded(batch))
    mcp.tool(
        name="jobs",
        title="Background jobs",
        annotations=ann("Background jobs", read_only=False, destructive=True),
        meta=ALWAYS_LOAD,
    )(_guarded(jobs_tool))
    return catalog
