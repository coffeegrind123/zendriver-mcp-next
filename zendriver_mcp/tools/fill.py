"""Filling one form field of any kind, with the browser's own trusted input.

fill_form (tools/forms.py) hands each field here. A field is found by selector,
tree id, or the words a person would use for it (its label), then filled the
way a person would fill that kind of control, so the page receives trusted
events and runs its own handlers:

    text, textarea, contenteditable  click to focus, select-all + delete, insertText
    checkbox, switch, radio          click it (or its label) only if the state must change
    radio / checkbox group           click the member whose label names the value
    <select>                         focus, arrow keys to the option
    <select multiple>                click, then Control-click, each wanted option
    ARIA combobox / listbox          click, type the value, click the matching option
    range                            focus, arrow keys to the value
    date, time, datetime-local       focus, type the digits in the locale's order
    file                             DOM.setFileInputFiles

Colour, month and week inputs have no trusted route (Chrome opens a native
picker), and any trusted route that did not produce the value falls back to
setting it by script: those fields come back with trusted=false, never silently.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from zendriver import cdp
from zendriver.core.connection import ProtocolException

from zendriver_mcp.dom import Handle
from zendriver_mcp.errors import (
    AmbiguousError,
    Code,
    ElementNotFoundError,
    InvalidArgumentError,
    ZendriverMCPError,
)
from zendriver_mcp.keys import send_key
from zendriver_mcp.tools import _fill_js as js

if TYPE_CHECKING:
    from zendriver_mcp.tools.base import ToolBase

TRUTHY = {"true", "yes", "on", "1", "checked", "check", "y"}
FALSY = {"false", "no", "off", "0", "unchecked", "uncheck", "n"}

# Control-click adds an option to a multi-select list; on macOS it is Command.
_ADD_TO_SELECTION = 4 if sys.platform == "darwin" else 2

# How long a combobox gets to show its options after the value is typed.
COMBOBOX_WAIT_SECONDS = 3.0
_POLL_SECONDS = 0.15

# Arrow presses allowed for a range or select before giving up on the trusted route.
_MAX_STEPS = 500

Status = Literal["filled", "missing", "ambiguous", "failed"]


class FieldSpec(BaseModel):
    """One field to fill, and what to put in it."""

    target: str | None = Field(
        default=None,
        description="Selector or tree id of the field; this or label. Example: '#email'",
    )
    label: str | None = Field(
        default=None,
        description="The words naming the field (label, placeholder, question); best match wins, ties are reported. Example: 'Email address'",
    )
    value: str | float | bool | None = Field(
        default=None,
        description="Text, an option's name, 'YYYY-MM-DD' / 'HH:MM', a number, or true/false. Example: 'Finland'",
    )
    checked: bool | None = Field(
        default=None,
        description="Checkbox, switch or radio state wanted. Example: true",
    )
    options: list[str] | None = Field(
        default=None,
        description="Multi-select or checkbox group: exactly these selected. Example: ['Email', 'SMS']",
    )
    files: list[str] | None = Field(
        default=None,
        description="File input: absolute paths on the server. Example: ['/home/user/cv.pdf']",
    )

    def name(self) -> str:
        return self.target if self.target is not None else f"label={self.label!r}"


class FieldOutcome(BaseModel):
    field: str = Field(description="The field as it was asked for: its target, or label='...'.")
    status: Status = Field(
        description="filled, missing (nothing matched), ambiguous (several matched), or failed."
    )
    kind: str | None = Field(
        default=None,
        description="What the field turned out to be: text, checkbox, radio, group, select, multiselect, combobox, listbox, range, date, time, datetime-local, month, week, color, file.",
    )
    id: int | None = Field(default=None, description="Interaction-tree id of the field.")
    trusted: bool | None = Field(
        default=None,
        description="Whether the page received trusted input (true) or the value was set by script (false).",
    )
    detail: str | None = Field(default=None, description="What was done, or why it failed.")


# ----------------------------------------------------------------------
# matching words against names (the Python twin of tier() in _fill_js.py)


def _norm(s: str) -> str:
    return re.sub(r"[\s:*]+$", "", re.sub(r"\s+", " ", str(s)).strip()).lower()


def tier(desc: str, needle: str) -> int:
    """4 exact, 3 starts the name, 2 a whole word or phrase in it, 1 anywhere in it."""
    d, n = _norm(desc), _norm(needle)
    if not d or not n:
        return 0
    if d == n:
        return 4
    if d.startswith(n) and not d[len(n) : len(n) + 1].isalnum():
        return 3
    if re.search(r"(^|[^\w])" + re.escape(n) + r"($|[^\w])", d):
        return 2
    if n in d:
        return 1
    return 0


def best_matches(needle: str, names: list[list[str]]) -> tuple[int, list[int]]:
    """The indices whose best name ranks highest for ``needle``, and that rank."""
    scores = [max((tier(x, needle) for x in alts if x), default=0) for alts in names]
    top = max(scores, default=0)
    return top, [i for i, s in enumerate(scores) if top and s == top]


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in TRUTHY:
        return True
    if s in FALSY:
        return False
    return None


# ----------------------------------------------------------------------


class Filler:
    """Fills fields for one tool call."""

    def __init__(self, tools: ToolBase) -> None:
        self.tools = tools
        self.dom = tools.dom

    @property
    def page(self) -> Any:
        return self.tools.session.page

    async def fill(self, spec: FieldSpec) -> FieldOutcome:
        out = FieldOutcome(field=spec.name(), status="failed")
        try:
            _validate(spec)
            handle = await self.locate(spec)
            out.id = await self.dom.id_for(handle)
            info = await self.tools.element_call(handle, js.INSPECT_FN)
            out.kind = info.get("kind")
            if out.kind is None:
                raise ZendriverMCPError(
                    f"<{info.get('tag')}> is not a form field.",
                    hint="Target the input, select or textarea itself, or use label.",
                )
            if info.get("disabled"):
                raise ZendriverMCPError("The field is disabled.")
            if info.get("readonly") and out.kind not in ("checkbox", "radio", "group"):
                raise ZendriverMCPError("The field is read-only.")
            trusted, detail = await self._fill_kind(out.kind, handle, info, spec)
            out.status, out.trusted, out.detail = "filled", trusted, detail
        except AmbiguousError as exc:
            out.status, out.detail = "ambiguous", str(exc)
        except ZendriverMCPError as exc:
            if exc.code in (Code.ELEMENT_NOT_FOUND, Code.STALE_ID):
                out.status = "missing"
            out.detail = str(exc) + (f" {exc.hint}" if exc.hint and out.status == "failed" else "")
        return out

    # ------------------------------------------------------------------
    # finding the field

    async def locate(self, spec: FieldSpec) -> Handle:
        if spec.target is not None:
            return await self.tools.resolve(spec.target)
        assert spec.label is not None
        best = 0
        hits: list[tuple[Any, dict[str, Any]]] = []
        total = 0
        for frame in await self.dom.frames():
            if not await self.dom.frame_shown(frame):
                continue
            try:
                root = await self.dom.root(frame)
                found = await self.dom.call(root, js.FIND_FIELD_FN, spec.label)
            except (ZendriverMCPError, ProtocolException):
                if frame.is_main:
                    raise
                continue
            t = int(found.get("tier") or 0)
            if t > best:
                best, hits, total = t, [], 0
            if t and t == best:
                hits += [(frame, h) for h in found.get("hits") or []]
                total += int(found.get("count") or 0)
        if not hits:
            raise ElementNotFoundError(
                f"label={spec.label!r}",
                hint="No field is named that. Check get_interaction_tree for the field's label, or pass target.",
            )
        if total > 1:
            listed = "; ".join(
                f"{h['label']!r} ({h['kind']}, id {h['id']}"
                + (f", frame {f.number}" if f.number else "")
                + ")"
                for f, h in hits[:6]
            )
            raise AmbiguousError(
                f"label={spec.label!r} names {total} fields equally well: {listed}."
            )
        return await self.dom.resolve(str(hits[0][1]["id"]))

    # ------------------------------------------------------------------
    # filling by kind

    async def _fill_kind(
        self, kind: str, handle: Handle, info: dict[str, Any], spec: FieldSpec
    ) -> tuple[bool, str]:
        if kind == "file":
            return await self._file(handle, spec)
        if kind in ("checkbox", "radio"):
            return await self._toggle(kind, handle, info, spec)
        if kind == "group":
            return await self._group(handle, spec)
        if kind == "select":
            return await self._select(handle, info, spec)
        if kind == "multiselect":
            return await self._multiselect(handle, info, spec)
        if kind in ("combobox", "listbox"):
            return await self._combobox(kind, handle, spec)
        if kind == "range":
            return await self._range(handle, info, spec)
        if kind in ("date", "time", "datetime-local"):
            return await self._datetime(kind, handle, spec)
        if kind in ("month", "week", "color"):
            value = _need_value(spec, kind)
            got = await self.tools.element_call(handle, js.SCRIPTED_VALUE_FN, value)
            return (
                False,
                f"set to {got!r} by script; Chrome offers no typed route for {kind} inputs",
            )
        return await self._text(handle, spec)

    async def _text(self, handle: Handle, spec: FieldSpec) -> tuple[bool, str]:
        from zendriver_mcp.tools.elements import focus_for_typing

        value = _need_value(spec, "text")
        await focus_for_typing(self.tools, handle)
        await self.tools.element_call(handle, js.CLEAR_FN)
        await self.page.send(cdp.input_.insert_text(value))
        got = (await self.tools.element_call(handle, js.INSPECT_FN)).get("value")
        if got != value:
            return True, f"typed {value!r}; the page shows {got!r}"
        return True, f"typed {value!r}"

    async def _click(self, handle: Handle) -> None:
        """A trusted click on the control, or on its label when it has no box of its own."""
        try:
            await self.dom.click(handle)
            return
        except ZendriverMCPError as exc:
            if exc.code not in (Code.ELEMENT_HIDDEN, Code.ELEMENT_OBSCURED):
                raise
            target = await self.tools.element_call(handle, js.LABEL_TARGET_FN)
            if not target:
                raise
        await self.dom.click(await self.dom.resolve(str(target["id"])))

    async def _toggle(
        self, kind: str, handle: Handle, info: dict[str, Any], spec: FieldSpec
    ) -> tuple[bool, str]:
        want = spec.checked if spec.checked is not None else _as_bool(spec.value)
        if want is None:
            raise InvalidArgumentError(
                f"A {kind} needs checked=true/false (or value true/false).",
                hint='Pass checked, e.g. {"label": "I agree", "checked": true}.',
            )
        if bool(info.get("checked")) == want:
            return True, f"already {'checked' if want else 'unchecked'}"
        if kind == "radio" and not want:
            raise InvalidArgumentError(
                "A radio button cannot be unchecked by clicking it.",
                hint="Choose another option in its group instead.",
            )
        await self._click(handle)
        now = bool((await self.tools.element_call(handle, js.INSPECT_FN)).get("checked"))
        if now != want:
            raise ZendriverMCPError(
                f"Clicked, but the {kind} is still {'checked' if now else 'unchecked'}."
            )
        return True, "checked" if want else "unchecked"

    async def _group(self, handle: Handle, spec: FieldSpec) -> tuple[bool, str]:
        members: list[dict[str, Any]] = await self.tools.element_call(handle, js.MEMBERS_FN)
        if not members:
            raise ZendriverMCPError("The group has no options to choose.")
        names = [[m["label"]] for m in members]
        if spec.options is not None:
            # Exactly these checked: a set of checkboxes.
            wanted: set[int] = set()
            for opt in spec.options:
                wanted.add(self._pick(opt, names, members))
            changed = []
            for i, m in enumerate(members):
                want = i in wanted
                if m["disabled"] or bool(m["checked"]) == want:
                    continue
                if m["kind"] == "radio" and not want:
                    continue
                await self._click(await self.dom.resolve(str(m["id"])))
                changed.append(m["label"])
            after = await self.tools.element_call(handle, js.MEMBERS_FN)
            on = [m["label"] for m in after if m["checked"]]
            if set(on) != {members[i]["label"] for i in wanted}:
                raise ZendriverMCPError(f"Clicked {changed}, but the checked options are now {on}.")
            return True, f"checked exactly {on}"
        if spec.value is None:
            raise InvalidArgumentError(
                "A group of options needs value (one option) or options (several).",
                hint='e.g. {"label": "Size", "value": "Large"}.',
            )
        i = self._pick(str(spec.value), names, members)
        member = members[i]
        if member["checked"]:
            return True, f"{member['label']!r} already chosen"
        await self._click(await self.dom.resolve(str(member["id"])))
        after = await self.tools.element_call(handle, js.MEMBERS_FN)
        if not any(m["checked"] and m["label"] == member["label"] for m in after):
            raise ZendriverMCPError(f"Clicked {member['label']!r}, but it is not chosen.")
        return True, f"chose {member['label']!r}"

    def _pick(self, needle: str, names: list[list[str]], items: list[dict[str, Any]]) -> int:
        """The one item ``needle`` names, refusing ties, misses and disabled items."""
        top, idx = best_matches(needle, names)
        if not idx:
            choices = ", ".join(repr(n[0]) for n in names[:20])
            raise ZendriverMCPError(
                f"No option matches {needle!r}.", hint=f"The options are: {choices}."
            )
        enabled = [i for i in idx if not items[i].get("disabled")]
        if not enabled:
            raise ZendriverMCPError(f"Option {names[idx[0]][0]!r} is disabled.")
        if len(enabled) > 1:
            listed = ", ".join(repr(names[i][0]) for i in enabled[:8])
            raise AmbiguousError(f"{needle!r} names {len(enabled)} options equally well: {listed}.")
        return enabled[0]

    async def _select(
        self, handle: Handle, info: dict[str, Any], spec: FieldSpec
    ) -> tuple[bool, str]:
        value = _need_value(spec, "select")
        options: list[dict[str, Any]] = info.get("options") or []
        names = [[o["text"], o["value"]] for o in options]
        target = self._pick(value, names, options)
        label = options[target]["text"]
        if info.get("selectedIndex") == target:
            return True, f"{label!r} already selected"
        # Arrow keys on a focused, closed <select> change it the way a person's
        # keyboard does: trusted input and change per step, disabled options skipped.
        await self.dom.focus(handle)
        current = int(info.get("selectedIndex", -1))
        for _ in range(min(len(options) + 2, _MAX_STEPS)):
            if current == target:
                break
            await send_key(self.page, "ArrowDown" if target > current or current < 0 else "ArrowUp")
            now = int(
                (await self.tools.element_call(handle, js.INSPECT_FN)).get("selectedIndex", -1)
            )
            if now == current:
                break  # the key did nothing here; fall back below
            current = now
        if current == target:
            return True, f"selected {label!r}"
        await self.tools.element_call(handle, js.SCRIPTED_SELECT_FN, [target])
        return False, f"selected {label!r} by script; arrow keys did not reach it"

    async def _multiselect(
        self, handle: Handle, info: dict[str, Any], spec: FieldSpec
    ) -> tuple[bool, str]:
        wanted_names = (
            spec.options if spec.options is not None else [_need_value(spec, "multiselect")]
        )
        options: list[dict[str, Any]] = await self.tools.element_call(handle, js.SELECT_OPTIONS_FN)
        names = [[o["text"], o["value"]] for o in options]
        wanted = sorted({self._pick(w, names, options) for w in wanted_names})
        labels = [options[i]["text"] for i in wanted]
        if {i for i, o in enumerate(options) if o["selected"]} == set(wanted):
            return True, f"{labels} already selected"
        trusted = True
        try:
            # A plain click selects one option and clears the rest; each further
            # one is added with Control held, as a person does it.
            for n, i in enumerate(wanted):
                opt = await self.dom.resolve(str(options[i]["id"]))
                await self.dom.click(opt, modifiers=0 if n == 0 else _ADD_TO_SELECTION)
        except ZendriverMCPError:
            trusted = False
        after = await self.tools.element_call(handle, js.SELECT_OPTIONS_FN)
        if {i for i, o in enumerate(after) if o["selected"]} != set(wanted):
            await self.tools.element_call(handle, js.SCRIPTED_SELECT_FN, wanted)
            return False, f"selected {labels} by script; clicking the options did not"
        return trusted, f"selected {labels}"

    async def _combobox(self, kind: str, handle: Handle, spec: FieldSpec) -> tuple[bool, str]:
        value = _need_value(spec, kind)
        await self.dom.click(handle)
        typed = False
        info = await self.tools.element_call(handle, js.INSPECT_FN)
        if kind == "combobox" and info.get("tag") == "input" and not info.get("readonly"):
            await self.tools.element_call(handle, js.CLEAR_FN)
            await self.page.send(cdp.input_.insert_text(value))
            typed = True
        deadline = asyncio.get_running_loop().time() + COMBOBOX_WAIT_SECONDS
        options: list[dict[str, Any]] = []
        while True:
            options = await self.tools.element_call(handle, js.OPTIONS_FN)
            top, idx = best_matches(value, [[o["text"]] for o in options])
            if idx or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(_POLL_SECONDS)
        if not options:
            if typed:
                return True, f"typed {value!r}; no list of options appeared"
            raise ZendriverMCPError(
                f"No options appeared after clicking {handle.label}.",
                hint="Open it with click and inspect get_interaction_tree for its options.",
            )
        i = self._pick(value, [[o["text"]] for o in options], options)
        chosen = options[i]["text"]
        await self.dom.click(await self.dom.resolve(str(options[i]["id"])))
        return True, f"chose {chosen!r}" + (f" after typing {value!r}" if typed else "")

    async def _range(
        self, handle: Handle, info: dict[str, Any], spec: FieldSpec
    ) -> tuple[bool, str]:
        raw = _need_value(spec, "range")
        try:
            target = float(raw)
        except ValueError as exc:
            raise InvalidArgumentError(f"A range needs a number, not {raw!r}.") from exc
        lo, hi = float(info.get("min", 0)), float(info.get("max", 100))
        if not lo <= target <= hi:
            raise InvalidArgumentError(f"{target:g} is outside the range {lo:g}-{hi:g}.")
        await self.dom.focus(handle)
        current = float(info.get("number", lo))
        for _ in range(_MAX_STEPS):
            if abs(current - target) < 1e-9:
                break
            await send_key(self.page, "ArrowRight" if target > current else "ArrowLeft")
            now = float(
                (await self.tools.element_call(handle, js.INSPECT_FN)).get("number", current)
            )
            if now == current or (now - target) * (current - target) < 0:
                current = now
                break  # stuck, or stepped past a value the step cannot hit
            current = now
        if abs(current - target) < 1e-9:
            return True, f"set to {target:g}"
        if info.get("tag") != "input":
            raise ZendriverMCPError(f"The slider stopped at {current:g}, not {target:g}.")
        got = await self.tools.element_call(handle, js.SCRIPTED_VALUE_FN, f"{target:g}")
        return False, f"set to {got} by script; arrow keys stopped at {current:g}"

    async def _datetime(self, kind: str, handle: Handle, spec: FieldSpec) -> tuple[bool, str]:
        value = _need_value(spec, kind)
        keys = await self._date_keys(kind, handle, value)
        await self.dom.focus(handle)
        for k in keys:
            await send_key(self.page, k)
        got = (await self.tools.element_call(handle, js.INSPECT_FN)).get("value")
        if not _same_time(got, value) and kind in ("time", "datetime-local"):
            # A 12-hour field leaves AM/PM unset for hours that do not imply it
            # (07 could be either); its letter sets it. A 24-hour field has no
            # such segment and the field already took the value above.
            hour = int(re.findall(r"T?(\d{2}):\d{2}", value)[-1])
            await send_key(self.page, "A" if hour < 12 else "P")
            got = (await self.tools.element_call(handle, js.INSPECT_FN)).get("value")
        if _same_time(got, value):
            return True, f"typed {value!r}"
        got = await self.tools.element_call(handle, js.SCRIPTED_VALUE_FN, value)
        if not _same_time(got, value):
            raise ZendriverMCPError(f"The field rejected {value!r}.", hint=_DATE_HINT.get(kind))
        return False, f"set to {got!r} by script; typing the digits did not take"

    async def _date_keys(self, kind: str, handle: Handle, value: str) -> list[str]:
        """The keys to type into a focused date or time field, segment by segment.

        Chrome moves to the next segment once one is full, except after the
        year, which takes up to six digits: when more follows it, ArrowRight
        moves on explicitly.
        """
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?", value)
        t = re.fullmatch(r"(\d{2}):(\d{2})(?::\d{2})?", value)
        if kind == "time":
            if not t:
                raise InvalidArgumentError(f"A time needs 'HH:MM', not {value!r}.")
            return list(t.group(1) + t.group(2))
        if not m or (kind == "datetime-local") != (m.group(4) is not None):
            raise InvalidArgumentError(f"Expected {_DATE_HINT[kind]}, not {value!r}.")
        parts = {"year": m.group(1), "month": m.group(2), "day": m.group(3)}
        order = await self.tools.element_call(handle, js.DATE_ORDER_FN)
        if sorted(order or []) != ["day", "month", "year"]:
            order = ["month", "day", "year"]
        segments = [parts[p] for p in order]
        if kind == "datetime-local":
            segments += [m.group(4), m.group(5)]
        keys: list[str] = []
        for i, seg in enumerate(segments):
            keys += list(seg)
            if i < len(order) and order[i] == "year" and i < len(segments) - 1:
                keys.append("ArrowRight")
        return keys

    async def _file(self, handle: Handle, spec: FieldSpec) -> tuple[bool, str]:
        paths = spec.files if spec.files is not None else ([str(spec.value)] if spec.value else [])
        if not paths:
            raise InvalidArgumentError("A file input needs files (absolute paths).")
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            raise InvalidArgumentError(
                f"No such file: {', '.join(missing)}",
                hint="Pass absolute paths that exist on the machine running this server.",
            )
        await self.dom.set_files(handle, [os.path.abspath(p) for p in paths])
        return True, "attached " + ", ".join(os.path.basename(p) for p in paths)


_DATE_HINT = {
    "date": "'YYYY-MM-DD'",
    "time": "'HH:MM'",
    "datetime-local": "'YYYY-MM-DDTHH:MM'",
}


def _same_time(got: Any, want: str) -> bool:
    """A date or time value equal to ``want``, ignoring seconds Chrome may add."""
    if not isinstance(got, str) or not got:
        return False
    return (
        got == want
        or got.startswith(want)
        and re.fullmatch(r":\d{2}(\.\d+)?", got[len(want) :]) is not None
    )


def _need_value(spec: FieldSpec, kind: str) -> str:
    if spec.value is None or isinstance(spec.value, bool):
        raise InvalidArgumentError(
            f"A {kind} field needs a value.",
            hint='e.g. {"label": "Country", "value": "Finland"}.',
        )
    if isinstance(spec.value, float) and spec.value.is_integer():
        return str(int(spec.value))
    return str(spec.value)


def _validate(spec: FieldSpec) -> None:
    if (spec.target is None) == (spec.label is None):
        raise InvalidArgumentError(
            "Each field needs exactly one of target or label.",
            hint='e.g. {"label": "Email", "value": "a@b.c"} or {"target": "#email", "value": "a@b.c"}.',
        )
    given = [k for k in ("value", "checked", "options", "files") if getattr(spec, k) is not None]
    if not given:
        raise InvalidArgumentError(
            f"{spec.name()}: say what to fill in, with value, checked, options or files."
        )
