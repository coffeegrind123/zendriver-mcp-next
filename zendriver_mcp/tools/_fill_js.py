"""Isolated-world helpers for fill_form (zendriver_mcp/tools/fill.py).

Every constant below is a FUNCTION DECLARATION for ``ToolBase.element_call``:
``function(el, ...args)``, run in our isolated world. They read the DOM and
number elements with ``globalThis.__zd.idOf``; none of them writes to the page
except the two ``SCRIPTED_*`` fallbacks, which fill_form reports as untrusted.
"""

from __future__ import annotations

# Shared by the functions below; spliced in at the top of each body.
_HELPERS = r"""
  const norm = (s) => String(s || "").replace(/\s+/g, " ").trim()
    .replace(/[\s:*]+$/, "").toLowerCase();
  const textOf = (n) => (n ? (n.innerText || n.textContent || "") : "").replace(/\s+/g, " ").trim();
  const byId = (el, id) => {
    const r = el.getRootNode();
    return (r.getElementById && r.getElementById(id)) || document.getElementById(id);
  };
  const labelText = (l) => {
    const c = l.cloneNode(true);
    c.querySelectorAll("input,textarea,select,button").forEach((x) => x.remove());
    return textOf(c);
  };
  function accName(el) {
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.trim().split(/\s+/).map((i) => byId(el, i)).filter(Boolean)
        .map(textOf).filter(Boolean).join(" ");
      if (t) return t;
    }
    const al = el.getAttribute("aria-label");
    if (al && al.trim()) return al.trim();
    for (const l of el.labels || []) {
      const t = labelText(l);
      if (t) return t;
    }
    return "";
  }
  // querySelectorAll across the light DOM and every open shadow root.
  function deepAll(root, sel, out) {
    out = out || [];
    for (const el of root.querySelectorAll(sel)) out.push(el);
    for (const el of root.querySelectorAll("*")) if (el.shadowRoot) deepAll(el.shadowRoot, sel, out);
    return out;
  }
  function rendered(el) {
    // A display:contents box (a layout wrapper) has no box of its own but
    // renders its children; checkVisibility() reports it as hidden.
    if (getComputedStyle(el).display === "contents") {
      return [...el.children].some(rendered);
    }
    if (el.checkVisibility && !el.checkVisibility({ checkVisibilityCSS: true })) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  // A control a person can reach: shown, or a custom checkbox whose <input>
  // is invisible under a styled <label>, or a file input behind a button.
  function reachable(el) {
    if (el.closest("[inert]")) return false;
    if (rendered(el)) return true;
    if (el.type === "file") return true;
    for (const l of el.labels || []) if (rendered(l)) return true;
    return false;
  }
  const DATE_TYPES = ["date", "time", "datetime-local", "month", "week"];
  function kindOf(el) {
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute("role") || "").toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "fieldset" || role === "radiogroup" || role === "group") return "group";
    if (tag === "select") return el.multiple ? "multiselect" : "select";
    if (tag === "input") {
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "file") return "file";
      if (type === "range") return "range";
      if (DATE_TYPES.includes(type)) return type;
      if (type === "color") return "color";
      if (role === "combobox" || el.closest("[role=combobox]")) return "combobox";
      return "text";
    }
    if (tag === "textarea") return "text";
    if (["checkbox", "switch", "menuitemcheckbox"].includes(role)) return "checkbox";
    if (role === "radio" || role === "menuitemradio") return "radio";
    if (role === "combobox") return "combobox";
    if (role === "listbox") return "listbox";
    if (role === "slider") return "range";
    if (el.isContentEditable || ["textbox", "searchbox", "spinbutton"].includes(role)) return "text";
    return null;
  }
  const MEMBER_SEL = "input[type=radio],input[type=checkbox],[role=radio],[role=checkbox]," +
    "[role=switch],[role=menuitemradio],[role=menuitemcheckbox]";
  const members = (g) => deepAll(g, MEMBER_SEL).filter((m) => !m.closest("[inert]"));
  function groupName(g) {
    const n = accName(g);
    if (n) return n;
    if (g.tagName === "FIELDSET") {
      const lg = g.querySelector(":scope > legend");
      if (lg) return textOf(lg);
    }
    const h = g.querySelector("[role=heading],h1,h2,h3,h4,h5,h6,legend");
    return h ? textOf(h) : "";
  }
  // Every name a person might use for a control, best first.
  function names(el, kind) {
    const out = [accName(el)];
    const role = el.getAttribute("role");
    if (role && ["checkbox", "radio", "switch", "option", "menuitemcheckbox", "menuitemradio"].includes(role)) {
      const t = textOf(el);
      if (t.length < 120) out.push(t);
    }
    if (!out[0]) {
      const wrap = el.closest("label");
      if (wrap) out.push(labelText(wrap));
    }
    const cb = el.closest("[role=combobox]");
    if (cb && cb !== el) out.push(accName(cb));
    // A radio's name attribute names its group, not the option.
    const attrs = kind === "radio" ? ["placeholder", "aria-placeholder", "title", "id"]
      : ["placeholder", "aria-placeholder", "title", "name", "id"];
    for (const a of attrs) out.push(el.getAttribute(a));
    if (kind !== "radio" && kind !== "checkbox") {
      const q = el.parentElement && el.parentElement.closest("fieldset,[role=group],[role=radiogroup],[role=listitem]");
      if (q) out.push(groupName(q));
    }
    return out.filter(Boolean);
  }
  const WORD = /[\p{L}\p{N}]/u;
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // 4 exact, 3 starts the name, 2 a whole word or phrase in it, 1 anywhere in it.
  function tier(desc, needle) {
    const d = norm(desc), n = norm(needle);
    if (!d || !n) return 0;
    if (d === n) return 4;
    if (d.startsWith(n) && !WORD.test(d.charAt(n.length))) return 3;
    if (new RegExp("(^|[^\\p{L}\\p{N}])" + esc(n) + "($|[^\\p{L}\\p{N}])", "u").test(d)) return 2;
    if (d.includes(n)) return 1;
    return 0;
  }
"""


