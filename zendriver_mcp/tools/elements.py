# element interaction tools - click, type, clear, focus, select, upload
import os
from typing import Annotated, Any

from pydantic import Field
from zendriver import cdp

from zendriver_mcp import submit_guard
from zendriver_mcp.dom import Handle
from zendriver_mcp.errors import (
    Code,
    ElementNotFoundError,
    InvalidArgumentError,
    ZendriverMCPError,
)
from zendriver_mcp.registry import Effect, Reach
from zendriver_mcp.response import Result
from zendriver_mcp.suggest import did_you_mean
from zendriver_mcp.tools._shadow_js import DESCRIBE_SHADOW_FN, FIND_BY_TEXT_FN, INNER_CLICKABLE_FN
from zendriver_mcp.tools.base import ToolBase

SELECTOR_DESCRIPTION = (
    "CSS selector, or a numeric id from get_interaction_tree passed as a string. "
    "Example: '#submit' or '42'"
)


class ElementResult(Result):
    target: str = Field(description="The selector, id or text the call acted on.")
    id: int | None = Field(
        default=None,
        description="Interaction-tree id of the element acted on; reusable while it exists.",
    )
    tag: str | None = Field(default=None, description="Tag name of the element acted on.")


class ShadowTree(Result):
    target: str = Field(description="The host selector or id described.")
    tree: dict[str, Any] = Field(
        description="Condensed tree: tag, id, role, type, text, 'light' children and 'shadow' children."
    )


async def find_by_text(tools: ToolBase, text: str) -> tuple[Handle, dict[str, Any]]:
    """The element a person would mean by ``text``, shadow DOM included.

    Tries the shadow-aware finder first (it climbs to the owning link or button
    and dives into custom-element shadow roots), then zendriver's DOM search,
    which also covers text the finder cannot see.
    """
    root = await tools.resolve(":root")
    found = await tools.element_call(root, FIND_BY_TEXT_FN, text)
    if isinstance(found, dict) and found.get("ok"):
        handle = await tools.resolve(str(found["id"]))
        return handle, found
    handle = await tools.handle_by_text(text)
    return handle, {"id": None, "tag": None}


async def focus_for_typing(tools: ToolBase, handle: Handle) -> str:
    """Focus by a real click; fall back to programmatic focus when covered.

    Typing only needs focus, and a field under a floating label or a
    decorative overlay is still a field: refusing to type into it would be
    wrong where refusing to click it is right. Returns how it was focused.
    """
    try:
        await tools.dom.click(handle)
        return "click"
    except ZendriverMCPError as exc:
        if exc.code not in (Code.ELEMENT_OBSCURED, Code.ELEMENT_HIDDEN):
            raise
    await tools.dom.focus(handle)
    return "focus"


