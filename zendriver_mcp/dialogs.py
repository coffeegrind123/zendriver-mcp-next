"""JavaScript dialogs (alert, confirm, prompt, beforeunload), per tab.

An open dialog freezes its tab: every script evaluation, and the CDP input
call that opened it, waits until the dialog is answered. Before this module a
page's `alert()` made every following tool on that tab run into its time budget
and fail as a generic timeout. Now the server knows a dialog is open, says so
at once (code ``dialog_open``), and answers it on request or by policy.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import zendriver as zd
from zendriver import cdp

from zendriver_mcp.errors import Code, ZendriverMCPError

logger = logging.getLogger(__name__)

# The most recent dialogs kept for list_dialogs.
LOG_SIZE = 50


class Policy(str, Enum):
    MANUAL = "manual"
    ACCEPT = "accept"
    DISMISS = "dismiss"


class DialogOpenError(ZendriverMCPError):
    code = Code.DIALOG_OPEN
    hint = (
        "Answer it with handle_dialog(accept=true/false), or set_dialog_policy to answer "
        "dialogs automatically."
    )


@dataclass
class Dialog:
    tab: str | None
    type: str
    message: str
    url: str
    default_prompt: str | None
    opened: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    outcome: str | None = None

    def describe(self) -> str:
        return f"{self.type} dialog {self.message!r}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "tab": self.tab,
            "type": self.type,
            "message": self.message,
            "url": self.url,
            "default_prompt": self.default_prompt,
            "opened": self.opened,
            "outcome": self.outcome,
        }


class Dialogs:
    """Open dialogs by tab object, the recent log, and the answering policy."""

    def __init__(self) -> None:
        self.policy = Policy.MANUAL
        self._open: dict[int, Dialog] = {}
        self._events: dict[int, asyncio.Event] = {}
        self.log: list[Dialog] = []

    def reset(self) -> None:
        self._open.clear()
        self._events.clear()
        self.log.clear()

    def open_on(self, tab: zd.Tab) -> Dialog | None:
        return self._open.get(id(tab))

    def opened_event(self, tab: zd.Tab) -> asyncio.Event:
        """Set while a dialog is open on ``tab``; a running tool races against it."""
        ev = self._events.get(id(tab))
        if ev is None:
            ev = self._events[id(tab)] = asyncio.Event()
            if id(tab) in self._open:
                ev.set()
        return ev

    async def on_opening(
        self, event: cdp.page.JavascriptDialogOpening, *, tab: zd.Tab, tab_id: str | None
    ) -> None:
        dialog = Dialog(
            tab=tab_id,
            type=str(event.type_.value if hasattr(event.type_, "value") else event.type_),
            message=event.message,
            url=event.url,
            default_prompt=event.default_prompt,
        )
        self.log.append(dialog)
        del self.log[:-LOG_SIZE]
        if self.policy is not Policy.MANUAL:
            dialog.outcome = f"auto-{self.policy.value}ed"
            # Answered from a task: this handler runs on zendriver's receive
            # loop, which must keep reading to deliver the reply.
            asyncio.get_running_loop().create_task(
                self._answer(tab, self.policy is Policy.ACCEPT, None)
            )
            return
        self._open[id(tab)] = dialog
        self.opened_event(tab).set()

    async def on_closed(self, event: cdp.page.JavascriptDialogClosed, *, tab: zd.Tab) -> None:
        dialog = self._open.pop(id(tab), None)
        if dialog is not None and dialog.outcome is None:
            dialog.outcome = "accepted" if event.result else "dismissed"
        ev = self._events.get(id(tab))
        if ev is not None:
            ev.clear()

    async def answer(self, tab: zd.Tab, accept: bool, prompt_text: str | None) -> Dialog:
        dialog = self._open.get(id(tab))
        if dialog is None:
            raise ZendriverMCPError(
                "No dialog is open on this tab.",
                code=Code.INVALID_ARGUMENT,
                hint="list_dialogs shows recent dialogs and how they were answered.",
            )
        await self._answer(tab, accept, prompt_text)
        dialog.outcome = "accepted" if accept else "dismissed"
        if prompt_text is not None and accept:
            dialog.outcome += f" with {prompt_text!r}"
        self._open.pop(id(tab), None)
        ev = self._events.get(id(tab))
        if ev is not None:
            ev.clear()
        return dialog

    async def _answer(self, tab: zd.Tab, accept: bool, prompt_text: str | None) -> None:
        try:
            await tab.send(
                cdp.page.handle_java_script_dialog(accept=accept, prompt_text=prompt_text)
            )
        except Exception:  # the dialog closed on its own (navigation) in the meantime
            logger.debug("handleJavaScriptDialog failed", exc_info=True)
