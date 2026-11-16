# element query tools - find element, find all, get text, get attribute, find buttons
from typing import Annotated, Any

from pydantic import BaseModel, Field

from zendriver_mcp.errors import InvalidArgumentError, ZendriverMCPError
from zendriver_mcp.registry import Effect
from zendriver_mcp.response import Result
from zendriver_mcp.tools.base import ToolBase

SELECTOR_DESCRIPTION = (
    "CSS selector, or a numeric id from get_interaction_tree passed as a string. "
    "Example: '.price' or '42'"
)

# Shown when a selector misses: the kinds of input a caller is usually after,
# named by selectors that exist on THIS page.
_SUGGEST_FN = """
function(_root) {
    const found = [];
    const ce = document.querySelector('[contenteditable="true"]');
    if (ce) found.push('[contenteditable="true"]');
    if (document.querySelector('[role="textbox"]')) found.push('[role="textbox"]');
    if (document.querySelector('textarea')) found.push('textarea');
    if (document.querySelector('input:not([type="hidden"])')) found.push('input');
    if (document.querySelector('button')) found.push('button');
    return found.slice(0, 5);
}
"""

_DESCRIBE_FN = """
function(el) {
    const style = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return {
        id: globalThis.__zd.idOf(el),
        tag: el.tagName.toLowerCase(),
        text: (el.innerText || el.textContent || "").trim().slice(0, 200),
        visible: style.display !== "none" && style.visibility !== "hidden" && r.width > 0 && r.height > 0,
        editable: el.isContentEditable || el.tagName === "INPUT" || el.tagName === "TEXTAREA",
        clickable: ["BUTTON", "A", "SUMMARY"].includes(el.tagName) || style.cursor === "pointer",
    };
}
"""

_FIND_ALL_FN = """
function(_root, sel, limit) {
    let all;
    try { all = document.querySelectorAll(sel); } catch (e) { return {error: String(e.message || e)}; }
    const items = Array.from(all).slice(0, limit).map(el => ({
        id: globalThis.__zd.idOf(el),
        tag: el.tagName.toLowerCase(),
        text: (el.innerText || el.textContent || "").trim().replace(/\\s+/g, " ").slice(0, 50),
    }));
    return {total: all.length, items};
}
"""