def _fn(params: str, body: str) -> str:
    return f"function({params}) {{\n{_HELPERS}\n{body}\n}}"


# The fields in the document best named by `needle`: the top tier's matches.
FIND_FIELD_FN = _fn(
    "_root, needle",
    r"""
  const S = globalThis.__zd;
  const SEL = "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset])" +
    ":not([type=image]),textarea,select,[contenteditable=''],[contenteditable=true]," +
    "[role=textbox],[role=searchbox],[role=combobox],[role=spinbutton],[role=slider]," +
    "[role=switch],[role=checkbox],[role=radio],[role=listbox],[role=menuitemcheckbox]," +
    "[role=menuitemradio],fieldset,[role=radiogroup],[role=group]";
  let best = 0, hits = [];
  const seen = new Set();
  for (const el of deepAll(document, SEL)) {
    if (seen.has(el)) continue;
    seen.add(el);
    const kind = kindOf(el);
    if (!kind) continue;
    if (kind === "group") {
      if (el.closest("[inert]") || !members(el).length || !rendered(el)) continue;
    } else if (!reachable(el)) {
      continue;
    }
    // A combobox wrapper around its own text input is one field: keep the input.
    if (el.getAttribute("role") === "combobox" && el.tagName !== "INPUT" && el.querySelector("input")) continue;
    const descs = kind === "group" ? [groupName(el)] : names(el, kind);
    let t = 0, matched = "";
    for (const d of descs) {
      const x = tier(d, needle);
      if (x > t) { t = x; matched = d; }
    }
    if (!t) continue;
    if (t > best) { best = t; hits = []; }
    if (t === best) hits.push({ el, id: S.idOf(el), kind, label: String(matched).replace(/\s+/g, " ").trim().slice(0, 80) });
  }
  // A group that matches stands for its own options: "Size" is the group,
  // not also each radio inside it.
  const groups = hits.filter((h) => h.kind === "group").map((h) => h.el);
  hits = hits.filter((h) => !groups.some((g) => g !== h.el && g.contains(h.el)));
  return { tier: best, count: hits.length, hits: hits.slice(0, 10).map(({ el, ...h }) => h) };
""",
)

# What kind of control `el` is and its current state.
INSPECT_FN = _fn(
    "el",
    r"""
  const kind = kindOf(el);
  const o = {
    kind,
    tag: el.tagName.toLowerCase(),
    disabled: !!(el.matches && el.matches(":disabled")) || !!el.closest("[aria-disabled=true]"),
    readonly: !!el.readOnly || el.getAttribute("aria-readonly") === "true",
    visible: rendered(el),
  };
  if (kind === "checkbox" || kind === "radio") {
    o.checked = el.tagName === "INPUT" ? el.checked : el.getAttribute("aria-checked") === "true";
  }
  if (kind === "select" || kind === "multiselect") {
    o.options = [...el.options].map((op) => ({
      text: (op.text || op.label || "").replace(/\s+/g, " ").trim(),
      value: op.value,
      disabled: op.disabled || (op.parentElement && op.parentElement.tagName === "OPTGROUP" && op.parentElement.disabled),
      selected: op.selected,
    }));
    o.selectedIndex = el.selectedIndex;
  }
  if (kind === "range") {
    if (el.tagName === "INPUT") {
      o.min = parseFloat(el.min || "0");
      o.max = parseFloat(el.max || "100");
      o.step = el.step === "any" || !el.step ? 1 : parseFloat(el.step);
      o.number = parseFloat(el.value);
    } else {
      o.min = parseFloat(el.getAttribute("aria-valuemin") || "0");
      o.max = parseFloat(el.getAttribute("aria-valuemax") || "100");
      o.number = parseFloat(el.getAttribute("aria-valuenow"));
    }
  }
  if (typeof el.value === "string" && el.tagName !== "SELECT") o.value = el.value;
  else if (el.isContentEditable) o.value = textOf(el);
  else o.value = textOf(el).slice(0, 200);
  return o;
""",
)

