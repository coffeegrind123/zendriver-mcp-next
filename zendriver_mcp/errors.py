"""Errors a tool raises on purpose, each with a stable code and a recovery hint.

Every tool failure reaches the client as an ``isError`` result whose
``structuredContent`` is ``{"error", "code", "hint"}`` and whose text is the
message followed by the hint. The code is for programs (``batch`` stops on it,
tests assert on it); the hint is for the model, and says what to do next rather
than restating what went wrong. See ``zendriver_mcp/response.py`` for the conversion.

Codes are part of the public contract: rename one and every client that branches
on it breaks. Add new ones instead.
"""

from __future__ import annotations

from enum import Enum


class Code(str, Enum):
    """Stable error codes, carried in ``structuredContent.code``."""

    INVALID_ARGUMENT = "invalid_argument"
    BROWSER_NOT_STARTED = "browser_not_started"
    PAGE_NOT_LOADED = "page_not_loaded"
    ELEMENT_NOT_FOUND = "element_not_found"
    ELEMENT_HIDDEN = "element_hidden"
    ELEMENT_OBSCURED = "element_obscured"
    STALE_ID = "stale_id"
    TAB_NOT_FOUND = "tab_not_found"
    TAB_FORBIDDEN = "tab_forbidden"
    UNKNOWN_TOOL = "unknown_tool"
    TIMEOUT = "timeout"
    OPERATION_TIMEOUT = "operation_timeout"
    BROWSER_UNREACHABLE = "browser_unreachable"
    LAUNCH_FAILED = "launch_failed"
    CHALLENGE_UNSOLVED = "challenge_unsolved"
    CONFLICT = "conflict"
    JS_ERROR = "js_error"
    NOT_INSTALLED = "not_installed"
    JOB_NOT_FOUND = "job_not_found"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERNAL = "internal"


