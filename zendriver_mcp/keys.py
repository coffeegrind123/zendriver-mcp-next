"""Trusted keyboard input through CDP.

Shared by press_key and fill_form, which both need the browser itself to
produce key events.
"""

from __future__ import annotations

from typing import Literal

import zendriver as zd
from zendriver import cdp

from zendriver_mcp.errors import InvalidArgumentError

# CDP key definitions for named keys: (key, code, windowsVirtualKeyCode, text).
# Sent through Input.dispatchKeyEvent, so the browser itself produces the
# keydown/keypress/keyup sequence with isTrusted:true and does what the key does
# natively: Enter submits the enclosing form (implicit submission), Tab moves
# focus, arrows move the caret. The old implementation dispatched synthetic
# KeyboardEvents from page JavaScript and re-implemented Enter and Tab by hand;
# both were isTrusted:false and missed real browser behaviour.
_NAMED_KEYS: dict[str, tuple[str, str, int, str | None]] = {
    "Enter": ("Enter", "Enter", 13, "\r"),
    "Tab": ("Tab", "Tab", 9, None),
    "Escape": ("Escape", "Escape", 27, None),
    "Backspace": ("Backspace", "Backspace", 8, None),
    "Delete": ("Delete", "Delete", 46, None),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38, None),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40, None),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37, None),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39, None),
    "Space": (" ", "Space", 32, " "),
    " ": (" ", "Space", 32, " "),
    "Home": ("Home", "Home", 36, None),
    "End": ("End", "End", 35, None),
    "PageUp": ("PageUp", "PageUp", 33, None),
    "PageDown": ("PageDown", "PageDown", 34, None),
}

# CDP modifier bit field.
_MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}

Modifier = Literal["Alt", "Control", "Meta", "Shift"]


def modifier_bits(modifiers: list[Modifier] | None) -> int:
    bits = 0
    for m in modifiers or []:
        bits |= _MODIFIER_BITS[m]
    return bits


def key_definition(key: str) -> tuple[str, str, int, str | None]:
    """CDP fields for a named key or a single printable character."""
    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]
    if len(key) != 1:
        raise InvalidArgumentError(
            f"Unknown key {key!r}.",
            hint="Use a single character or one of: "
            + ", ".join(k for k in _NAMED_KEYS if k != " "),
        )
    ch = key
    if ch.isalpha():
        return ch, f"Key{ch.upper()}", ord(ch.upper()), ch
    if ch.isdigit():
        return ch, f"Digit{ch}", ord(ch), ch
    # Punctuation: no reliable US-layout code without a table; Chrome accepts
    # an empty code and still inserts the text.
    return ch, "", 0, ch


async def send_key(page: zd.Tab, key: str, mods: int = 0) -> None:
    """Press and release one key, as the browser's own trusted input.

    Goes to the top-level tab, which routes it to whatever has focus, in any
    frame.
    """
    k, code, vk, text = key_definition(key)
    if mods & (_MODIFIER_BITS["Control"] | _MODIFIER_BITS["Alt"] | _MODIFIER_BITS["Meta"]):
        text = None  # a shortcut, not text entry
    await page.send(
        cdp.input_.dispatch_key_event(
            type_="keyDown" if text else "rawKeyDown",
            key=k,
            code=code or None,
            windows_virtual_key_code=vk or None,
            native_virtual_key_code=vk or None,
            text=text,
            unmodified_text=text,
            modifiers=mods or None,
        )
    )
    await page.send(
        cdp.input_.dispatch_key_event(
            type_="keyUp",
            key=k,
            code=code or None,
            windows_virtual_key_code=vk or None,
            native_virtual_key_code=vk or None,
            modifiers=mods or None,
        )
    )
