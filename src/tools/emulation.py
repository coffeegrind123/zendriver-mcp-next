"""Emulation tools: viewport, device profiles, CPU + network throttling.

All state is per-tab and resets on tab close. Pass `0` widths/heights or
``restore_*`` tools to revert to the real device.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from zendriver import cdp

from src.errors import InvalidArgumentError
from src.registry import Effect
from src.response import Result
from src.suggest import did_you_mean
from src.tools.base import ToolBase


@dataclass(frozen=True)
class DeviceProfile:
    """Minimal device emulation profile."""

    name: str
    width: int
    height: int
    device_scale_factor: float
    mobile: bool
    user_agent: str


# Small but representative set - matches DevTools' preset dropdown closely.
DEVICE_PROFILES: dict[str, DeviceProfile] = {
    "iphone-15-pro": DeviceProfile(
        name="iPhone 15 Pro",
        width=393,
        height=852,
        device_scale_factor=3.0,
        mobile=True,
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
    ),
    "pixel-8": DeviceProfile(
        name="Pixel 8",
        width=412,
        height=915,
        device_scale_factor=2.625,
        mobile=True,
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Mobile Safari/537.36"
        ),
    ),
    "ipad-pro": DeviceProfile(
        name="iPad Pro 12.9",
        width=1024,
        height=1366,
        device_scale_factor=2.0,
        mobile=True,
        user_agent=(
            "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
    ),
    "desktop-1080p": DeviceProfile(
        name="Desktop 1080p",
        width=1920,
        height=1080,
        device_scale_factor=1.0,
        mobile=False,
        user_agent="",  # empty => don't override
    ),
}


# Matches the throttling presets DevTools exposes.
NETWORK_PROFILES: dict[str, dict[str, float]] = {
    # latency ms, down Bps, up Bps
    "offline": {"offline": 1, "latency": 0, "download": 0, "upload": 0},
    "slow-3g": {
        "offline": 0,
        "latency": 400,
        "download": 400 * 1024 / 8,
        "upload": 400 * 1024 / 8,
    },
    "fast-3g": {
        "offline": 0,
        "latency": 150,
        "download": 1.5 * 1024 * 1024 / 8,
        "upload": 750 * 1024 / 8,
    },
    "4g": {
        "offline": 0,
        "latency": 20,
        "download": 9 * 1024 * 1024 / 8,
        "upload": 1.5 * 1024 * 1024 / 8,
    },
    "no-throttling": {"offline": 0, "latency": 0, "download": -1, "upload": -1},
}


class ViewportResult(Result):
    width: int = Field(description="Viewport width in CSS pixels (0 = real device).")
    height: int = Field(description="Viewport height in CSS pixels (0 = real device).")
    device_scale_factor: float = Field(description="Device pixel ratio (0 = real device).")
    mobile: bool = Field(description="Whether mobile viewport behaviour is on.")


class DeviceResult(ViewportResult):
    profile: str = Field(description="Profile key applied.")
    name: str = Field(description="Human-readable device name.")
    user_agent: str | None = Field(
        default=None, description="User-Agent applied, or null when the profile keeps the real one."
    )
    touch: bool = Field(description="Whether touch emulation is on.")


class DeviceInfo(BaseModel):
    key: str = Field(description="Profile key, as set_device takes it.")
    name: str = Field(description="Device name.")
    width: int
    height: int
    device_scale_factor: float
    mobile: bool


class DeviceList(Result):
    devices: list[DeviceInfo] = Field(description="Available device profiles.")


class ThrottleResult(Result):
    rate: float = Field(description="CPU slowdown multiplier in force (1 = none).")


class NetworkProfile(BaseModel):
    name: str = Field(description="Profile name, as set_network_conditions takes it.")
    offline: bool
    latency: float = Field(description="Added round-trip latency in ms.")
    download: float = Field(description="Download throughput in bytes/s (-1 = unthrottled).")
    upload: float = Field(description="Upload throughput in bytes/s (-1 = unthrottled).")


class NetworkResult(Result):
    profile: NetworkProfile = Field(description="The profile applied.")


class NetworkProfileList(Result):
    profiles: list[NetworkProfile] = Field(description="Available network presets.")


class MediaResult(Result):
    # Not `media`: that name is Result.media(), the content-block hook.
    media_type: str | None = Field(
        default=None, description="Emulated CSS media type; null = default."
    )
    prefers_color_scheme: str | None = Field(
        default=None, description="Emulated prefers-color-scheme; null = default."
    )


def _profile_model(name: str, p: dict[str, float]) -> NetworkProfile:
    return NetworkProfile(
        name=name,
        offline=bool(p["offline"]),
        latency=p["latency"],
        download=p["download"],
        upload=p["upload"],
    )


class EmulationTools(ToolBase):
    """Viewport, device, CPU, and network emulation via CDP."""

    GROUP = "emulation"

    def _register_tools(self) -> None:
        self._register(self.set_viewport, Effect.WRITE, "Set viewport", idempotent=True)
        self._register(self.restore_viewport, Effect.WRITE, "Restore viewport", idempotent=True)
        self._register(self.set_device, Effect.WRITE, "Emulate device", idempotent=True)
        self._register(self.list_devices, Effect.READ, "List device profiles", page_scoped=False)
        self._register(self.set_cpu_throttle, Effect.WRITE, "Throttle CPU", idempotent=True)
        self._register(
            self.set_network_conditions, Effect.WRITE, "Throttle network", idempotent=True
        )
        self._register(
            self.list_network_profiles, Effect.READ, "List network profiles", page_scoped=False
        )
        self._register(self.emulate_media, Effect.WRITE, "Emulate CSS media", idempotent=True)

    async def set_viewport(
        self,
        width: Annotated[int, Field(description="Viewport width in CSS pixels. Example: 1280")],
        height: Annotated[int, Field(description="Viewport height in CSS pixels. Example: 800")],
        device_scale_factor: Annotated[
            float,
            Field(description="Device pixel ratio: 1.0 standard, 2.0 Retina. Example: 1.0"),
        ] = 1.0,
        mobile: Annotated[
            bool,
            Field(
                description="Emulate mobile viewport behaviour (meta viewport, overlay scrollbars). Example: false"
            ),
        ] = False,
    ) -> ViewportResult:
        """Override the visible viewport dimensions of the tab.

        Lasts until restore_viewport or the tab closes. For a whole device
        (size, DPR, touch, User-Agent) use set_device instead. Returns the
        metrics now in force.
        """
        if width < 0 or height < 0:
            raise InvalidArgumentError("width and height must be >= 0")
        await self.session.page.send(
            cdp.emulation.set_device_metrics_override(
                width=width,
                height=height,
                device_scale_factor=device_scale_factor,
                mobile=mobile,
            )
        )
        return ViewportResult(
            summary=f"Viewport: {width}x{height}@{device_scale_factor}x (mobile={mobile})",
            width=width,
            height=height,
            device_scale_factor=device_scale_factor,
            mobile=mobile,
        )

    async def restore_viewport(self) -> ViewportResult:
        """Restore the real device's viewport by clearing the override.

        Undoes set_viewport and the size part of set_device. Returns zeros,
        which CDP reads as "use the real device".
        """
        await self.session.page.send(cdp.emulation.clear_device_metrics_override())
        return ViewportResult(
            summary="Viewport override cleared",
            width=0,
            height=0,
            device_scale_factor=0.0,
            mobile=False,
        )

    async def set_device(
        self,
        profile: Annotated[
            str,
            Field(description="Device profile key from list_devices. Example: 'iphone-15-pro'"),
        ],
    ) -> DeviceResult:
        """Emulate a preset device: viewport, pixel ratio, touch, and User-Agent.

        Profiles: iphone-15-pro, pixel-8, ipad-pro, desktop-1080p (list_devices
        has the details). Sets a matching User-Agent when the profile has one.
        Returns everything applied.
        """
        key = profile.lower().strip()
        if key not in DEVICE_PROFILES:
            raise InvalidArgumentError(
                f"Unknown device profile: {profile}",
                hint=did_you_mean(key, list(DEVICE_PROFILES))
                or f"Available: {', '.join(sorted(DEVICE_PROFILES))}.",
            )
        p = DEVICE_PROFILES[key]
        await self.session.page.send(
            cdp.emulation.set_device_metrics_override(
                width=p.width,
                height=p.height,
                device_scale_factor=p.device_scale_factor,
                mobile=p.mobile,
            )
        )
        if p.user_agent:
            await self.session.page.send(
                cdp.network.set_user_agent_override(user_agent=p.user_agent)
            )
        await self.session.page.send(cdp.emulation.set_touch_emulation_enabled(enabled=p.mobile))
        return DeviceResult(
            summary=f"Emulating {p.name}: {p.width}x{p.height}@{p.device_scale_factor}x",
            profile=key,
            name=p.name,
            width=p.width,
            height=p.height,
            device_scale_factor=p.device_scale_factor,
            mobile=p.mobile,
            user_agent=p.user_agent or None,
            touch=p.mobile,
        )

    async def list_devices(self) -> DeviceList:
        """List the device profiles set_device accepts, with their metrics."""
        devices = [
            DeviceInfo(
                key=k,
                name=p.name,
                width=p.width,
                height=p.height,
                device_scale_factor=p.device_scale_factor,
                mobile=p.mobile,
            )
            for k, p in sorted(DEVICE_PROFILES.items())
        ]
        return DeviceList(
            summary="Device profiles: " + ", ".join(d.key for d in devices), devices=devices
        )

    async def set_cpu_throttle(
        self,
        rate: Annotated[
            float,
            Field(
                description="Slowdown multiplier: 1.0 disables throttling, 4.0 is DevTools' 4x, 20.0 approximates a low-end phone. Example: 4.0"
            ),
        ] = 1.0,
    ) -> ThrottleResult:
        """Slow the tab's CPU by a multiplier, to test performance on weak devices.

        Pass 1.0 to remove throttling. Returns the rate in force.
        """
        if rate < 1.0:
            raise InvalidArgumentError(
                "rate must be >= 1.0", hint="Pass 1.0 to disable throttling."
            )
        await self.session.page.send(cdp.emulation.set_cpu_throttling_rate(rate=rate))
        return ThrottleResult(summary=f"CPU throttle: {rate}x", rate=rate)

    async def set_network_conditions(
        self,
        profile: Annotated[
            str,
            Field(
                description="Preset name: offline, slow-3g, fast-3g, 4g, or no-throttling. Example: 'slow-3g'"
            ),
        ],
    ) -> NetworkResult:
        """Apply a named network-throttling preset to the tab.

        no-throttling removes it. list_network_profiles has the numbers behind
        each preset. Returns the profile applied.
        """
        key = profile.lower().strip()
        if key not in NETWORK_PROFILES:
            raise InvalidArgumentError(
                f"Unknown network profile: {profile}",
                hint=did_you_mean(key, list(NETWORK_PROFILES))
                or f"Available: {', '.join(NETWORK_PROFILES)}.",
            )
        p = NETWORK_PROFILES[key]
        await self.session.page.send(
            cdp.network.emulate_network_conditions(
                offline=bool(p["offline"]),
                latency=p["latency"],
                download_throughput=p["download"],
                upload_throughput=p["upload"],
            )
        )
        return NetworkResult(summary=f"Network profile: {key}", profile=_profile_model(key, p))

    async def list_network_profiles(self) -> NetworkProfileList:
        """List the network presets set_network_conditions accepts, with their numbers."""
        profiles = [_profile_model(k, p) for k, p in NETWORK_PROFILES.items()]
        return NetworkProfileList(
            summary="Network profiles: " + ", ".join(p.name for p in profiles), profiles=profiles
        )

    async def emulate_media(
        self,
        media: Annotated[
            Literal["", "screen", "print"],
            Field(
                description="CSS media type to emulate; '' restores the default. Example: 'print'"
            ),
        ] = "",
        prefers_color_scheme: Annotated[
            Literal["", "light", "dark"],
            Field(
                description="prefers-color-scheme to emulate; '' restores the default. Example: 'dark'"
            ),
        ] = "",
    ) -> MediaResult:
        """Force a CSS media type and/or prefers-color-scheme on the tab.

        Use to check print styles or dark mode. Empty strings restore the
        defaults. Returns what is now emulated.
        """
        features = []
        if prefers_color_scheme:
            features.append(
                cdp.emulation.MediaFeature(name="prefers-color-scheme", value=prefers_color_scheme)
            )
        await self.session.page.send(
            cdp.emulation.set_emulated_media(media=media or None, features=features or None)
        )
        return MediaResult(
            summary=f"Media: {media or 'default'}, prefers-color-scheme: "
            f"{prefers_color_scheme or 'default'}",
            media_type=media or None,
            prefers_color_scheme=prefers_color_scheme or None,
        )