_FIND_BUTTONS_FN = """
function(_root, filterArg) {
    const filter = (filterArg || "").toLowerCase();
    const results = [];

    // helper to get button description
    function getDescription(el) {
        // check text content
        const text = el.innerText?.trim();
        if (text) return text;

        // check aria-label
        const ariaLabel = el.getAttribute("aria-label");
        if (ariaLabel) return ariaLabel;

        // check title
        const title = el.getAttribute("title");
        if (title) return title;

        // check for icon (svg, img, i tag)
        const svg = el.querySelector("svg");
        if (svg) {
            const svgTitle = svg.querySelector("title")?.textContent;
            if (svgTitle) return "Icon: " + svgTitle;
            const use = svg.querySelector("use");
            if (use) {
                const href = use.getAttribute("href") || use.getAttribute("xlink:href");
                if (href) return "Icon: " + href.split("#").pop();
            }
            return "Icon button";
        }

        const img = el.querySelector("img");
        if (img) return "Image: " + (img.alt || img.src.split("/").pop());

        const icon = el.querySelector("i, span[class*='icon']");
        if (icon) return "Icon: " + (icon.className || "unknown");

        return "(no description)";
    }

    // helper to escape CSS selector special characters
    function escapeCSS(str) {
        return str.replace(/([!"#$%&'()*+,./:;<=>?@[\\]^`{|}~])/g, '\\\\$1');
    }

    // helper to check if class is valid for CSS selector
    function isValidClass(c) {
        // skip Tailwind responsive/state prefixes and complex classes
        if (c.includes(':') || c.includes('[') || c.includes(']') || c.includes('/')) return false;
        if (c.includes('hover') || c.includes('active') || c.includes('focus') || c.includes('visible')) return false;
        // skip classes with special CSS chars that would need escaping
        if (/[!"#$%&'()*+,./:;<=>?@\\^`{|}~]/.test(c)) return false;
        // skip very short generic classes or very long ones
        if (c.length < 2 || c.length > 30) return false;
        // must start with a letter or hyphen
        if (!/^[a-zA-Z_-]/.test(c)) return false;
        return true;
    }

    // helper to get unique selector
    function getSelector(el) {
        // prefer ID
        if (el.id) return "#" + escapeCSS(el.id);

        // prefer name attribute
        if (el.name) return el.tagName.toLowerCase() + "[name='" + el.name + "']";

        // try aria-label
        const ariaLabel = el.getAttribute("aria-label");
        if (ariaLabel && ariaLabel.length < 50) {
            const sel = el.tagName.toLowerCase() + "[aria-label='" + ariaLabel.replace(/'/g, "\\'") + "']";
            try {
                if (document.querySelectorAll(sel).length === 1) return sel;
            } catch(e) {}
        }

        // try data-testid, then data-id (each with its own attr name)
        const testId = el.getAttribute("data-testid");
        if (testId) {
            return el.tagName.toLowerCase() + "[data-testid='" + testId + "']";
        }
        const dataId = el.getAttribute("data-id");
        if (dataId) {
            return el.tagName.toLowerCase() + "[data-id='" + dataId + "']";
        }

        // try simple classes only (no Tailwind prefixes)
        const classes = Array.from(el.classList).filter(isValidClass);
        if (classes.length > 0) {
            const escapedClasses = classes.slice(0, 2).map(escapeCSS).join(".");
            const sel = el.tagName.toLowerCase() + "." + escapedClasses;
            try {
                if (document.querySelectorAll(sel).length === 1) return sel;
            } catch(e) {}
        }

        // fallback: use data attributes
        for (const attr of el.attributes) {
            if (attr.name.startsWith("data-") && attr.value && attr.value.length < 50) {
                const sel = el.tagName.toLowerCase() + "[" + attr.name + "='" + attr.value.replace(/'/g, "\\'") + "']";
                try {
                    if (document.querySelectorAll(sel).length === 1) return sel;
                } catch(e) {}
            }
        }

        // last resort: nth-of-type
        const parent = el.parentElement;
        if (parent) {
            const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
            const index = siblings.indexOf(el) + 1;
            return el.tagName.toLowerCase() + ":nth-of-type(" + index + ")";
        }

        return el.tagName.toLowerCase();
    }

    // find all button-like elements
    const selectors = [
        "button",
        "input[type='submit']",
        "input[type='button']",
        "[role='button']",
        "a[href='#']",
        "a[href='javascript:']",
        "[onclick]"
    ];

    const seen = new Set();
    for (const sel of selectors) {
        for (const el of document.querySelectorAll(sel)) {
            if (seen.has(el)) continue;
            seen.add(el);

            const style = window.getComputedStyle(el);
            if (style.display === "none" || style.visibility === "hidden") continue;

            const desc = getDescription(el);
            if (filter && !desc.toLowerCase().includes(filter)) continue;

            results.push({
                id: globalThis.__zd.idOf(el),
                selector: getSelector(el),
                tag: el.tagName,
                description: desc.substring(0, 50),
                type: el.type || el.getAttribute("role") || "button"
            });
        }
    }

    return results.slice(0, 20);
}
"""

_FIND_INPUTS_FN = """
function(_root, filterArg) {
    const filter = (filterArg || "").toLowerCase();
    const results = [];

    function getSelector(el) {
        if (el.id) return "#" + el.id;
        if (el.name) return el.tagName.toLowerCase() + "[name='" + el.name + "']";
        if (el.placeholder) return el.tagName.toLowerCase() + "[placeholder='" + el.placeholder.substring(0, 30) + "']";

        const classes = Array.from(el.classList || []).filter(c => c.length < 30);
        if (classes.length > 0) return el.tagName.toLowerCase() + "." + classes[0];

        return el.tagName.toLowerCase();
    }

    function getDescription(el) {
        const label = document.querySelector("label[for='" + el.id + "']");
        if (label) return label.textContent.trim();
        if (el.placeholder) return el.placeholder;
        if (el.ariaLabel) return el.ariaLabel;
        return el.name || el.type || "input";
    }

    // standard inputs
    for (const el of document.querySelectorAll("input, textarea")) {
        const style = window.getComputedStyle(el);
        if (style.display === "none" || el.type === "hidden") continue;

        const inputType = el.type || "text";
        if (filter && !inputType.includes(filter)) continue;

        results.push({
            id: globalThis.__zd.idOf(el),
            selector: getSelector(el),
            type: inputType,
            description: getDescription(el).substring(0, 40)
        });
    }

    // contenteditable and role=textbox
    for (const el of document.querySelectorAll('[contenteditable="true"], [role="textbox"]')) {
        const style = window.getComputedStyle(el);
        if (style.display === "none") continue;

        if (filter && !["text", "contenteditable", "textbox"].some(t => t.includes(filter))) continue;

        results.push({
            id: globalThis.__zd.idOf(el),
            selector: getSelector(el),
            type: el.getAttribute("role") || "contenteditable",
            description: el.ariaLabel || el.placeholder || "rich text editor"
        });
    }

    return results.slice(0, 20);
}
"""