class ZendriverMCPError(Exception):
    """Base for every deliberate tool failure.

    ``code`` and ``hint`` default per subclass; a raise site can override either
    when it knows something more specific.
    """

    code: Code = Code.FAILED
    hint: str | None = None

    def __init__(
        self,
        message: str,
        *,
        code: Code | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if hint is not None:
            self.hint = hint


class InvalidArgumentError(ZendriverMCPError):
    """The caller's arguments are wrong in a way the schema cannot express."""

    code = Code.INVALID_ARGUMENT


class BrowserNotStartedError(ZendriverMCPError):
    code = Code.BROWSER_NOT_STARTED
    hint = "Call start_browser, then retry."

    def __init__(self, message: str = "Browser not started.") -> None:
        super().__init__(message)


class PageNotLoadedError(ZendriverMCPError):
    code = Code.PAGE_NOT_LOADED
    hint = "Call navigate with a URL first."

    def __init__(self, message: str = "No page loaded.") -> None:
        super().__init__(message)


class ElementNotFoundError(ZendriverMCPError):
    code = Code.ELEMENT_NOT_FOUND
    hint = (
        "Check the selector with find_element (it lists what does exist), or take "
        "ids from get_interaction_tree."
    )

    def __init__(self, selector: str, *, hint: str | None = None) -> None:
        super().__init__(f"Element not found: {selector}", hint=hint)
        self.selector = selector


class ElementHiddenError(ZendriverMCPError):
    code = Code.ELEMENT_HIDDEN
    hint = "Scroll it into view with scroll_to_element, or open whatever reveals it first."

    def __init__(self, selector: str) -> None:
        super().__init__(f"Element is hidden: {selector}")
        self.selector = selector


class StaleIdError(ZendriverMCPError):
    """A numeric id from get_interaction_tree no longer names an element."""

    code = Code.STALE_ID
    hint = (
        "Ids last only until the page navigates or re-renders. Run "
        "get_interaction_tree again and use the new id."
    )

    def __init__(self, element_id: str) -> None:
        super().__init__(f"Element id {element_id} is not on the current page.")
        self.element_id = element_id


class TabNotFoundError(ZendriverMCPError):
    code = Code.TAB_NOT_FOUND

    def __init__(self, tab_id: str, known: list[str]) -> None:
        from zendriver_mcp.suggest import did_you_mean

        hint = did_you_mean(tab_id, known) or "List the open tabs with list_tabs."
        super().__init__(f"Tab not found: {tab_id}", hint=hint)
        self.tab_id = tab_id


class TabForbiddenError(ZendriverMCPError):
    """The tab belongs to another authenticated client of a shared server."""

    code = Code.TAB_FORBIDDEN
    hint = "Use a tab this client opened (list_tabs shows only those), or open one with new_tab."

    def __init__(self, tab_id: str) -> None:
        super().__init__(f"Tab {tab_id} belongs to another client.")


class ToolTimeoutError(ZendriverMCPError):
    """The tool ran past its whole time budget (a hung CDP call, usually)."""

    code = Code.TIMEOUT
    hint = (
        "Check get_browser_status; if the page is busy, retry. A browser that "
        "stopped answering is replaced automatically on the next call."
    )

    def __init__(self, tool: str, timeout: float) -> None:
        super().__init__(f"Tool '{tool}' exceeded its {timeout:.0f}s time budget")
        self.tool = tool
        self.timeout = timeout


class BrowserUnreachableError(ZendriverMCPError):
    """The budget ran out AND Chrome no longer answers CDP: alive but wedged.

    Distinct from ToolTimeoutError, which says only "this call was slow" and is
    what a 22-hour outage reported instead.
    """

    code = Code.BROWSER_UNREACHABLE

    def __init__(self, tool: str, timeout: float, will_autostart: bool) -> None:
        nxt = (
            "The next call starts a fresh browser."
            if will_autostart
            else "Call start_browser for a fresh one."
        )
        super().__init__(
            f"Tool '{tool}' hit its {timeout:.0f}s budget and Chrome stopped answering "
            "CDP. The process was still alive but wedged, so the dead session has "
            "been discarded.",
            hint=nxt,
        )
        self.tool = tool
        self.timeout = timeout


class CloudflareChallengeError(ZendriverMCPError):
    code = Code.CHALLENGE_UNSOLVED
    hint = "Take a screenshot to see the challenge, then retry bypass_cloudflare with a longer timeout."

    def __init__(self, message: str = "Could not solve the Cloudflare challenge in time.") -> None:
        super().__init__(message)


class TracingError(ZendriverMCPError):
    """Unexpected state transitions around CDP Tracing.* commands."""

    code = Code.CONFLICT


class LighthouseNotInstalledError(ZendriverMCPError):
    code = Code.NOT_INSTALLED
    hint = "Install it with `npm i -g lighthouse`, or check with check_lighthouse_available."

    def __init__(self) -> None:
        super().__init__("Lighthouse CLI not found.")


class AccessibilityUidError(ZendriverMCPError):
    """An unknown or stale accessibility uid."""

    code = Code.STALE_ID
    hint = "Take a fresh get_accessibility_snapshot and use a uid from it."


class UnknownToolError(ZendriverMCPError):
    code = Code.UNKNOWN_TOOL

    def __init__(self, name: str, known: list[str]) -> None:
        from zendriver_mcp.suggest import did_you_mean

        hint = did_you_mean(name, known) or "Find the right name with search_tools."
        super().__init__(f"Unknown tool: {name}", hint=hint)


class JobNotFoundError(ZendriverMCPError):
    code = Code.JOB_NOT_FOUND
    hint = "List current jobs with jobs(action='list'). Finished jobs are kept for an hour."

    def __init__(self, job_id: str) -> None:
        super().__init__(f"Job not found: {job_id}")


class BrowserLaunchError(ZendriverMCPError):
    """Chrome was asked to start, tried, and died, with Chrome's own reason.

    Distinct from ``BrowserNotStartedError`` ("nobody called start_browser")
    and from ``ToolTimeoutError`` ("the tool ran too long"). Those two used to
    absorb every launch failure between them, which is how a dead ``$DISPLAY``
    reached a caller as a bare transport timeout: the one line that named the
    cause went to a log file and the exception guessed at something else.
    """

    code = Code.LAUNCH_FAILED

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr
