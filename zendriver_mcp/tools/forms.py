# form and input tools - fill form, submit, keyboard, mouse
import json
from typing import Annotated, Any, Literal

from pydantic import Field
from zendriver import cdp

from zendriver_mcp.errors import Code, InvalidArgumentError, ZendriverMCPError
from zendriver_mcp.registry import Effect, Reach
from zendriver_mcp.response import Result
from zendriver_mcp.tools.base import ToolBase
from zendriver_mcp.tools.elements import focus_for_typing

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

SELECTOR_DESCRIPTION = (
    "CSS selector, or a numeric id from get_interaction_tree, of the element to "
    "focus first. Omit to target whatever currently has focus. Example: '#search'"
)


def _key_definition(key: str) -> tuple[str, str, int, str | None]:
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


class FillResult(Result):
    filled: list[str] = Field(description="Selectors that were filled.")
    missing: list[str] = Field(description="Selectors that matched nothing and were skipped.")


class KeyResult(Result):
    key: str = Field(description="The key pressed.")
    target: str | None = Field(default=None, description="Element focused first, if any.")


class SubmitResult(Result):
    target: str = Field(description="The form selector.")
    method: str = Field(description="'requestSubmit' (handlers and validation run) or 'submit'.")


class PointResult(Result):
    x: float = Field(description="Viewport x of the click.")
    y: float = Field(description="Viewport y of the click.")
    hit: str | None = Field(
        default=None, description="The element at that point, as '<tag#id.class>'."
    )


