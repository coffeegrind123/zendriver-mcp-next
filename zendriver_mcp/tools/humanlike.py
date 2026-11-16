"""Human-like interaction tools.

Exposes MCP tools that move the cursor and type with realistic timing,
wrapping the low-level primitives in ``zendriver_mcp.humaninput``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from zendriver_mcp import submit_guard
from zendriver_mcp.errors import Code, InvalidArgumentError, ZendriverMCPError
from zendriver_mcp.humaninput import (
    Point,
    human_click,
    human_type,
    keystroke_delays,
)
from zendriver_mcp.registry import Effect, Reach
from zendriver_mcp.response import Result
from zendriver_mcp.tools.base import ToolBase
from zendriver_mcp.tools.elements import find_by_text


class HumanClickResult(Result):
    target: str = Field(description="The selector, id or text clicked.")
    x: float = Field(description="Viewport x the cursor ended at.")
    y: float = Field(description="Viewport y the cursor ended at.")
    id: int | None = Field(
        default=None, description="Interaction-tree id of the element, when known."
    )
    shadow: bool | None = Field(
        default=None, description="True when the target sat inside shadow DOM."
    )


class HumanTypeResult(Result):
    chars: int = Field(description="Characters typed.")
    wpm: int = Field(description="Typing speed modelled.")


class DurationResult(Result):
    seconds: float = Field(description="Expected typing time in seconds.")


class HumanInputTools(ToolBase):
    """Cursor paths and typing that look organic to bot-detection."""

    GROUP = "humaninput"

    def _register_tools(self) -> None:
        self._register(self.human_click, Effect.WRITE, "Human-like click", reach=Reach.WEB)
        self._register(self.human_type, Effect.WRITE, "Human-like typing", reach=Reach.WEB)
        self._register(
            self.estimated_typing_duration,
            Effect.READ,
            "Estimate typing time",
            idempotent=True,
            page_scoped=False,
        )

    async def human_click(
        self,
        selector: Annotated[
            str | None,
            Field(
                description="CSS selector, or a numeric id from get_interaction_tree. Example: '#login' or '12'"
            ),
        ] = None,
        text: Annotated[
            str | None,
            Field(
                description="Visible text of the element, matched across light and shadow DOM. Used only when selector is omitted. Example: 'Sign in'"
            ),
        ] = None,
        move_duration: Annotated[
            float,
            Field(description="Seconds the cursor takes to travel to the target. Example: 0.4"),
        ] = 0.4,
        force: Annotated[
            bool,
            Field(
                description="Click even when another element covers the target; the click lands on whatever is on top. Example: false"
            ),
        ] = False,
    ) -> HumanClickResult:
        """Click an element after moving the cursor along a curved, humanish path.

        Pass exactly one of selector or text. The cursor travels from where the
        last human_click left it along a Bezier path with jitter, then presses
        and releases with a realistic hold, all as trusted input. Text mode is
        shadow-DOM aware: it aims at the tightest interactive element matching
        the text, inside nested shadow roots when needed. Slower than click;
        use it where bot detection watches pointer movement. Like click, it
        refuses with element_obscured when something covers the target, unless
        force is set. Returns the point clicked and the target's tree id.
        """
        if selector:
            handle = await self.resolve(selector)
            info: dict = {}
            label = selector
        elif text:
            handle, info = await find_by_text(self, text)
            label = text
        else:
            raise InvalidArgumentError("Pass selector or text.")

        await submit_guard.check_click(self, handle)
        cx, cy = await self.dom.center(handle)
        if not force:
            blocker = await self.dom.obstruction(handle, cx, cy)
            if blocker:
                raise ZendriverMCPError(
                    f"{blocker} covers {label} at ({cx:.0f}, {cy:.0f}).",
                    code=Code.ELEMENT_OBSCURED,
                    hint="Close the overlay on top (a cookie banner, a modal) or scroll so the "
                    "element is unobstructed. force=true clicks at that point anyway.",
                )
        await human_click(self.session.page, target=Point(cx, cy), move_duration=move_duration)
        shadow_note = " (shadow)" if info.get("shadow") else ""
        return HumanClickResult(
            summary=f"Human-clicked: {label}{shadow_note}",
            target=label,
            x=cx,
            y=cy,
            id=info.get("id"),
            shadow=info.get("shadow"),
        )

    async def human_type(
        self,
        text: Annotated[str, Field(description="Text to type. Example: 'hello world'")],
        selector: Annotated[
            str | None,
            Field(
                description="Element to click into first with human_click. Omit to type into whatever has focus. Example: '#search'"
            ),
        ] = None,
        wpm: Annotated[
            int,
            Field(
                description="Typing speed in words per minute; 220 is a fast but plausible adult typist. Example: 220"
            ),
        ] = 220,
    ) -> HumanTypeResult:
        """Type text one character at a time with human keystroke timing.

        Delays between characters follow a jittered distribution around wpm.
        If selector is given the element is first clicked with human_click.
        Takes roughly estimated_typing_duration seconds; long texts can run
        into the tool's time budget, so split them.
        Returns how many characters were typed and at what speed.
        """
        if wpm < 1:
            raise InvalidArgumentError("wpm must be at least 1.")
        if selector:
            await self.human_click(selector=selector)
        await human_type(self.session.page, text=text, wpm=wpm)
        return HumanTypeResult(
            summary=f"Typed {len(text)} chars at ~{wpm} wpm", chars=len(text), wpm=wpm
        )

    async def estimated_typing_duration(
        self,
        char_count: Annotated[int, Field(description="Number of characters. Example: 120")],
        wpm: Annotated[
            int, Field(description="Typing speed in words per minute. Example: 220")
        ] = 220,
    ) -> DurationResult:
        """Estimate the typing duration human_type needs for char_count characters.

        Useful for scheduling follow-up actions, or for splitting a long text so
        each human_type call stays inside the tool's time budget. Returns the
        expected seconds.
        """
        if wpm < 1 or char_count < 0:
            raise InvalidArgumentError("char_count must be >= 0 and wpm >= 1.")
        seconds = float(sum(keystroke_delays(char_count, wpm=wpm)))
        return DurationResult(
            summary=f"~{seconds:.1f}s to type {char_count} chars at {wpm} wpm", seconds=seconds
        )
