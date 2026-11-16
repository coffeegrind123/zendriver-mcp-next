"""Proxy configuration.

Chrome's proxy is set at launch via ``--proxy-server`` and cannot be changed
at runtime - even the DevTools Protocol has no primitive for it. So to switch
proxies we restart the browser. State that's tied to a user data directory
(cookies, localStorage, extensions) survives the restart; transient session
state doesn't.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from zendriver_mcp.errors import InvalidArgumentError
from zendriver_mcp.launch import HEADED_ONLY_NOTE
from zendriver_mcp.registry import Effect, Reach
from zendriver_mcp.response import Result
from zendriver_mcp.tools.base import ToolBase

_USER_DATA_DIR = Field(
    description="Chrome profile directory to relaunch with, so cookies and logins survive the restart. Omit to start fresh. Example: '/home/user/.cache/zendriver-profile'"
)
_HEADLESS = Field(
    description="Accepted and IGNORED: every launch is headed (headless is what bot detection looks for). Example: false"
)


class ProxyResult(Result):
    proxy: str | None = Field(default=None, description="Proxy in force; null = direct connection.")
    headless_ignored: bool = Field(description="True when headless was requested and refused.")


def _redact(url: str) -> str:
    """The proxy URL without its password, for results and logs."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.password is None:
        return url
    netloc = f"{parsed.username}:***@{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    return parsed._replace(netloc=netloc).geturl()


class ProxyTools(ToolBase):
    """Configure an HTTP/SOCKS proxy by restarting the browser."""

    GROUP = "proxy"

    def _register_tools(self) -> None:
        # Restart flow (stop + fresh launch) can easily exceed the default. A
        # restart closes every tab, hence DESTRUCTIVE.
        self._register(
            self.configure_proxy,
            Effect.DESTRUCTIVE,
            "Restart with proxy",
            reach=Reach.WEB,
            page_scoped=False,
            timeout=120,
        )
        self._register(
            self.clear_proxy,
            Effect.DESTRUCTIVE,
            "Restart without proxy",
            page_scoped=False,
            timeout=120,
        )

    async def configure_proxy(
        self,
        proxy_url: Annotated[
            str,
            Field(
                description="Proxy URL, optionally with credentials: http://host:port, socks5://host:port. Example: 'http://user:pass@10.0.0.1:8080'"
            ),
        ],
        user_data_dir: Annotated[str | None, _USER_DATA_DIR] = None,
        headless: Annotated[bool, _HEADLESS] = False,
    ) -> ProxyResult:
        """Restart the browser so it routes traffic through ``proxy_url``.

        Accepts ``http://host:port``, ``socks5://host:port``, or Chrome's
        full proxy rule syntax. Pass a ``user_data_dir`` to preserve the
        logged-in session across the restart - without it you start fresh.

        ``headless`` is accepted and ignored: the restart is headed like every
        other launch (zendriver_mcp/launch.py). A proxied session is the one most likely
        to be aimed at something that fingerprints the browser, so this is the
        last place a headless flag should quietly get through. Every open tab is
        closed by the restart. Returns the proxy in force (password redacted).
        """
        if not proxy_url:
            raise InvalidArgumentError(
                "proxy_url cannot be empty.",
                hint="Use clear_proxy to go back to a direct connection.",
            )
        session = self.session
        await session.stop()
        await session.start(headless=headless, user_data_dir=user_data_dir, proxy=proxy_url)
        override = f" — {HEADED_ONLY_NOTE}" if headless else ""
        shown = _redact(proxy_url)
        return ProxyResult(
            summary=f"Browser restarted with proxy: {shown}{override}",
            proxy=shown,
            headless_ignored=bool(headless),
        )

    async def clear_proxy(
        self,
        user_data_dir: Annotated[str | None, _USER_DATA_DIR] = None,
        headless: Annotated[bool, _HEADLESS] = False,
    ) -> ProxyResult:
        """Restart the browser without a proxy (a direct connection).

        Every open tab is closed by the restart. ``headless`` is accepted and
        ignored, as everywhere else.
        Returns the proxy now in force (none) once the browser has restarted.
        """
        session = self.session
        await session.stop()
        await session.start(headless=headless, user_data_dir=user_data_dir, proxy=None)
        override = f" — {HEADED_ONLY_NOTE}" if headless else ""
        return ProxyResult(
            summary=f"Browser restarted without proxy{override}",
            proxy=None,
            headless_ignored=bool(headless),
        )
