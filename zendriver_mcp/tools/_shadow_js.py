"""Shared JavaScript snippets for shadow-DOM-aware queries and clicks.

Modern design systems (bunq, NS, bol.com, etc.) nest real interactive
elements several shadow-root deep inside custom elements like
``<nes-selectable-radio>`` -> shadowRoot -> ``<nes-selectable>`` ->
shadowRoot -> ``[role="radio"]``. A plain ``document.querySelector`` or
even a one-level ``shadowRoot.querySelector`` misses these. These
helpers recursively pierce every open shadow root to FIND targets; the
clicking is done by trusted CDP input on the Python side.
"""

from __future__ import annotations

# Roles and tag names that are "real" interactive targets. Broader than a
# pure button list - radios, checkboxes, menuitems, tabs, etc. all live
# inside design-system components and need to be clicked to do anything.
_CLICKABLE_ROLES = (
    "button",
    "link",
    "checkbox",
    "radio",
    "switch",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "tab",
    "option",
    "treeitem",
)


_CLICKABLE_ROLES_JS = "[" + ",".join(f'"{r}"' for r in _CLICKABLE_ROLES) + "]"


# Walks the light DOM + every open shadow root and invokes ``cb`` on each
# element. Defined at module scope so every Python tool that evaluates JS
# can prefix this snippet and then use ``walkAll(root, fn)`` freely.
WALK_ALL_JS = """
function walkAll(root, cb) {
  const doc = root.ownerDocument || (root.nodeType === 9 ? root : null);
  const base = doc ? doc.createTreeWalker : null;
  // Some shadow roots don't expose ownerDocument; fall back to document.
  const w = (doc || document).createTreeWalker(root, NodeFilter.SHOW_ELEMENT, null);
  let n = w.currentNode;
  while (n) {
    cb(n);
    if (n.shadowRoot) walkAll(n.shadowRoot, cb);
    n = w.nextNode();
  }
}
"""


# Recursively look for an interactive element inside a host, crossing any
# number of shadow roots. Falls back to null when the host is a leaf.
FIND_INNER_CLICKABLE_JS = f"""
const CLICKABLE_ROLES = {_CLICKABLE_ROLES_JS};

function isClickableEl(el) {{
  if (!el || el.nodeType !== 1) return false;
  const tag = el.tagName.toLowerCase();
  if (["button", "a", "input", "select", "textarea", "summary"].includes(tag)) return true;
  const role = el.getAttribute && el.getAttribute("role");
  if (role && CLICKABLE_ROLES.includes(role)) return true;
  if (el.getAttribute && el.getAttribute("contenteditable") === "true") return true;
  if (el.onclick || (el.getAttribute && el.getAttribute("onclick"))) return true;
  const cs = getComputedStyle(el);
  if (cs && cs.cursor === "pointer") return true;
  return false;
}}

function findInnerClickable(host, maxDepth) {{
  if (!host || maxDepth < 0) return null;
  if (isClickableEl(host) && host !== host.ownerDocument) {{
    // The host itself qualifies; no need to dive deeper.
    return host;
  }}
  const root = host.shadowRoot;
  if (!root) return null;
  // Breadth-first over the shadow DOM tree so we pick the topmost
  // clickable rather than the deepest descendant.
  const queue = Array.from(root.querySelectorAll("*"));
  for (const el of queue) {{
    if (isClickableEl(el)) return el;
  }}
  // Nothing at this level - recurse into nested custom-element hosts.
  for (const el of queue) {{
    if (el.shadowRoot) {{
      const hit = findInnerClickable(el, maxDepth - 1);
      if (hit) return hit;
    }}
  }}
  return null;
}}
"""


# Every function below is a FUNCTION DECLARATION for ``ToolBase.element_call``:
# it runs in the isolated world (zendriver_mcp/dom.py) with the element as its first
# argument and the caller's values as the rest, passed as CDP values, so nothing
# is ever interpolated into the source. None of them dispatch events: they FIND
# a target and return its walker id (``globalThis.__zd.idOf``), and the Python
# side clicks it with trusted CDP input. Synthetic ``dispatchEvent`` clicks are
# ``isTrusted:false``, which is the first thing bot detection checks.

