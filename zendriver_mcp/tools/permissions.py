"""Permission overrides for the browser context.

Pre-grant or deny the things Chrome usually asks about: geolocation, camera,
microphone, notifications, clipboard. Avoids agents getting stuck on a
"Do you want to allow..." prompt.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from zendriver import cdp

from zendriver_mcp.errors import Code, InvalidArgumentError, ZendriverMCPError
from zendriver_mcp.registry import Effect
from zendriver_mcp.response import Result
from zendriver_mcp.suggest import did_you_mean
from zendriver_mcp.tools.base import ToolBase

# The full enum has 30+ entries; these are the ones agents care about.
_COMMON_PERMISSIONS = {
    "geolocation": cdp.browser.PermissionType.GEOLOCATION,
    "notifications": cdp.browser.PermissionType.NOTIFICATIONS,
    "camera": cdp.browser.PermissionType.VIDEO_CAPTURE,
    "microphone": cdp.browser.PermissionType.AUDIO_CAPTURE,
    "clipboard-read-write": cdp.browser.PermissionType.CLIPBOARD_READ_WRITE,
    "midi": cdp.browser.PermissionType.MIDI,
    "midi-sysex": cdp.browser.PermissionType.MIDI_SYSEX,
    "background-sync": cdp.browser.PermissionType.BACKGROUND_SYNC,
    "storage-access": cdp.browser.PermissionType.STORAGE_ACCESS,
    "local-fonts": cdp.browser.PermissionType.LOCAL_FONTS,
}


class GrantResult(Result):
    granted: list[str] = Field(description="CDP permission names granted.")
    origin: str | None = Field(default=None, description="Origin granted to; null = all origins.")


class PermissionNames(Result):
    names: list[str] = Field(description="Short names grant_permissions accepts.")


def _connection(session):  # type: ignore[no-untyped-def]
    connection = session.browser.connection
    if connection is None:
        raise ZendriverMCPError(
            "Browser has no active connection.",
            code=Code.BROWSER_UNREACHABLE,
            hint="Call start_browser for a fresh browser.",
        )
    return connection


class PermissionsTools(ToolBase):
    """Grant or reset browser permissions for an origin."""

    GROUP = "permissions"

    def _register_tools(self) -> None:
        self._register(
            self.grant_permissions,
            Effect.WRITE,
            "Grant permissions",
            idempotent=True,
            page_scoped=False,
        )
        self._register(
            self.reset_permissions,
            Effect.WRITE,
            "Reset permissions",
            idempotent=True,
            page_scoped=False,
        )
        self._register(
            self.list_permission_names, Effect.READ, "List permission names", page_scoped=False
        )

    async def grant_permissions(
        self,
        permissions: Annotated[
            list[str],
            Field(
                description="Permission names from list_permission_names, or CDP enum names (VIDEO_CAPTURE / videoCapture). Example: ['geolocation', 'notifications']"
            ),
        ],
        origin: Annotated[
            str | None,
            Field(
                description="Origin to grant to, scheme and host. Omit for every origin. Example: 'https://example.com'"
            ),
        ] = None,
    ) -> GrantResult:
        """Pre-grant browser permissions so the page never shows a permission prompt.

        Browser-wide (the default browser context), not per tab. Undo with
        reset_permissions. Returns what was granted and to which origin.
        """
        resolved: list[cdp.browser.PermissionType] = []
        unknown: list[str] = []
        for name in permissions:
            lowered = name.strip().lower()
            if lowered in _COMMON_PERMISSIONS:
                resolved.append(_COMMON_PERMISSIONS[lowered])
                continue
            try:
                resolved.append(cdp.browser.PermissionType.from_json(name))
            except ValueError:
                unknown.append(name)

        if unknown:
            hints = [h for h in (did_you_mean(u, list(_COMMON_PERMISSIONS)) for u in unknown) if h]
            raise InvalidArgumentError(
                f"Unknown permissions: {unknown}",
                hint=" ".join(hints) or "See list_permission_names for accepted names.",
            )

        await _connection(self.session).send(
            cdp.browser.grant_permissions(permissions=resolved, origin=origin)
        )
        scope = origin or "all origins"
        return GrantResult(
            summary=f"Granted {len(resolved)} permission(s) to {scope}",
            granted=[r.value for r in resolved],
            origin=origin,
        )

    async def reset_permissions(self) -> Result:
        """Reset every permission override in the browser context to Chrome's defaults."""
        await _connection(self.session).send(cdp.browser.reset_permissions())
        return Result(summary="Permissions reset to defaults")

    async def list_permission_names(self) -> PermissionNames:
        """List the short permission names grant_permissions accepts."""
        names = sorted(_COMMON_PERMISSIONS.keys())
        return PermissionNames(summary="Permissions: " + ", ".join(names), names=names)
