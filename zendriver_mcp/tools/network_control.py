"""Network-layer controls: URL blocking, extra headers, service worker bypass.

Complements ``LoggingTools`` (passive observation): these tools *change* what
the browser does on the wire. Each applies to one tab (CDP's Network domain is
per target): the active tab, or the one passed as ``tab``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from zendriver import cdp

from zendriver_mcp.errors import InvalidArgumentError
from zendriver_mcp.registry import Effect, Reach
from zendriver_mcp.response import Result
from zendriver_mcp.tools.base import ToolBase


class BlockList(Result):
    patterns: list[str] = Field(description="Patterns now blocked on the tab.")


class HeaderSet(Result):
    headers: list[str] = Field(description="Names of the headers now added to every request.")


class ServiceWorkerBypass(Result):
    bypass: bool = Field(description="Whether service workers are now bypassed.")


class NetworkControlTools(ToolBase):
    """Active network controls that modify outgoing requests."""

    GROUP = "network"

    def _register_tools(self) -> None:
        self._register(
            self.block_urls, Effect.WRITE, "Block URLs", reach=Reach.WEB, idempotent=True
        )
        self._register(
            self.unblock_all_urls,
            Effect.WRITE,
            "Unblock all URLs",
            reach=Reach.WEB,
            idempotent=True,
        )
        self._register(
            self.set_extra_headers,
            Effect.WRITE,
            "Set extra headers",
            reach=Reach.WEB,
            idempotent=True,
        )
        self._register(
            self.bypass_service_worker,
            Effect.WRITE,
            "Bypass service worker",
            reach=Reach.WEB,
            idempotent=True,
        )

    async def block_urls(
        self,
        patterns: Annotated[
            list[str],
            Field(
                description="URL patterns in Chrome's wildcard syntax; replaces any earlier list. Empty clears. Example: ['*googletagmanager.com*', '*.png']"
            ),
        ],
    ) -> BlockList:
        """Block this tab's future requests whose URL matches any pattern.

        Blocked requests fail at the network layer (net::ERR_BLOCKED_BY_CLIENT).
        The list replaces the previous one; pass [] or call unblock_all_urls to
        remove all blocks. For mocked responses or chosen error codes use
        mock_response / fail_requests instead.
        """
        if any(not isinstance(p, str) or not p for p in patterns):
            raise InvalidArgumentError("patterns must be non-empty strings")
        await self.session.page.send(cdp.network.set_blocked_ur_ls(urls=patterns))
        summary = (
            f"Blocking {len(patterns)} URL pattern(s)" if patterns else "URL block-list cleared"
        )
        return BlockList(summary=summary, patterns=patterns)

    async def unblock_all_urls(self) -> BlockList:
        """Remove every URL-pattern block on this tab."""
        await self.session.page.send(cdp.network.set_blocked_ur_ls(urls=[]))
        return BlockList(summary="URL block-list cleared", patterns=[])

    async def set_extra_headers(
        self,
        headers: Annotated[
            dict[str, str],
            Field(
                description="Header names and values added to every request from this tab; replaces any earlier set. Empty clears. Example: {'X-Tenant': 'acme'}"
            ),
        ],
    ) -> HeaderSet:
        """Add headers to every outgoing request from this tab.

        Useful for API keys, tenant overrides, tracing ids. Replaces the
        previous set; pass {} to clear. Note that unusual headers are visible to
        the sites you visit.
        """
        if any(not k for k in headers):
            raise InvalidArgumentError("header names must not be empty")
        await self.session.page.send(
            cdp.network.set_extra_http_headers(headers=cdp.network.Headers(headers))
        )
        names = sorted(headers)
        summary = f"Extra headers set: {names}" if headers else "Extra headers cleared"
        return HeaderSet(summary=summary, headers=names)

    async def bypass_service_worker(
        self,
        bypass: Annotated[
            bool,
            Field(description="True to skip service workers, false to restore them. Example: true"),
        ] = True,
    ) -> ServiceWorkerBypass:
        """Make this tab skip its service worker and always go to the network.

        Use when a cached service worker hides the real responses you want to
        observe or intercept.
        """
        await self.session.page.send(cdp.network.set_bypass_service_worker(bypass=bypass))
        return ServiceWorkerBypass(summary=f"Service worker bypass: {bypass}", bypass=bypass)
