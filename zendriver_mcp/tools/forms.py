# form and input tools - fill form, submit, keyboard, mouse
import json
from typing import Annotated, Any, Literal

from pydantic import Field
from zendriver import cdp

from zendriver_mcp import submit_guard
from zendriver_mcp.errors import InvalidArgumentError
from zendriver_mcp.keys import Modifier, key_definition, modifier_bits, send_key
from zendriver_mcp.registry import Effect, Reach
from zendriver_mcp.response import Result
from zendriver_mcp.tools.base import ToolBase
from zendriver_mcp.tools.fill import FieldOutcome, FieldSpec, Filler

# The form an element belongs to (itself, an ancestor, or its form= owner).
_FORM_OF_FN = """function(el) {
  const form = el.tagName === 'FORM' ? el : (el.form || el.closest('form'));
  return form ? { id: globalThis.__zd.idOf(form) } : null;
}"""

SELECTOR_DESCRIPTION = (
    "CSS selector, or a numeric id from get_interaction_tree, of the element to "
    "focus first. Omit to target whatever currently has focus. Example: '#search'"
)


class FillResult(Result):
    filled: list[str] = Field(description="Fields that were filled.")
    missing: list[str] = Field(description="Fields that matched nothing and were skipped.")
    ambiguous: list[str] = Field(
        default_factory=list,
        description="Fields whose label fits several fields; nothing was guessed.",
    )
    failed: list[str] = Field(default_factory=list, description="Fields found but not filled.")
    fields: list[FieldOutcome] = Field(
        default_factory=list, description="Each field's outcome: status, kind, id, trusted, detail."
    )


class KeyResult(Result):
    key: str = Field(description="The key pressed.")
    target: str | None = Field(default=None, description="Element focused first, if any.")


class SubmitResult(Result):
    target: str = Field(description="The form selector.")
    method: str = Field(description="'requestSubmit' (handlers and validation run) or 'submit'.")


class AllowResult(Result):
    guard_enabled: bool = Field(description="Whether the submit guard is on for this server.")


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
        self._register(
            self.allow_next_submit, Effect.WRITE, "Allow the next submit", page_scoped=False
        )

    async def fill_form(
        self,
        form_data: Annotated[
            dict[str, Any] | str | None,
            Field(
                description='Shorthand: selector (or tree id) -> value, as an object or JSON string. Example: {"#email": "a@b.c"}'
            ),
        ] = None,
        fields: Annotated[
            list[FieldSpec] | None,
            Field(
                description='Fields in order, each by target or label, with value, checked, options or files. Example: [{"label": "Email", "value": "a@b.c"}, {"label": "I agree", "checked": true}]'
            ),
        ] = None,
    ) -> FillResult:
        """Fill several form fields of any kind in one call, as a person would.

        A field is found by selector, tree id, or label (its label, placeholder
        or question, in any frame) and filled with trusted input the way that
        control is filled by hand: typing, clicking checkboxes and radios only
        when needed, picking a group's or combobox's option, arrow keys for
        selects and ranges, typed dates and times, files. Colour, month and
        week inputs, and any field the trusted route missed, are set by script
        and reported trusted=false. Returns each field as filled, missing,
        ambiguous (never guessed) or failed. Does not submit.
        """
        specs: list[FieldSpec] = []
        if form_data is not None:
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
            for selector, value in data.items():
                checked = value if isinstance(value, bool) else None
                specs.append(
                    FieldSpec(
                        target=str(selector),
                        value=None if checked is not None else value,
                        checked=checked,
                    )
                )
        specs += fields or []
        if not specs:
            raise InvalidArgumentError(
                "Nothing to fill.",
                hint='Pass fields, e.g. [{"label": "Email", "value": "a@b.c"}], or form_data.',
            )

        filler = Filler(self)
        outcomes = [await filler.fill(spec) for spec in specs]
        by: dict[str, list[str]] = {"filled": [], "missing": [], "ambiguous": [], "failed": []}
        for o in outcomes:
            by[o.status].append(o.field)
        untrusted = [o.field for o in outcomes if o.trusted is False]
        parts = [f"Filled {len(by['filled'])} of {len(outcomes)} field(s)"]
        for key in ("missing", "ambiguous", "failed"):
            if by[key]:
                parts.append(f"{len(by[key])} {key}: {', '.join(by[key])}")
        if untrusted:
            parts.append(f"set by script (untrusted): {', '.join(untrusted)}")
        return FillResult(
            summary="; ".join(parts),
            filled=by["filled"],
            missing=by["missing"],
            ambiguous=by["ambiguous"],
            failed=by["failed"],
            fields=outcomes,
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
        Returns the form and the method used.
        """
        form = await self.element_call(selector, _FORM_OF_FN)
        if form is None:
            raise InvalidArgumentError(
                f"{selector} is outside any form.",
                hint="Find the form with find_element('form'), or click the submit button.",
            )
        await submit_guard.check_form(self, await self.dom.resolve(str(form["id"])))
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

        Trusted keydown/keyup, with the key's own effect (Enter submits or
        activates, Tab moves focus). Use when a page listens for keys that
        type_text does not fire. Focuses selector first when given. Returns the
        key and the element focused, if any.
        """
        mods = modifier_bits(modifiers)
        key_definition(key)  # an unknown key fails before anything is focused
        focused = await self.dom.focus(selector) if selector else None
        chord = bool(mods & modifier_bits(["Control", "Meta"]))
        await submit_guard.check_key(self, key, chord, focused)
        await send_key(self.session.page, key, mods)
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
        Returns the key pressed and the element focused first, if any.
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
        sat at that point. Returns the point and the element that was hit there.
        """
        await submit_guard.check_point(self, x, y)
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

    async def allow_next_submit(self) -> AllowResult:
        """Let exactly one action through the submit guard, once a person has approved it.

        With ZENDRIVER_MCP_SUBMIT_GUARD=1, submitting a form or pressing a
        send/pay/delete-style control fails with submit_blocked. Call this only
        after the person agreed, then repeat the action. Returns whether the
        guard is on.
        """
        if not submit_guard.enabled():
            return AllowResult(
                summary="The submit guard is off; nothing to allow.", guard_enabled=False
            )
        self.session.submit_allowance = True
        return AllowResult(summary="The next submit is allowed, once.", guard_enabled=True)