class FormTools(ToolBase):
    """tools for forms and input handling"""

    GROUP = "forms"

    def _register_tools(self) -> None:
        """register form and input tools"""
        self._register(self.fill_form, Effect.WRITE, "Fill form fields", reach=Reach.WEB)
        self._register(self.submit_form, Effect.WRITE, "Submit form", reach=Reach.WEB)
        self._register(self.press_key, Effect.WRITE, "Press key", reach=Reach.WEB)
        self._register(self.press_enter, Effect.WRITE, "Press Enter", reach=Reach.WEB)
        self._register(self.mouse_click, Effect.WRITE, "Click at coordinates", reach=Reach.WEB)

    async def fill_form(
        self,
        form_data: Annotated[
            dict[str, Any] | str,
            Field(
                description='An object mapping CSS selector (or interaction-tree id) to the value to type, or the same object as a JSON string. Example: {"#email": "user@example.com", "#password": "hunter2"}'
            ),
        ],
    ) -> FillResult:
        """Fill several form fields in one call, clearing each before typing.

        Faster than repeated clear_input/type_text pairs. Each field is focused
        with a real click, emptied, and given its value through the browser's
        text insertion. Selectors that match nothing are reported in missing,
        not treated as failures. Does not submit: follow with submit_form,
        press_enter or click.
        """
        if isinstance(form_data, str):
            try:
                data = json.loads(form_data)
            except json.JSONDecodeError as exc:
                raise InvalidArgumentError(
                    f"form_data is not valid JSON: {exc}",
                    hint='Pass an object like {"#email": "a@b.c"}.',
                ) from exc
        else:
            data = form_data
        if not isinstance(data, dict):
            raise InvalidArgumentError("form_data must be an object of selector -> value.")

        filled: list[str] = []
        missing: list[str] = []
        for selector, value in data.items():
            try:
                handle = await self.resolve(str(selector))
            except ZendriverMCPError as exc:
                if exc.code in (Code.ELEMENT_NOT_FOUND, Code.STALE_ID):
                    missing.append(str(selector))
                    continue
                raise
            await focus_for_typing(self, handle)
            await self.element_call(
                handle,
                "function(el) {"
                " if (typeof el.select === 'function') { el.select(); }"
                " else { const r = document.createRange(); r.selectNodeContents(el);"
                "   const s = getSelection(); s.removeAllRanges(); s.addRange(r); }"
                " document.execCommand('delete'); }",
            )
            await self.session.page.send(cdp.input_.insert_text(str(value)))
            filled.append(str(selector))

        note = f"; {len(missing)} not found: {', '.join(missing)}" if missing else ""
        return FillResult(
            summary=f"Filled {len(filled)} field(s){note}", filled=filled, missing=missing
        )

    async def submit_form(
        self,
        selector: Annotated[
            str,
            Field(
                description="CSS selector or interaction-tree id of the form element. Defaults to the page's first form. Example: '#login-form'"
            ),
        ] = "form",
        native: Annotated[
            bool,
            Field(
                description="Use form.submit(), which skips submit handlers and validation. Only for forms whose handlers block submission. Example: false"
            ),
        ] = False,
    ) -> SubmitResult:
        """Submit a form as if its submit button were pressed.

        Uses form.requestSubmit(), so the page's submit handlers and HTML
        validation run the way they do for a user. On JavaScript-driven forms,
        clicking the actual submit button or press_enter is still the most
        faithful option. Fails when the selector does not match a <form>.
        """
        result = await self.element_call(
            selector,
            "function(el, native) {"
            " const form = el.tagName === 'FORM' ? el : el.closest('form');"
            " if (!form) return {error: el.tagName.toLowerCase()};"
            " if (!native && typeof form.requestSubmit === 'function') {"
            "   form.requestSubmit(); return {method: 'requestSubmit'}; }"
            " form.submit(); return {method: 'submit'}; }",
            native,
        )
        if "error" in result:
            raise InvalidArgumentError(
                f"{selector} is a <{result['error']}> outside any form.",
                hint="Find the form with find_element('form'), or click the submit button.",
            )
        return SubmitResult(
            summary=f"Form submitted via {result['method']}: {selector}",
            target=selector,
            method=result["method"],
        )

    async def press_key(
        self,
        key: Annotated[
            str,
            Field(
                description="Key name for named keys ('Enter', 'Tab', 'Escape', 'Backspace', 'Delete', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Space', 'Home', 'End', 'PageUp', 'PageDown'), or a single character for literal keys. Example: 'Enter'"
            ),
        ],
        selector: Annotated[str | None, Field(description=SELECTOR_DESCRIPTION)] = None,
        modifiers: Annotated[
            list[Modifier] | None,
            Field(
                description="Modifier keys held during the press, for shortcuts. Example: ['Control'] with key 'a' selects all"
            ),
        ] = None,
    ) -> KeyResult:
        """Press one key as a real keyboard event, with its native effect.

        The browser produces keydown, keypress (for printable keys) and keyup,
        all trusted, and does what the key does: Enter submits the enclosing
        form or activates a focused button, Tab moves focus, Backspace deletes.
        Use when a framework listens for key events that type_text's bulk insert
        does not fire. Focuses selector first when given. Returns the key
        pressed and the element focused first, if any.
        """
        k, code, vk, text = _key_definition(key)
        mods = 0
        for m in modifiers or []:
            mods |= _MODIFIER_BITS[m]
        if mods & (_MODIFIER_BITS["Control"] | _MODIFIER_BITS["Alt"] | _MODIFIER_BITS["Meta"]):
            text = None  # a shortcut, not text entry
        if selector:
            await self.dom.focus(selector)
        page = self.session.page
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
        combo = "+".join([*(modifiers or []), key])
        return KeyResult(
            summary=f"Pressed {combo}" + (f" on {selector}" if selector else ""),
            key=combo,
            target=selector,
        )

    async def press_enter(
        self,
        selector: Annotated[str | None, Field(description=SELECTOR_DESCRIPTION)] = None,
    ) -> KeyResult:
        """Press Enter, submitting an enclosing form or activating a button.

        A convenience wrapper for press_key('Enter'); use that for any other key.
        More faithful than submit_form on JavaScript-driven forms, because the
        browser performs the implicit submission itself.
        """
        return await self.press_key("Enter", selector)

    async def mouse_click(
        self,
        x: Annotated[
            float,
            Field(
                description="Horizontal position in CSS pixels from the viewport's left edge. Example: 640"
            ),
        ],
        y: Annotated[
            float,
            Field(
                description="Vertical position in CSS pixels from the viewport's top edge. Example: 400"
            ),
        ],
        button: Annotated[
            Literal["left", "right", "middle"],
            Field(description="Mouse button. Example: 'left'"),
        ] = "left",
        count: Annotated[
            int, Field(description="Number of clicks; 2 is a double click. Example: 1")
        ] = 1,
    ) -> PointResult:
        """Click at viewport coordinates rather than at an element.

        A last resort for canvas, map, and custom widgets that expose nothing
        selectable — prefer click for anything with a selector or numeric id,
        since coordinates break whenever the layout shifts. The point must be
        within the current viewport; scroll first if not. Reports what element
        sat at that point.
        """
        page = self.session.page
        hit = await self.run_js(
            "(() => { const t = document.elementFromPoint(%f, %f); if (!t) return null;"
            " const id = t.id ? '#' + t.id : '';"
            " return '<' + t.tagName.toLowerCase() + id + '>'; })()" % (float(x), float(y))
        )
        btn = cdp.input_.MouseButton(button)
        held = {"left": 1, "right": 2, "middle": 4}[button]
        await page.send(cdp.input_.dispatch_mouse_event(type_="mouseMoved", x=x, y=y))
        for n in range(1, max(1, count) + 1):
            await page.send(
                cdp.input_.dispatch_mouse_event(
                    type_="mousePressed", x=x, y=y, button=btn, click_count=n, buttons=held
                )
            )
            await page.send(
                cdp.input_.dispatch_mouse_event(
                    type_="mouseReleased", x=x, y=y, button=btn, click_count=n, buttons=0
                )
            )
        return PointResult(
            summary=f"Clicked at ({x:g}, {y:g})" + (f" on {hit}" if hit else ""),
            x=x,
            y=y,
            hit=hit,
        )
