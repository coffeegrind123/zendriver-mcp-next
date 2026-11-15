"""An opt-in guard against actions that submit a form or commit something.

Off unless ``ZENDRIVER_MCP_SUBMIT_GUARD=1``. When on, the input tools refuse
(code ``submit_blocked``) a click, key press or form submission that would:

- submit a form: a click on its submit button, Enter in a field where HTML's
  implicit submission applies (the form's first submit button, including one
  attached from outside with ``form=``; or no submit button and a single
  text-like field), or submit_form; search forms are exempt;
- press a control whose label reads as committing something: send, pay, buy,
  purchase, checkout, place order, confirm, delete, publish, post, transfer,
  donate, submit;
- press Control/Meta+Enter in a textarea or rich editor, which sends in most
  mail and chat editors.

``allow_next_submit`` lets exactly one such action through. Most MCP clients
ask the person before running it, which makes it the human approval.

Every check is a read of the DOM in our isolated world: nothing in the page is
patched or wrapped (no ``HTMLFormElement.prototype.submit`` proxy, no submit
listeners), so the guard cannot be detected and cannot break the page.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from zendriver_mcp.errors import Code, ZendriverMCPError

if TYPE_CHECKING:
    from zendriver_mcp.dom import Handle
    from zendriver_mcp.tools.base import ToolBase

ENV = "ZENDRIVER_MCP_SUBMIT_GUARD"


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() in ("1", "true", "yes", "on")


class SubmitBlockedError(ZendriverMCPError):
    code = Code.SUBMIT_BLOCKED
    hint = (
        "The submit guard is on. If the person has approved this, call allow_next_submit "
        "and repeat the action; otherwise stop and ask them."
    )


_HELPERS = r"""
  const textOf = (n) => (n ? (n.innerText || n.textContent || "") : "").replace(/\s+/g, " ").trim();
  const byId = (el, id) => {
    const r = el.getRootNode();
    return (r.getElementById && r.getElementById(id)) || document.getElementById(id);
  };
  function nameOf(el) {
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.trim().split(/\s+/).map((i) => byId(el, i)).filter(Boolean).map(textOf).join(" ");
      if (t) return t;
    }
    const al = el.getAttribute("aria-label");
    if (al && al.trim()) return al.trim();
    if (el.tagName === "INPUT") return el.value || el.alt || el.title || "";
    const t = textOf(el);
    if (t) return t;
    const img = el.querySelector && el.querySelector("img[alt],svg title");
    return (img && (img.alt || img.textContent)) || el.title || "";
  }
  // Labels that commit something a person would want to approve first.
  const DANGER = /\b(submit|send|pay|buy|purchase|checkout|check out|place order|order now|confirm|delete|publish|post|transfer|donate)\b/i;
  // Form submissions that only move or look: search, steps of a wizard.
  const HARMLESS = /\b(search|find|filter|next|continue|back|previous)\b/i;
  function searchForm(form, submitter) {
    if (form.getAttribute("role") === "search" || form.closest("[role=search],search")) return true;
    if (form.querySelector("input[type=search]")) return true;
    return !!(submitter && HARMLESS.test(nameOf(submitter)) && !DANGER.test(nameOf(submitter)));
  }
  const describe = (t) => {
    const n = nameOf(t).slice(0, 40);
    return "<" + t.tagName.toLowerCase() + (n ? ' "' + n + '"' : "") + ">";
  };
  // What activating `el` does, if it is something to approve; else null.
  function verdictFor(el) {
    let t = el;
    for (let n = el; n; n = n.parentElement || (n.getRootNode() instanceof ShadowRoot ? n.getRootNode().host : null)) {
      if (n.matches && n.matches("button,input[type=submit],input[type=image],input[type=button],[role=button],a[href],[role=link],[role=menuitem],label")) {
        t = n;
        break;
      }
    }
    if (t.tagName === "LABEL" && t.control) t = t.control;
    const submits = (t.tagName === "BUTTON" && t.type === "submit" && t.form) ||
      (t.tagName === "INPUT" && (t.type === "submit" || t.type === "image") && t.form);
    if (submits && !searchForm(t.form, t)) return { what: describe(t), why: "it submits a form" };
    const name = nameOf(t);
    if (DANGER.test(name)) return { what: describe(t), why: "its label reads as committing an action" };
    return null;
  }
  const BLOCKING = new Set(["text", "search", "url", "tel", "email", "password", "date", "month",
    "week", "time", "datetime-local", "number"]);
  const SUBMITTER = (e) => (e.tagName === "BUTTON" && e.type === "submit") ||
    (e.tagName === "INPUT" && (e.type === "submit" || e.type === "image"));
"""

# A click on `el`.
CLICK_FN = "function(el) {" + _HELPERS + " return verdictFor(el); }"

# A key on focused `el`: Enter or Space on a button activates it; Enter in a
# field may submit its form (HTML implicit submission); Control/Meta+Enter in
# a textarea or rich editor sends in most mail and chat editors.
KEY_FN = (
    "function(el, key, chord) {"
    + _HELPERS
    + r"""
  const activates = el.matches("button,input[type=submit],input[type=image],input[type=button],[role=button],a[href],[role=link]");
  if (activates && (key === "Enter" || key === " " || key === "Space")) return verdictFor(el);
  if (key !== "Enter") return null;
  if (chord && (el.tagName === "TEXTAREA" || el.isContentEditable)) {
    return { what: describe(el), why: "Control/Meta+Enter sends in most editors" };
  }
  if (el.tagName !== "INPUT" || !el.form) return null;
  const form = el.form;
  const def = [...form.elements].find(SUBMITTER);
  let submits;
  if (def) submits = !def.disabled;
  else submits = [...form.elements].filter((e) => e.tagName === "INPUT" && BLOCKING.has(e.type)).length <= 1;
  if (!submits || searchForm(form, def)) return null;
  return { what: def ? describe(def) : describe(form), why: "Enter in this field submits its form" };
}"""
)

# submit_form on `form`.
FORM_FN = (
    "function(form) {"
    + _HELPERS
    + r"""
  const def = [...form.elements].find(SUBMITTER);
  if (searchForm(form, def)) return null;
  return { what: describe(def || form), why: "submit_form submits it" };
}"""
)


async def _decide(tools: ToolBase, verdict: Any, action: str) -> None:
    if not verdict:
        return
    session = tools.session
    if getattr(session, "submit_allowance", False):
        session.submit_allowance = False  # one approval, one action
        return
    raise SubmitBlockedError(f"Blocked {action} on {verdict.get('what')}: {verdict.get('why')}.")


async def check_click(tools: ToolBase, handle: Handle) -> None:
    if enabled():
        await _decide(tools, await tools.element_call(handle, CLICK_FN), "a click")


async def check_key(tools: ToolBase, key: str, chord: bool, handle: Handle | None) -> None:
    if not enabled() or key not in ("Enter", " ", "Space"):
        return
    target = handle or await tools.dom.focused()
    if target is None:
        return
    await _decide(tools, await tools.element_call(target, KEY_FN, key, chord), f"pressing {key}")


async def check_form(tools: ToolBase, form: Handle) -> None:
    if enabled():
        await _decide(tools, await tools.element_call(form, FORM_FN), "submit_form")


async def check_point(tools: ToolBase, x: float, y: float) -> None:
    """A click at tab coordinates: judged by the element the browser reports there."""
    if not enabled():
        return
    handle = await tools.dom.at_point(x, y)
    if handle is not None:
        await check_click(tools, handle)