class ElementTools(ToolBase):
    """tools for interacting with page elements"""

    GROUP = "elements"

    def _register_tools(self) -> None:
        """register element interaction tools"""
        self._register(self.click, Effect.WRITE, "Click element", reach=Reach.WEB)
        self._register(self.click_shadow, Effect.WRITE, "Click inside shadow DOM", reach=Reach.WEB)
        self._register(self.describe_shadow, Effect.READ, "Describe shadow DOM")
        self._register(self.type_text, Effect.WRITE, "Type text", reach=Reach.WEB)
        self._register(
            self.clear_input, Effect.WRITE, "Clear input", reach=Reach.WEB, idempotent=True
        )
        self._register(self.focus_element, Effect.WRITE, "Focus element", idempotent=True)
        self._register(
            self.select_option, Effect.WRITE, "Select option", reach=Reach.WEB, idempotent=True
        )
        self._register(
            self.upload_file, Effect.WRITE, "Upload file", reach=Reach.WEB, idempotent=True
        )

    async def _tag(self, handle: Handle) -> str:
        return str(
            await self.element_call(handle, "function(el) { return el.tagName.toLowerCase(); }")
        )

    async def click(
        self,
        selector: Annotated[str | None, Field(description=SELECTOR_DESCRIPTION)] = None,
        text: Annotated[
            str | None,
            Field(
                description="Visible text of the element to click, matched as a best match across light and shadow DOM. Used only when selector is omitted. Example: 'Add to cart'"
            ),
        ] = None,
        force: Annotated[
            bool,
            Field(
                description="Click at the element's position even when something else covers it (an overlay, a banner). Off by default, so a covered element is reported instead of clicking the wrong thing. Example: false"
            ),
        ] = False,
    ) -> ElementResult:
        """Click one element, located by selector, numeric id, or visible text.

        Pass selector or text. A real mouse event at the element's centre,
        scrolled into view, so the page sees a user's click. Ids from
        get_interaction_tree are the most reliable on complex pages. Fails with
        element_obscured, naming the cover, when something is on top; force=true
        clicks anyway. Returns the target, its tag, and its id when matched by text.
        """
        if selector:
            # Tag first: the click may navigate, and then the element is gone.
            handle = await self.resolve(selector)
            tag = await self._tag(handle)
            await submit_guard.check_click(self, handle)
            await self.dom.click(handle, force=force)
            return ElementResult(summary=f"Clicked: {selector}", target=selector, tag=tag)
        if text:
            handle, info = await find_by_text(self, text)
            await submit_guard.check_click(self, handle)
            await self.dom.click(handle, force=force)
            shadow = " (inside shadow DOM)" if info.get("shadow") else ""
            return ElementResult(
                summary=f"Clicked: {text}{shadow}",
                target=text,
                id=info.get("id"),
                tag=(info.get("tag") or "").lower() or None,
            )
        raise InvalidArgumentError("Pass selector or text.")

    async def click_shadow(
        self,
        selector: Annotated[
            str,
            Field(
                description="CSS selector, or a numeric id, of the outer custom element in the light DOM whose shadow tree holds the real control. Example: 'nes-button' or '5'"
            ),
        ],
        max_depth: Annotated[
            int, Field(description="How many nested shadow roots to descend through. Example: 6")
        ] = 6,
        force: Annotated[
            bool,
            Field(description="Click even when something covers the target. Example: false"),
        ] = False,
    ) -> ElementResult:
        """Click the deepest interactive element inside a custom element's shadow DOM.

        Use when a page wraps a real <button> / [role="radio"] /
        [role="checkbox"] in one or more open shadow roots (<nes-button>,
        <sds-radio>, <lion-input> and friends). selector should match the outer
        custom element in the light DOM; this recurses through every nested
        shadowRoot up to max_depth, finds the first interactive descendant and
        clicks it with a real mouse event. Returns the inner element's id and
        tag, which later calls can reuse.
        """
        host = await self.resolve(selector)
        found = await self.element_call(host, INNER_CLICKABLE_FN, int(max_depth))
        if not isinstance(found, dict) or not found.get("ok"):
            reason = found.get("reason") if isinstance(found, dict) else "unknown"
            raise ZendriverMCPError(
                f"No interactive element inside {selector} ({reason}).",
                code=Code.ELEMENT_NOT_FOUND,
                hint="Inspect it with describe_shadow; the control may sit in a closed shadow root or deeper than max_depth.",
            )
        inner = await self.resolve(str(found["id"]))
        await submit_guard.check_click(self, inner)
        await self.dom.click(inner, force=force)
        role = f" role={found['role']}" if found.get("role") else ""
        return ElementResult(
            summary=f"Shadow-clicked: <{found['tag'].lower()}{role}> inside {selector}",
            target=selector,
            id=found["id"],
            tag=found["tag"].lower(),
        )

    async def describe_shadow(
        self,
        selector: Annotated[
            str,
            Field(
                description="CSS selector, or a numeric id, of the custom element whose nested shadow tree to dump. Example: 'lion-input' or '9'"
            ),
        ],
        max_depth: Annotated[
            int, Field(description="How many nested shadow roots to descend through. Example: 6")
        ] = 6,
    ) -> ShadowTree:
        """Dump a custom element's nested shadow-DOM tree for debugging.

        Returns a condensed tree: each node has tag, id, role, type, text, a
        'light' array of light-DOM children and a 'shadow' array for the
        element's shadowRoot children (when open). Use this when find_buttons /
        find_inputs are not surfacing a control you can see; the result names
        the nested-host path so you can target click_shadow.
        """
        host = await self.resolve(selector)
        tree = await self.element_call(host, DESCRIBE_SHADOW_FN, int(max_depth))
        return ShadowTree(summary=f"Shadow tree of {selector}", target=selector, tree=tree or {})

    async def type_text(
        self,
        text: Annotated[
            str,
            Field(
                description="Literal text to insert. Inserted as one chunk, so it fires no per-key events. Example: 'user@example.com'"
            ),
        ],
        selector: Annotated[str, Field(description=SELECTOR_DESCRIPTION)],
    ) -> ElementResult:
        """Type text into an input via the browser's own text insertion.

        Focuses the element with a real click first (programmatic focus when
        something decorative covers it), then inserts the text through CDP, so
        the page receives genuine input events and no page JavaScript runs.
        Appends to any existing value: call clear_input first to replace it.
        Use press_key or human_type when a page needs per-key events.
        Returns the target, its tag, and a summary with the character count.
        """
        handle = await self.resolve(selector)
        await focus_for_typing(self, handle)
        await self.session.page.send(cdp.input_.insert_text(text))
        return ElementResult(
            summary=f"Typed {len(text)} char(s) into {selector}",
            target=selector,
            tag=await self._tag(handle),
        )

    async def clear_input(
        self,
        selector: Annotated[str, Field(description=SELECTOR_DESCRIPTION)],
    ) -> ElementResult:
        """Empty an input, textarea, or contenteditable element.

        Use before type_text to replace a value rather than append to it.
        Selects all and deletes through the editing pipeline, so the page sees a
        real edit and its input handlers fire. Returns the target, its tag and its tree id.
        """
        handle = await self.resolve(selector)
        await self.dom.focus(handle)
        await self.element_call(
            handle,
            "function(el) {"
            " if (typeof el.select === 'function') { el.select(); }"
            " else { const r = document.createRange(); r.selectNodeContents(el);"
            "   const s = getSelection(); s.removeAllRanges(); s.addRange(r); }"
            " document.execCommand('delete'); }",
        )
        return ElementResult(
            summary=f"Cleared: {selector}", target=selector, tag=await self._tag(handle)
        )

    async def focus_element(
        self,
        selector: Annotated[str, Field(description=SELECTOR_DESCRIPTION)],
    ) -> ElementResult:
        """Move keyboard focus to an element without clicking it.

        Use before press_key when a click would trigger unwanted behaviour, or to
        open a widget that reacts to focus. type_text focuses on its own, so this
        is not needed first. Returns the target, its tag and its tree id.
        """
        handle = await self.dom.focus(selector)
        return ElementResult(
            summary=f"Focused: {selector}", target=selector, tag=await self._tag(handle)
        )

    async def select_option(
        self,
        selector: Annotated[
            str,
            Field(
                description="CSS selector of the <select> element, or a numeric id from get_interaction_tree. Example: '#country' or '9'"
            ),
        ],
        value: Annotated[
            str,
            Field(
                description="The option's value attribute. A visible label is accepted too when no value matches. Example: 'FI'"
            ),
        ],
    ) -> ElementResult:
        """Choose an option in a native <select> dropdown.

        Matches the option's value attribute first, then its visible label. Sets
        it and dispatches bubbling input and change events, so framework
        listeners fire. Only works on native <select>; custom dropdowns built
        from divs need click instead. An unknown value fails with the closest
        real options in the hint. The input/change events it dispatches are
        isTrusted:false (a native <select> has no trusted path short of driving
        it by keyboard: focus_element, then press_key ArrowDown/Enter), which a
        page checking event.isTrusted can notice. Returns the target, with the option chosen in the summary.
        """
        handle = await self.resolve(selector)
        result = await self.element_call(
            handle,
            "function(el, want) {"
            " if (el.tagName !== 'SELECT') return {error: 'not_select', tag: el.tagName.toLowerCase()};"
            " const opts = Array.from(el.options);"
            " let o = opts.find(x => x.value === want)"
            "   || opts.find(x => x.text.trim() === want.trim());"
            " if (!o) return {error: 'no_option',"
            "   options: opts.map(x => x.value), labels: opts.map(x => x.text.trim())};"
            " el.value = o.value;"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " return {value: o.value, label: o.text.trim()}; }",
            value,
        )
        if result.get("error") == "not_select":
            raise InvalidArgumentError(
                f"{selector} is a <{result['tag']}>, not a <select>.",
                hint="Custom dropdowns are opened and chosen with click.",
            )
        if result.get("error") == "no_option":
            values = list(result.get("options", []))
            labels = list(result.get("labels", []))
            listing = ", ".join(f"{v!r} ({lb})" for v, lb in zip(values, labels, strict=False))
            close = did_you_mean(value, [c for c in values + labels if len(c) > 1])
            raise InvalidArgumentError(
                f"No option {value!r} in {selector}.",
                hint=(f"{close} " if close else "") + f"Options: {listing[:600]}",
            )
        return ElementResult(
            summary=f"Selected {result['label']!r} (value {result['value']!r}) in {selector}",
            target=selector,
            tag="select",
        )

    async def upload_file(
        self,
        selector: Annotated[
            str,
            Field(
                description="CSS selector of the <input type=\"file\"> element, or a numeric id from get_interaction_tree. Example: 'input[type=\"file\"]' or '5'"
            ),
        ],
        file_path: Annotated[
            str,
            Field(
                description="Absolute path to the file, as seen by the machine running this server, not the browser host. Example: '/home/user/documents/id.png'"
            ),
        ],
    ) -> ElementResult:
        """Attach a local file to a file input, without opening the OS file picker.

        The path must exist on the server's filesystem. Submit the surrounding
        form afterwards with submit_form or click. Returns the target, its tag and its tree id.
        """
        if not os.path.isfile(file_path):
            raise InvalidArgumentError(
                f"No such file: {file_path}",
                hint="Pass an absolute path that exists on the machine running this server.",
            )
        handle = await self.resolve(selector)
        if await self._tag(handle) != "input":
            raise ElementNotFoundError(
                selector, hint='Target the <input type="file"> itself, not its styled label.'
            )
        await self.dom.set_files(handle, [os.path.abspath(file_path)])
        return ElementResult(
            summary=f"Attached {os.path.basename(file_path)} to {selector}",
            target=selector,
            tag="input",
        )
