"""Cookie import/export via CDP (works on HTTP-only cookies, all domains).

``StorageTools.get_cookies`` covers the current page's cookies. These tools work
on the whole cookie jar through the browser-level ``Storage`` domain
(``Storage.getCookies`` / ``setCookies`` / ``clearCookies``), so they see every
site's cookies, including HttpOnly ones, and need no open page: a full
authenticated session can be saved and restored round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field
from zendriver import cdp
from zendriver.core.connection import Connection

from zendriver_mcp.artifacts import publish, resolve_artifact_path
from zendriver_mcp.dom import dom_for
from zendriver_mcp.errors import BrowserNotStartedError, InvalidArgumentError
from zendriver_mcp.registry import Effect
from zendriver_mcp.response import FileResult, Result
from zendriver_mcp.storage_restore import origin_of
from zendriver_mcp.tools.base import ToolBase
from zendriver_mcp.tools.storage import Cookie, to_cookie

# export_session's file format; import_session also reads export_cookies' array.
SESSION_FORMAT = 1


class AllCookies(Result):
    cookies: list[Cookie] = Field(description="Every cookie the browser holds, all sites.")
    domains: int = Field(description="Number of distinct domains.")


class CookieExport(FileResult):
    count: int = Field(description="Cookies written.")


class CookieImport(Result):
    imported: int = Field(description="Cookies set.")
    skipped: int = Field(description="File entries that were not valid cookies.")


class SessionExport(FileResult):
    cookies: int = Field(description="Cookies written.")
    origins: list[str] = Field(description="Origins whose localStorage was written.")


class SessionImport(Result):
    cookies: int = Field(description="Cookies set.")
    restored: list[str] = Field(description="Origins whose localStorage was written now.")
    on_next_load: list[str] = Field(
        description="Origins not open now: their localStorage is written when they next load."
    )


class CookieTools(ToolBase):
    """Full-fidelity cookie I/O, including HTTP-only cookies across all origins."""

    GROUP = "cookies"

    def _register_tools(self) -> None:
        self._register(
            self.export_cookies, Effect.DESTRUCTIVE, "Export cookies to file", page_scoped=False
        )
        self._register(self.import_cookies, Effect.WRITE, "Import cookies", page_scoped=False)
        self._register(
            self.export_session,
            Effect.DESTRUCTIVE,
            "Export session to file",
            page_scoped=False,
        )
        self._register(self.import_session, Effect.WRITE, "Import session", page_scoped=False)
        self._register(
            self.clear_all_cookies,
            Effect.DESTRUCTIVE,
            "Clear all cookies",
            idempotent=True,
            page_scoped=False,
        )
        self._register(self.list_all_cookies, Effect.READ, "List all cookies", page_scoped=False)

    def _conn(self) -> Connection:
        """The browser-level CDP connection: cookie-jar commands need no tab."""
        conn = self.session.browser.connection
        if conn is None:
            raise BrowserNotStartedError()
        return conn

    async def _jar(self) -> list[cdp.network.Cookie]:
        return await self._conn().send(cdp.storage.get_cookies()) or []

    async def list_all_cookies(self) -> AllCookies:
        """Return every cookie the browser is holding, across all sites.

        Includes HttpOnly cookies. For only the current page's cookies use
        get_cookies.
        """
        cookies = [to_cookie(c) for c in await self._jar()]
        domains = len({c.domain for c in cookies})
        return AllCookies(
            summary=f"{len(cookies)} cookie(s) across {domains} domain(s)",
            cookies=cookies,
            domains=domains,
        )

    async def export_cookies(
        self,
        file_path: Annotated[
            str,
            Field(
                description="Where to write the JSON file, under $HOME or the temp dir. Empty writes a timestamped file in the temp dir. Example: '/tmp/session-cookies.json'"
            ),
        ] = "",
    ) -> CookieExport:
        """Save every cookie, HttpOnly included, to a JSON file for import_cookies.

        The format is CDP's cookie list, which import_cookies (here or in
        another session) reads back. The file holds session secrets, so it is
        written owner-only (0600). The file is also served as a resource.
        """
        serialised = [c.to_json() for c in await self._jar()]
        target = resolve_artifact_path(
            file_path, default_prefix="zendriver-cookies", default_ext="json"
        )
        target.write_text(json.dumps(serialised, indent=2, sort_keys=True))
        # Cookies contain session secrets; restrict to owner-read/write.
        try:
            target.chmod(0o600)
        except OSError:
            pass  # best-effort on platforms without POSIX perms
        return CookieExport(
            summary=f"Exported {len(serialised)} cookies -> {target}",
            count=len(serialised),
            files=[publish(target, "application/json")],
        )

    async def import_cookies(
        self,
        file_path: Annotated[
            str,
            Field(
                description="JSON file written by export_cookies: an array of CDP cookies. Example: '/tmp/session-cookies.json'"
            ),
        ],
    ) -> CookieImport:
        """Load cookies from a JSON file produced by export_cookies.

        Entries that are not valid cookies are skipped and counted, so a partial
        restore still works. Existing cookies with the same name, domain and
        path are overwritten.
        """
        source = Path(file_path).expanduser().resolve()
        if not source.is_file():
            raise InvalidArgumentError(f"Cookie file not found: {source}")

        try:
            raw = json.loads(source.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidArgumentError(f"Cookie file is not readable JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise InvalidArgumentError("Cookie file must contain a JSON array")

        params: list[cdp.network.CookieParam] = []
        skipped = 0
        for entry in raw:
            try:
                params.append(cdp.network.CookieParam.from_json(entry))
            except (KeyError, ValueError, TypeError, AttributeError):
                skipped += 1

        if not params:
            raise InvalidArgumentError(
                "No valid cookies found in file",
                hint="Each entry needs at least name, value and domain (or url).",
            )

        await self._conn().send(cdp.storage.set_cookies(cookies=params))
        suffix = f" ({skipped} skipped)" if skipped else ""
        return CookieImport(
            summary=f"Imported {len(params)} cookies{suffix}", imported=len(params), skipped=skipped
        )

    async def export_session(
        self,
        file_path: Annotated[
            str,
            Field(
                description="Where to write the JSON file, under $HOME or the temp dir. Empty writes a timestamped file in the temp dir. Example: '/tmp/session.json'"
            ),
        ] = "",
    ) -> SessionExport:
        """Save the logged-in state, every cookie plus localStorage, for import_session.

        localStorage is saved for sites open in a tab or iframe now (Chrome
        exposes no others), so open those first. Written owner-only (0600).
        Returns the counts and the file.
        """
        cookies = [c.to_json() for c in await self._jar()]
        storage: dict[str, dict[str, str]] = {}
        for tab in self.session.tab_objects():
            try:
                frames = await dom_for(tab).frames()
            except Exception:  # a tab closing or navigating mid-export
                continue
            for frame in frames:
                origin = origin_of(frame.url)
                if origin is None or origin in storage:
                    continue
                try:
                    items = await frame.session.send(
                        cdp.dom_storage.get_dom_storage_items(
                            storage_id=cdp.dom_storage.StorageId(
                                is_local_storage=True, security_origin=origin
                            )
                        )
                    )
                except Exception:
                    continue
                if items:
                    storage[origin] = {k: v for k, v in items}
        target = resolve_artifact_path(
            file_path, default_prefix="zendriver-session", default_ext="json"
        )
        payload = {"version": SESSION_FORMAT, "cookies": cookies, "local_storage": storage}
        target.write_text(json.dumps(payload, indent=2, sort_keys=True))
        try:
            target.chmod(0o600)
        except OSError:
            pass  # best-effort on platforms without POSIX perms
        keys = sum(len(v) for v in storage.values())
        return SessionExport(
            summary=f"Exported {len(cookies)} cookies and {keys} localStorage item(s) "
            f"from {len(storage)} origin(s) -> {target}",
            cookies=len(cookies),
            origins=sorted(storage),
            files=[publish(target, "application/json")],
        )

    async def import_session(
        self,
        file_path: Annotated[
            str,
            Field(
                description="JSON file written by export_session (an export_cookies file is accepted too). Example: '/tmp/session.json'"
            ),
        ],
    ) -> SessionImport:
        """Restore a state saved by export_session: cookies and localStorage.

        localStorage goes in now for open sites, and for others on their next
        load, before their own scripts run. Returns what was restored now and
        what waits for a load.
        """
        source = Path(file_path).expanduser().resolve()
        if not source.is_file():
            raise InvalidArgumentError(f"Session file not found: {source}")
        try:
            raw = json.loads(source.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidArgumentError(f"Session file is not readable JSON: {exc}") from exc
        if isinstance(raw, list):
            raw = {"cookies": raw, "local_storage": {}}
        if not isinstance(raw, dict):
            raise InvalidArgumentError("Session file must be an export_session object.")

        params: list[cdp.network.CookieParam] = []
        for entry in raw.get("cookies") or []:
            try:
                params.append(cdp.network.CookieParam.from_json(entry))
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
        if params:
            await self._conn().send(cdp.storage.set_cookies(cookies=params))

        storage = raw.get("local_storage") or {}
        if not isinstance(storage, dict):
            raise InvalidArgumentError("local_storage must map origin -> {key: value}.")
        open_now: dict[str, Any] = {}
        for tab in self.session.tab_objects(visible_only=False):
            try:
                for frame in await dom_for(tab).frames():
                    origin = origin_of(frame.url)
                    if origin is not None:
                        open_now.setdefault(origin, frame.session)
            except Exception:
                continue
        restored: list[str] = []
        pending = self.session.pending_storage
        for origin, items in storage.items():
            if not isinstance(items, dict):
                continue
            values = {str(k): str(v) for k, v in items.items()}
            target = open_now.get(origin)
            if target is not None:
                sid = cdp.dom_storage.StorageId(is_local_storage=True, security_origin=origin)
                try:
                    for k, v in values.items():
                        await target.send(
                            cdp.dom_storage.set_dom_storage_item(storage_id=sid, key=k, value=v)
                        )
                    restored.append(origin)
                    continue
                except Exception:
                    pass  # fall through to restoring on its next load
            pending.pending[origin] = values
        await pending.install_all(self.session.tab_objects(visible_only=False))
        waiting = sorted(o for o in storage if o in pending.pending)
        return SessionImport(
            summary=f"Restored {len(params)} cookies; localStorage of {len(restored)} open "
            f"origin(s) now" + (f", {len(waiting)} on their next load" if waiting else ""),
            cookies=len(params),
            restored=sorted(restored),
            on_next_load=waiting,
        )

    async def clear_all_cookies(self) -> Result:
        """Delete every cookie in the browser, for every site.

        Logs every site out. To remove one site's state without touching others,
        use set_cookie with a past expiry, or clear_storage for Web Storage.
        """
        await self._conn().send(cdp.storage.clear_cookies())
        return Result(summary="All cookies cleared")