# The radios and checkboxes of a group, with their names and states.
MEMBERS_FN = _fn(
    "el",
    r"""
  const S = globalThis.__zd;
  return members(el).filter(reachable).map((m) => ({
    id: S.idOf(m),
    kind: kindOf(m),
    label: (names(m, kindOf(m))[0] || "").replace(/\s+/g, " ").trim().slice(0, 120),
    checked: m.tagName === "INPUT" ? m.checked : m.getAttribute("aria-checked") === "true",
    disabled: !!(m.matches && m.matches(":disabled")) || m.getAttribute("aria-disabled") === "true",
  }));
""",
)

# The options of a native <select>, each numbered so it can be clicked.
SELECT_OPTIONS_FN = _fn(
    "el",
    r"""
  const S = globalThis.__zd;
  return [...el.options].map((op) => ({
    id: S.idOf(op),
    text: (op.text || op.label || "").replace(/\s+/g, " ").trim(),
    value: op.value,
    disabled: op.disabled,
    selected: op.selected,
  }));
""",
)

# The visible options of a combobox's or listbox's popup: those it names with
# aria-controls / aria-owns first, otherwise any shown [role=option].
OPTIONS_FN = _fn(
    "el",
    r"""
  const S = globalThis.__zd;
  const scopes = [];
  if (el.getAttribute("role") === "listbox") scopes.push(el);
  for (const n of [el, el.closest("[role=combobox]")]) {
    if (!n) continue;
    const ids = ((n.getAttribute("aria-controls") || "") + " " + (n.getAttribute("aria-owns") || "")).trim();
    for (const id of ids ? ids.split(/\s+/) : []) {
      const t = byId(el, id);
      if (t) scopes.push(t);
    }
  }
  let opts = [];
  for (const s of new Set(scopes)) opts.push(...deepAll(s, "[role=option]"));
  if (!opts.some(rendered)) opts = deepAll(document, "[role=option]");
  return [...new Set(opts)].filter(rendered).slice(0, 100).map((o) => ({
    id: S.idOf(o),
    text: (accName(o) || textOf(o)).slice(0, 120),
    disabled: o.getAttribute("aria-disabled") === "true",
  }));
""",
)

# A shown <label> to click for a control that has no box of its own.
LABEL_TARGET_FN = _fn(
    "el",
    r"""
  const S = globalThis.__zd;
  for (const l of el.labels || []) if (rendered(l)) return { id: S.idOf(l) };
  const wrap = el.closest("label");
  return wrap && rendered(wrap) ? { id: S.idOf(wrap) } : null;
""",
)

# Empty a text control by selecting its contents and deleting them, which the
# page sees as an ordinary delete.
CLEAR_FN = r"""function(el) {
  if (typeof el.select === "function") { el.select(); }
  else {
    const r = document.createRange(); r.selectNodeContents(el);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  }
  document.execCommand("delete");
}"""

# The order Chrome's date field shows its parts in, for typing digits into it.
DATE_ORDER_FN = r"""function(_el) {
  return new Intl.DateTimeFormat(navigator.language).formatToParts(new Date(2001, 1, 3))
    .filter((p) => ["year", "month", "day"].includes(p.type)).map((p) => p.type);
}"""

# Untrusted fallbacks: set the value through the element's own native setter
# (so a framework's value tracker sees a change) and fire input and change.
# Used only where no trusted route exists (colour, month, week) or the trusted
# one did not take, and always reported as trusted:false.
SCRIPTED_VALUE_FN = r"""function(el, v) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v);
  el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return el.value;
}"""

SCRIPTED_SELECT_FN = r"""function(el, indices) {
  [...el.options].forEach((o, i) => { o.selected = indices.includes(i); });
  el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return [...el.options].map((o, i) => (o.selected ? i : -1)).filter((i) => i >= 0);
}"""