# Find the tightest interactive element whose text contains ``target``, across
# light and shadow DOM. Called with the document root as the element.
FIND_BY_TEXT_FN = (
    "function(_root, target) {\n"
    + WALK_ALL_JS
    + FIND_INNER_CLICKABLE_JS
    + """
  const needle = (target || "").trim().toLowerCase();
  if (!needle) return { ok: false, reason: "empty_text" };
  const candidates = [];
  walkAll(document, (el) => {
    const text = (el.innerText || el.textContent || "").trim();
    if (!text || !text.toLowerCase().includes(needle)) return;
    candidates.push(el);
  });
  if (candidates.length === 0) return { ok: false, reason: "no_match" };

  // Prefer the deepest, smallest match. A match on <body> also matches
  // every descendant; the tightest wrapper is almost always what the
  // caller meant.
  candidates.sort((a, b) =>
    (a.innerText || a.textContent || "").length - (b.innerText || b.textContent || "").length);

  // Pick the first clickable candidate, itself or via a climb that crosses
  // shadow boundaries.
  let pick = null;
  for (const el of candidates) {
    if (isClickableEl(el)) { pick = el; break; }
    let p = el.parentNode;
    while (p) {
      if (p.host) p = p.host;
      if (p && p.nodeType === 1 && isClickableEl(p)) { pick = p; break; }
      p = p && p.parentNode;
    }
    if (pick) break;
  }
  if (!pick) pick = candidates[0];

  // Normalize to the nearest real navigable ancestor: a <span>/icon inside
  // an <a> often qualifies via cursor:pointer, but the <a> is what navigates.
  if (pick && typeof pick.closest === "function") {
    const navigable = pick.closest("a, button, [role='link'], [role='button']");
    if (navigable) pick = navigable;
  }

  // A custom element host: dive into its shadow chain for the real leaf
  // (the inner <button> of a <nes-button>).
  const inner = findInnerClickable(pick, 5);
  const targetEl = inner || pick;
  return {
    ok: true,
    id: globalThis.__zd.idOf(targetEl),
    tag: targetEl.tagName,
    role: targetEl.getAttribute ? (targetEl.getAttribute("role") || null) : null,
    shadow: !!inner && inner !== pick,
    text: (targetEl.innerText || targetEl.textContent || "").trim().slice(0, 80),
  };
}
"""
)


# Given a light-DOM host, find the deepest interactive element reachable
# through its nested shadow roots (used by click_shadow).
INNER_CLICKABLE_FN = (
    "function(host, maxDepth) {\n"
    + FIND_INNER_CLICKABLE_JS
    + """
  const inner = findInnerClickable(host, maxDepth || 6);
  if (!inner) return { ok: false, reason: "no_inner_clickable" };
  return {
    ok: true,
    id: globalThis.__zd.idOf(inner),
    tag: inner.tagName,
    role: inner.getAttribute ? (inner.getAttribute("role") || null) : null,
    text: (inner.innerText || inner.textContent || "").trim().slice(0, 80),
  };
}
"""
)


# Dump the nested shadow-DOM tree of a light-DOM element for debugging.
# Returns a condensed JSON tree so agents don't have to hand-roll
# recursive execute_js calls to find the right selector path.
DESCRIBE_SHADOW_FN = """
function(host, maxDepth) {
  const cap = maxDepth || 6;
  function summary(el) {
    const tag = el.tagName.toLowerCase();
    const id = el.id || null;
    const role = el.getAttribute ? el.getAttribute("role") : null;
    const type = el.getAttribute ? el.getAttribute("type") : null;
    const text = (el.innerText || el.textContent || "").trim().slice(0, 40);
    return { tag, id, role, type, text };
  }
  function walk(el, depth) {
    const node = summary(el);
    node.light = [];
    for (const child of Array.from(el.children || [])) {
      if (depth <= 0) break;
      node.light.push(walk(child, depth - 1));
    }
    if (el.shadowRoot && depth > 0) {
      node.shadow = Array.from(el.shadowRoot.children || []).map(
        (c) => walk(c, depth - 1),
      );
    }
    return node;
  }
  return walk(host, cap);
}
"""