class FoundElement(BaseModel):
    id: int = Field(description="Interaction-tree id; pass it as a selector to click and friends.")
    tag: str = Field(description="Tag name.")
    text: str = Field(description="Leading text, trimmed.")


class FindResult(Result):
    found: bool = Field(description="Whether a matching element exists.")
    target: str = Field(description="The selector or text searched for.")
    id: int | None = Field(default=None, description="Interaction-tree id of the match.")
    tag: str | None = Field(default=None, description="Tag name of the match.")
    text: str | None = Field(default=None, description="The match's text, up to 200 chars.")
    visible: bool | None = Field(default=None, description="Whether it is rendered and has size.")
    editable: bool | None = Field(default=None, description="Whether it takes typed text.")
    clickable: bool | None = Field(default=None, description="Whether it looks clickable.")
    suggestions: list[str] | None = Field(
        default=None, description="On a miss: selectors that DO exist on this page."
    )


class FindAllResult(Result):
    selector: str = Field(description="The selector searched.")
    total: int = Field(description="How many elements match in all.")
    elements: list[FoundElement] = Field(description="The first `limit` matches.")


class TextResult(Result):
    target: str = Field(description="The selector or id read.")
    text: str = Field(description="The element's text; empty when it has none.")

    def render(self) -> str:
        return self.text if self.text else "(empty)"


class AttributeResult(Result):
    target: str = Field(description="The selector or id read.")
    attribute: str = Field(description="The attribute name.")
    present: bool = Field(description="Whether the element carries the attribute.")
    value: str | None = Field(default=None, description="Its value, when present.")


class Control(BaseModel):
    id: int = Field(description="Interaction-tree id; pass it as a selector.")
    selector: str = Field(description="A CSS selector for it, best effort.")
    type: str = Field(description="Button or input type.")
    description: str = Field(
        description="Label, from text, aria-label, title, icon or placeholder."
    )
    tag: str | None = Field(default=None, description="Tag name.")


class ControlList(Result):
    controls: list[Control] = Field(description="Matches, up to 20.")


