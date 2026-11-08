"""Request interception: mock responses, fail requests.

Uses ``Fetch.enable`` + one persistent ``requestPaused`` handler per tab that
walks that tab's rules and acts on the first match. The first rule on a tab
enables interception there; removing its last rule disables it.

Unlike ``NetworkControlTools.block_urls`` (network-layer block, fires a
TCP error), this operates at the Fetch layer and can mock responses with
arbitrary status codes, headers, and bodies - handy for testing retry
logic, rate-limit handling, or offline paths.

Rules belong to the tab they were added on (the active tab, or the call's
``tab``). Each paused request is answered on the tab that paused it: an earlier
version answered on whatever tab was active, so a request paused in a
background tab got its continue sent to the wrong target and hung forever.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Literal

import zendriver as zd
from mcp.server.mcpserver import MCPServer as FastMCP
from pydantic import BaseModel, Field
from zendriver import cdp

from zendriver_mcp.errors import Code, InvalidArgumentError, ZendriverMCPError
from zendriver_mcp.registry import Effect, Reach
from zendriver_mcp.response import Result
from zendriver_mcp.suggest import did_you_mean
from zendriver_mcp.tools.base import ToolBase

PausedHandler = Callable[[cdp.fetch.RequestPaused], Awaitable[bool]]

# Mock bodies are held entirely in memory before base64-encoding and being
# sent over CDP. 10 MiB is comfortably larger than anything a typical API
# would return; larger responses suggest accidental misuse or an abuse
# attempt rather than a legitimate mock.
_MAX_MOCK_BODY_BYTES = 10 * 1024 * 1024

ErrorReason = Literal[
    "Failed",
    "Aborted",
    "TimedOut",
    "AccessDenied",
    "ConnectionClosed",
    "ConnectionReset",
    "ConnectionRefused",
    "ConnectionAborted",
    "ConnectionFailed",
    "NameNotResolved",
    "InternetDisconnected",
    "AddressUnreachable",
    "BlockedByClient",
    "BlockedByResponse",
]


@dataclass(slots=True)
class _Rule:
    id: str
    tab_id: str
    url_pattern: str  # fnmatch-style, e.g. "*/api/v1/*"
    action: str  # "mock" or "fail"
    status: int = 200
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    error_reason: str = "Failed"  # matches cdp.network.ErrorReason members
    match_count: int = 0


@dataclass(slots=True)
class _Hook:
    tab: zd.Tab
    handler: PausedHandler


class RuleInfo(BaseModel):
    id: str = Field(description="Rule id, for stop_interception.")
    tab: str = Field(description="Tab the rule applies to.")
    url_pattern: str = Field(description="Glob matched against the full request URL.")
    action: Literal["mock", "fail"] = Field(description="What a matching request gets.")
    status: int | None = Field(default=None, description="Mocked HTTP status (mock rules).")
    error_reason: str | None = Field(default=None, description="Network error (fail rules).")
    match_count: int = Field(description="Requests this rule has handled.")


class RuleAdded(Result):
    rule: RuleInfo = Field(description="The rule just added.")
    active_rules: int = Field(description="Rules active across all tabs.")


class RuleList(Result):
    rules: list[RuleInfo] = Field(description="Active rules, in match order per tab.")


class RulesRemoved(Result):
    removed: list[str] = Field(description="Ids of the rules removed.")
    active_rules: int = Field(description="Rules still active.")


class InterceptionTools(ToolBase):
    """Mock or fail outgoing requests matching URL patterns."""

    GROUP = "interception"

    def __init__(self, mcp: FastMCP) -> None:
        self._rules: list[_Rule] = []
        self._hooks: dict[str, _Hook] = {}  # tab id -> its Fetch handler
        self._lock = asyncio.Lock()
        self._next_id = 0
        # ToolBase.__init__ auto-registers the reset callback below.
        super().__init__(mcp)

    def _reset_state(self) -> None:
        self._rules = []
        self._hooks = {}
        self._next_id = 0

    def _register_tools(self) -> None:
        self._register(self.mock_response, Effect.WRITE, "Mock responses", reach=Reach.WEB)
        self._register(self.fail_requests, Effect.WRITE, "Fail requests", reach=Reach.WEB)
        self._register(
            self.list_interceptions, Effect.READ, "List interception rules", page_scoped=False
        )
        self._register(
            self.stop_interception,
            Effect.WRITE,
            "Remove interception rules",
            idempotent=True,
            page_scoped=False,
        )

    @staticmethod
    def _info(r: _Rule) -> RuleInfo:
        return RuleInfo(
            id=r.id,
            tab=r.tab_id,
            url_pattern=r.url_pattern,
            action="mock" if r.action == "mock" else "fail",
            status=r.status if r.action == "mock" else None,
            error_reason=r.error_reason if r.action == "fail" else None,
            match_count=r.match_count,
        )

    async def mock_response(
        self,
        url_pattern: Annotated[
            str,
            Field(
                description="Unix glob (* and ?) matched against the full request URL. Example: '*/api/v1/user*'"
            ),
        ],
        status: Annotated[int, Field(description="HTTP status to return. Example: 200")] = 200,
        body: Annotated[
            str,
            Field(
                description="Response body as UTF-8 text, at most 10 MiB. Example: '{\"ok\": true}'"
            ),
        ] = "",
        headers: Annotated[
            dict[str, str] | None,
            Field(
                description="Response headers. Omit for none. Example: {'Content-Type': 'application/json'}"
            ),
        ] = None,
    ) -> RuleAdded:
        """Answer every matching request from this tab with a mocked response.

        The request never reaches the network. Rules are checked in the order
        added and the first match wins. Returns the rule's id; pass it to
        stop_interception, or call that with no id to clear everything.
        """
        if not url_pattern:
            raise InvalidArgumentError("url_pattern must not be empty")
        if not 100 <= status <= 599:
            raise InvalidArgumentError(f"status must be an HTTP status (100-599), got {status}")
        if len(body.encode("utf-8")) > _MAX_MOCK_BODY_BYTES:
            raise InvalidArgumentError(
                f"mock body exceeds {_MAX_MOCK_BODY_BYTES // (1024 * 1024)} MiB cap"
            )
        rule = await self._add_rule(
            url_pattern=url_pattern,
            action="mock",
            status=status,
            body=body,
            headers=headers or {},
        )
        return RuleAdded(
            summary=f"Mocking {url_pattern} -> HTTP {status} on {rule.tab_id} ({rule.id})",
            rule=self._info(rule),
            active_rules=len(self._rules),
        )

    async def fail_requests(
        self,
        url_pattern: Annotated[
            str,
            Field(
                description="Unix glob (* and ?) matched against the full request URL. Example: '*/analytics/*'"
            ),
        ],
        error_reason: Annotated[
            ErrorReason,
            Field(description="Network error the request fails with. Example: 'TimedOut'"),
        ] = "Failed",
    ) -> RuleAdded:
        """Fail every matching request from this tab with a network error.

        Use to test how a page copes with an API being down or slow to fail.
        Rules are checked in the order added and the first match wins. Returns
        the rule's id for stop_interception.
        """
        if not url_pattern:
            raise InvalidArgumentError("url_pattern must not be empty")
        try:
            cdp.network.ErrorReason.from_json(error_reason)
        except ValueError as exc:
            raise InvalidArgumentError(f"Unknown error_reason: {error_reason}") from exc

        rule = await self._add_rule(
            url_pattern=url_pattern, action="fail", error_reason=error_reason
        )
        return RuleAdded(
            summary=f"Failing {url_pattern} with {error_reason} on {rule.tab_id} ({rule.id})",
            rule=self._info(rule),
            active_rules=len(self._rules),
        )

    async def list_interceptions(self) -> RuleList:
        """List the active interception rules on every tab, with match counts."""
        rules = [self._info(r) for r in self._rules]
        summary = f"{len(rules)} active rule(s)" if rules else "No interception rules"
        return RuleList(summary=summary, rules=rules)

    async def stop_interception(
        self,
        rule_id: Annotated[
            str | None,
            Field(
                description="Id of the one rule to remove. Omit to remove every rule on every tab. Example: 'rule_001'"
            ),
        ] = None,
    ) -> RulesRemoved:
        """Remove one interception rule by id, or all of them.

        A tab whose last rule is removed stops intercepting entirely, so its
        requests flow normally again.
        """
        async with self._lock:
            if rule_id is None:
                removed = [r.id for r in self._rules]
                self._rules.clear()
                for tab_id in list(self._hooks):
                    await self._disable(tab_id)
                return RulesRemoved(
                    summary=f"Cleared {len(removed)} interception rule(s)",
                    removed=removed,
                    active_rules=0,
                )

            for i, r in enumerate(self._rules):
                if r.id == rule_id:
                    self._rules.pop(i)
                    if not any(x.tab_id == r.tab_id for x in self._rules):
                        await self._disable(r.tab_id)
                    return RulesRemoved(
                        summary=f"Removed rule {rule_id}",
                        removed=[rule_id],
                        active_rules=len(self._rules),
                    )

        known = [r.id for r in self._rules]
        raise ZendriverMCPError(
            f"No rule with id {rule_id}",
            code=Code.INVALID_ARGUMENT,
            hint=did_you_mean(rule_id, known) or "List the active rules with list_interceptions.",
        )

    def _fresh_id(self) -> str:
        self._next_id += 1
        return f"rule_{self._next_id:03d}"

    async def _add_rule(self, **fields: object) -> _Rule:
        tab = self.session.page
        tab_id = self.session.tab_id_of(tab)
        if tab_id is None:
            raise ZendriverMCPError(
                "The tab is not tracked by this server.",
                hint="List tabs with list_tabs, or open one with new_tab.",
            )
        async with self._lock:
            rule = _Rule(id=self._fresh_id(), tab_id=tab_id, **fields)  # type: ignore[arg-type]
            if tab_id not in self._hooks:
                await self._enable(tab, tab_id)
            self._rules.append(rule)
        return rule

    async def _enable(self, tab: zd.Tab, tab_id: str) -> None:
        # The session owns the tab's Fetch domain (proxy auth needs it too);
        # we only register the interceptor it consults for each paused request.
        handler = self._make_handler(tab, tab_id)
        self._hooks[tab_id] = _Hook(tab, handler)
        try:
            await self.session.set_request_interceptor(tab, handler)
        except Exception:
            self._hooks.pop(tab_id, None)
            raise

    async def _disable(self, tab_id: str) -> None:
        hook = self._hooks.pop(tab_id, None)
        if hook is None:
            return
        try:
            await self.session.set_request_interceptor(hook.tab, None)
        except Exception:
            # Tab may have closed; Fetch state goes with it.
            pass

    def _make_handler(self, tab: zd.Tab, tab_id: str) -> PausedHandler:
        async def on_paused(event: cdp.fetch.RequestPaused) -> bool:
            url = event.request.url
            async with self._lock:
                match = next(
                    (
                        r
                        for r in self._rules
                        if r.tab_id == tab_id and fnmatch.fnmatch(url, r.url_pattern)
                    ),
                    None,
                )
                if match is not None:
                    match.match_count += 1

            # No match: the session continues it. A match is answered here, on
            # the tab that paused it; an unanswered paused request hangs forever.
            if match is None:
                return False
            try:
                if match.action == "fail":
                    await tab.send(
                        cdp.fetch.fail_request(
                            request_id=event.request_id,
                            error_reason=cdp.network.ErrorReason(match.error_reason),
                        )
                    )
                else:
                    encoded_body = base64.b64encode(match.body.encode("utf-8")).decode("ascii")
                    response_headers = [
                        cdp.fetch.HeaderEntry(name=name, value=value)
                        for name, value in match.headers.items()
                    ]
                    await tab.send(
                        cdp.fetch.fulfill_request(
                            request_id=event.request_id,
                            response_code=match.status,
                            response_headers=response_headers or None,
                            body=encoded_body,
                        )
                    )
            except Exception:
                pass  # tab closed or browser stopped mid-request
            return True

        return on_paused
