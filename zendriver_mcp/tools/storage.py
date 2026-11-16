# storage tools - the current page's cookies and Web Storage
from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field
from zendriver import cdp

from zendriver_mcp.errors import InvalidArgumentError, PageNotLoadedError
from zendriver_mcp.registry import Effect
from zendriver_mcp.response import Result
from zendriver_mcp.tools.base import ToolBase


class Cookie(BaseModel):
    name: str = Field(description="Cookie name.")
    value: str = Field(description="Cookie value.")
    domain: str = Field(description="Domain it is scoped to.")
    path: str = Field(description="Path it is scoped to.")
    expires: float | None = Field(
        default=None, description="Expiry as Unix seconds; omitted for a session cookie."
    )
    http_only: bool = Field(description="Hidden from page JavaScript.")
    secure: bool = Field(description="Sent over HTTPS only.")
    same_site: str | None = Field(default=None, description="Strict, Lax or None.")


class CookieList(Result):
    url: str = Field(description="The page URL the cookies apply to.")
    cookies: list[Cookie] = Field(description="Cookies the browser would send to that URL.")


class CookieSet(Result):
    cookie: Cookie = Field(description="The cookie as stored.")


class StorageItems(Result):
    origin: str = Field(description="Origin whose storage was read.")
    items: dict[str, str] = Field(description="Every key and value.")


class StorageWrite(Result):
    origin: str = Field(description="Origin written to.")
    key: str = Field(description="Key written.")


class StorageCleared(Result):
    origin: str = Field(description="Origin cleared.")
    local_keys: int = Field(description="localStorage keys removed.")
    session_keys: int = Field(description="sessionStorage keys removed.")


def to_cookie(c: Any) -> Cookie:
    """Map a CDP Network.Cookie to the result model."""
    session_cookie = bool(getattr(c, "session", False)) or (c.expires is None or c.expires <= 0)
    same_site = getattr(c, "same_site", None)
    return Cookie(
        name=c.name,
        value=c.value,
        domain=c.domain,
        path=c.path,
        expires=None if session_cookie else float(c.expires),
        http_only=bool(c.http_only),
        secure=bool(c.secure),
        same_site=same_site.value if same_site is not None else None,
    )