class QueryTools(ToolBase):
    """tools for querying and inspecting elements"""

    GROUP = "query"

    def _register_tools(self) -> None:
        """register element query tools"""
        self._register(self.find_element, Effect.READ, "Find element")
        self._register(self.find_all_elements, Effect.READ, "Find all elements")
        self._register(self.get_element_text, Effect.READ, "Element text")
        self._register(self.get_element_attribute, Effect.READ, "Element attribute")
        self._register(self.find_buttons, Effect.READ, "Find buttons")
        self._register(self.find_inputs, Effect.READ, "Find inputs")

    async def _doc_call(self, function: str, *args: Any) -> Any:
        """Run an element_call function against the document root."""
        return await self.element_call(await self.resolve(":root"), function, *args)

    async def find_element(
        self,
        selector: Annotated[
            str | None,
            Field(
                description="CSS selector, or a numeric id from get_interaction_tree, to look for. Example: '#login-form input[type=\"email\"]'"
            ),
        ] = None,
        text: Annotated[
            str | None,
            Field(
                description="Visible text to search for, matched as a best match. Used only when selector is omitted. Example: 'Sign in'"
            ),
        ] = None,
    ) -> FindResult:
        """Check whether one element exists, and report its tag, text, and visibility.

        Pass exactly one of selector or text. Use to verify a selector before
        acting on it. A miss is an answer, not an error: found is false and
        suggestions lists input-like selectors that do exist on the page, which
        is usually enough to correct the selector. A hit includes the element's
        interaction-tree id, reusable by click and type_text while it exists.
        """
        target = selector or text
        if not target:
            raise InvalidArgumentError("Pass selector or text.")
        try:
            if selector:
                handle = await self.resolve(selector)
            else:
                handle = await self.handle_by_text(text or "")
        except ZendriverMCPError as exc:
            if exc.code.value not in ("element_not_found", "stale_id"):
                raise
            suggestions = await self._doc_call(_SUGGEST_FN) if selector else None
            hint = f"; available: {', '.join(suggestions)}" if suggestions else ""
            return FindResult(
                summary=f"Element not found: {target}{hint}",
                found=False,
                target=target,
                suggestions=suggestions or None,
            )
        info = await self.element_call(handle, _DESCRIBE_FN)
        state = "visible" if info["visible"] else "HIDDEN"
        return FindResult(
            summary=f"Found <{info['tag']}> id {info['id']} ({state}): {info['text'] or '(no text)'}",
            found=True,
            target=target,
            **info,
        )

    async def find_all_elements(
        self,
        selector: Annotated[
            str,
            Field(
                description="CSS selector matching the set of elements to list. Example: 'article h2'"
            ),
        ],
        limit: Annotated[
            int,
            Field(
                description="Maximum number of elements to include. The total found is reported regardless. Example: 20"
            ),
        ] = 20,
    ) -> FindAllResult:
        """List every element matching a selector, with its id, tag and leading text.

        Use to enumerate repeated structures such as search results, table rows,
        or list items. Returns the total found and up to limit elements, each
        with an interaction-tree id that click and type_text accept, and its
        text truncated to 50 characters.
        """
        res = await self._doc_call(_FIND_ALL_FN, selector, max(1, int(limit)))
        if "error" in res:
            raise InvalidArgumentError(
                f"Invalid CSS selector {selector!r}: {res['error']}",
                hint='Quote attribute values and escape special characters, e.g. input[name="q"].',
            )
        items = [FoundElement(**i) for i in res["items"]]
        return FindAllResult(
            summary=f"Found {res['total']} element(s) for {selector} (showing {len(items)})",
            selector=selector,
            total=res["total"],
            elements=items,
        )

    async def get_element_text(
        self,
        selector: Annotated[str, Field(description=SELECTOR_DESCRIPTION)],
    ) -> TextResult:
        """Read the visible text of one element.

        Use to extract a single value — a price, a status, a heading — instead of
        pulling the whole page with get_text_content. Falls back to textContent
        for elements that are not rendered. The text is empty when it has none.
        Returns the element's visible text.
        """
        text = await self.element_call(
            selector, "function(el) { return (el.innerText || el.textContent || '').trim(); }"
        )
        text = str(text or "")
        return TextResult(
            summary=f"{len(text)} char(s) of text in {selector}", target=selector, text=text
        )

    async def get_element_attribute(
        self,
        selector: Annotated[str, Field(description=SELECTOR_DESCRIPTION)],
        attribute: Annotated[
            str,
            Field(
                description="Name of the HTML attribute to read, as written in the markup. Example: 'href'"
            ),
        ],
    ) -> AttributeResult:
        """Read one HTML attribute from one element.

        Use for values that are not visible text — href, src, value, data-*,
        aria-*. Reports whether the attribute is present and its raw value as
        written in the markup (a relative href stays relative).
        Returns whether the attribute is present and its value.
        """
        value = await self.element_call(
            selector,
            "function(el, name) { return el.hasAttribute(name) ? el.getAttribute(name) : null; }",
            attribute,
        )
        present = value is not None
        return AttributeResult(
            summary=f"{attribute}={value!r}"
            if present
            else f"{attribute} is not set on {selector}",
            target=selector,
            attribute=attribute,
            present=present,
            value=value,
        )

    async def find_buttons(
        self,
        filter_text: Annotated[
            str | None,
            Field(
                description="Case-insensitive substring to match against each button's label. Omit to list every button. Example: 'submit'"
            ),
        ] = None,
    ) -> ControlList:
        """List the page's clickable buttons with an id and a selector for each.

        Covers button tags, input[type=submit|button], [role=button], onclick
        handlers, and icon-only buttons — labelling those from aria-label,
        title, or the icon reference. Hidden buttons are excluded. Returns up to
        20, each with an interaction-tree id that click accepts directly and a
        best-effort CSS selector.
        """
        found = await self._doc_call(_FIND_BUTTONS_FN, filter_text or "") or []
        controls = [Control(**c) for c in found]
        what = f" matching '{filter_text}'" if filter_text else ""
        return ControlList(summary=f"Found {len(controls)} button(s){what}", controls=controls)

    async def find_inputs(
        self,
        filter_type: Annotated[
            str | None,
            Field(
                description="Case-insensitive substring matched against each field's input type, e.g. 'text', 'search', 'password', 'email'. Omit to list every field. Example: 'password'"
            ),
        ] = None,
    ) -> ControlList:
        """List the page's input fields with an id and a selector for each.

        Covers input, textarea, contenteditable, and [role=textbox], labelling
        each from its <label> element, its placeholder, or aria-label. Hidden
        and type=hidden fields are excluded. Returns up to 20, each with an
        interaction-tree id that type_text and fill_form accept directly.
        """
        found = await self._doc_call(_FIND_INPUTS_FN, filter_type or "") or []
        controls = [Control(**c) for c in found]
        what = f" of type '{filter_type}'" if filter_type else ""
        return ControlList(summary=f"Found {len(controls)} input(s){what}", controls=controls)