class StorageTools(ToolBase):
    """tools for the current page's cookies and Web Storage.

    Everything here goes through CDP or the isolated world (zendriver_mcp/dom.py), never
    page JavaScript: a page that hooks document.cookie or Storage.prototype to
    spot automation sees none of these reads and writes.
    """

    GROUP = "storage"

    def _register_tools(self) -> None:
        """register storage tools"""
        self._register(self.read_page_cookies, Effect.READ, "Page cookies")
        self._register(self.write_cookie, Effect.WRITE, "Write cookie", idempotent=True)
        self._register(self.read_local_storage, Effect.READ, "Read localStorage")
        self._register(
            self.write_local_storage, Effect.WRITE, "Write localStorage", idempotent=True
        )
        self._register(self.clear_storage, Effect.DESTRUCTIVE, "Clear Web Storage", idempotent=True)

    def _page_url(self) -> str:
        url = getattr(self.session.page, "url", None) or ""
        if not url.startswith(("http://", "https://")):
            raise PageNotLoadedError(
                f"The tab is on {url or 'no page'}, which has no cookies or storage origin."
            )
        return url

    async def read_page_cookies(self) -> CookieList:
        """Read the cookies that apply to the current page, HttpOnly ones included.

        Use to inspect session state after a login. Read through CDP, so
        HttpOnly cookies appear (document.cookie hides them) and the page cannot
        observe the read. For every cookie in the browser, across all sites,
        use list_all_cookies. Returns each cookie with domain, path, expiry and
        flags.
        """
        url = self._page_url()
        cookies = await self.session.page.send(cdp.network.get_cookies(urls=[url])) or []
        items = [to_cookie(c) for c in cookies]
        summary = f"{len(items)} cookie(s) for {url}" if items else f"(no cookies) for {url}"
        return CookieList(summary=summary, url=url, cookies=items)

    async def write_cookie(
        self,
        name: Annotated[str, Field(description="Cookie name. Example: 'session_id'")],
        value: Annotated[str, Field(description="Cookie value, unencoded. Example: 'abc123'")],
        domain: Annotated[
            str | None,
            Field(
                description="Domain to scope the cookie to; a leading dot covers subdomains. Omit to scope it to the current page's host. Example: '.example.com'"
            ),
        ] = None,
        path: Annotated[str, Field(description="Path to scope the cookie to. Example: '/'")] = "/",
        expires: Annotated[
            float | None,
            Field(
                description="Expiry as Unix seconds. Omit for a session cookie. Example: 1893456000"
            ),
        ] = None,
        secure: Annotated[bool, Field(description="Send over HTTPS only. Example: false")] = False,
        http_only: Annotated[
            bool, Field(description="Hide from page JavaScript. Example: false")
        ] = False,
        same_site: Annotated[
            Literal["Strict", "Lax", "None"] | None,
            Field(description="SameSite policy. Omit for the browser default. Example: 'Lax'"),
        ] = None,
    ) -> CookieSet:
        """Set one cookie for the current page's site, through CDP.

        Can set expiry, Secure, HttpOnly and SameSite, which document.cookie
        cannot. Overwrites a cookie with the same name, domain and path. Most
        sites read cookies on load, so reload_page if the change should take
        effect now. Returns the cookie as stored.
        """
        if not name:
            raise InvalidArgumentError("name must not be empty")
        url = self._page_url()
        ok = await self.session.page.send(
            cdp.network.set_cookie(
                name=name,
                value=value,
                url=None if domain else url,
                domain=domain,
                path=path,
                secure=secure,
                http_only=http_only,
                same_site=cdp.network.CookieSameSite(same_site) if same_site else None,
                expires=cdp.network.TimeSinceEpoch(expires) if expires else None,
            )
        )
        if ok is False:
            raise InvalidArgumentError(
                f"Chrome refused the cookie {name!r}",
                hint="A domain must match the page's site, and SameSite=None needs secure=true.",
            )
        stored = await self.session.page.send(cdp.network.get_cookies(urls=[url])) or []
        match = next(
            (
                c
                for c in stored
                if c.name == name and c.path == path and (domain is None or c.domain == domain)
            ),
            None,
        )
        cookie = (
            to_cookie(match)
            if match is not None
            else Cookie(
                name=name,
                value=value,
                domain=domain or "",
                path=path,
                expires=expires,
                http_only=http_only,
                secure=secure,
                same_site=same_site,
            )
        )
        return CookieSet(summary=f"Cookie set: {name}={value}", cookie=cookie)

    async def _origin(self) -> str:
        self._page_url()
        return str(await self.run_js("location.origin"))

    async def read_local_storage(self) -> StorageItems:
        """Read every localStorage key and value for the current page's origin.

        Use to inspect client-side app state such as auth tokens or feature
        flags. sessionStorage is not included. Read from an isolated world, so
        the page cannot see the read. Returns the origin and every key and value.
        """
        origin = await self._origin()
        items = await self.run_js(
            "(() => { const o = {}; for (let i = 0; i < localStorage.length; i++) {"
            " const k = localStorage.key(i); o[k] = localStorage.getItem(k); } return o; })()"
        )
        items = items or {}
        return StorageItems(
            summary=f"{len(items)} localStorage key(s) for {origin}", origin=origin, items=items
        )

    async def write_local_storage(
        self,
        key: Annotated[str, Field(description="localStorage key to write. Example: 'theme'")],
        value: Annotated[
            str,
            Field(
                description="Value to store. localStorage holds strings only; serialise objects to JSON yourself. Example: 'dark'"
            ),
        ],
    ) -> StorageWrite:
        """Write one localStorage item for the current page's origin.

        Overwrites any existing value under the same key. Most apps read
        localStorage at load, so reload_page after writing if the change should
        take effect. Written from an isolated world; the page's own scripts see
        the value but not the write. Returns the origin and the key written.
        """
        origin = await self._origin()
        await self.run_js(f"localStorage.setItem({json.dumps(key)}, {json.dumps(value)})")
        return StorageWrite(summary=f"localStorage set: {key}", origin=origin, key=key)

    async def clear_storage(self) -> StorageCleared:
        """Clear both localStorage and sessionStorage for the current origin.

        Use to reset client-side app state between runs. Cookies are NOT cleared —
        this usually will not log a session out on its own; use
        clear_all_cookies for that.
        Returns the origin and how many localStorage and sessionStorage keys were removed.
        """
        origin = await self._origin()
        counts = await self.run_js(
            "(() => { const r = [localStorage.length, sessionStorage.length];"
            " localStorage.clear(); sessionStorage.clear(); return r; })()"
        )
        local_keys, session_keys = (counts or [0, 0])[:2]
        return StorageCleared(
            summary=f"Cleared localStorage ({local_keys}) and sessionStorage ({session_keys}) for {origin}",
            origin=origin,
            local_keys=int(local_keys),
            session_keys=int(session_keys),
        )
