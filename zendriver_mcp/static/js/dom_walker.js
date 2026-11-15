function (...closedRoots) {
  // Runs in an ISOLATED world (src/dom.py), never the page's. Ids are kept in
  // that world's globalThis.__zd store, not written into the DOM: a
  // data-* attribute on every element is readable by any page script.
  // Ids are stable for an element's life, so one from an earlier walk still
  // works while its element exists, and a number is never reused.
  //
  // `closedRoots` are the page's closed shadow roots, which `el.shadowRoot`
  // hides from script. dom.py finds them through CDP (DOM.getDocument with
  // pierce) and passes them in, so nothing has to be hooked in the page.
  const S = globalThis.__zd;
  if (!S) throw new Error("element store missing: run via src/dom.py");
  S.prune();

  const vh = window.innerHeight, vw = window.innerWidth;

  const closedOf = new Map();
  for (const r of closedRoots) if (r && r.host) closedOf.set(r.host, r);
  const shadowOf = (el) => el.shadowRoot || closedOf.get(el) || null;

  // An open modal <dialog> makes everything outside it inert without any
  // attribute saying so.
  const modal = document.querySelector(":modal");
  function composedContains(outer, el) {
    for (let n = el; n; n = n.parentNode || (n instanceof ShadowRoot ? n.host : null)) {
      if (n === outer) return true;
    }
    return false;
  }

  // Tree-walker verdict for one element. REJECT drops the element AND its
  // subtree, so it is only for states a descendant cannot undo: display:none,
  // inert, aria-hidden, opacity 0. `visibility` CAN be undone by a child
  // (visibility:visible inside visibility:hidden shows), and a zero-size box
  // (display:contents, a collapsed wrapper) can still have visible children,
  // so those are SKIP: not listed, but descended into.
  function verdict(el) {
    if (el.inert || el.getAttribute("aria-hidden") === "true") return NodeFilter.FILTER_REJECT;
    const s = getComputedStyle(el);
    if (s.display === "none" || s.opacity === "0" || s.contentVisibility === "hidden") {
      return NodeFilter.FILTER_REJECT;
    }
    return NodeFilter.FILTER_ACCEPT;
  }

  // Whether an accepted element is itself on screen.
  function shown(el) {
    if (getComputedStyle(el).visibility !== "visible") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  // Look an id up where the referencing element lives first: inside a shadow
  // root, getElementById on the document cannot see it.
  function byId(el, id) {
    const root = el.getRootNode();
    return (root.getElementById && root.getElementById(id)) || document.getElementById(id);
  }

  function textOf(n) {
    return (n.innerText || n.textContent || "").trim();
  }

  // A <label>'s text without the text of the controls inside it.
  function labelText(l) {
    const c = l.cloneNode(true);
    c.querySelectorAll("input,textarea,select,button").forEach(x => x.remove());
    return textOf(c);
  }

  // Smart label inference - tries multiple sources
  function lbl(el) {
    // aria-label
    let l = el.getAttribute("aria-label");
    if (l) return l.trim();

    // aria-labelledby is a LIST of ids, joined in order.
    const lblBy = el.getAttribute("aria-labelledby");
    if (lblBy) {
      const t = lblBy.trim().split(/\s+/).map(id => byId(el, id)).filter(Boolean)
        .map(textOf).filter(Boolean).join(" ");
      if (t) return t;
    }

    const tag = el.tagName;

    // Form controls
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      if (el.type === "submit" || el.type === "button") return el.value || "";

      // <label for> and a wrapping <label>, both, as the browser associates
      // them: scoped to the element's own tree, and immune to ids that are
      // not valid in a CSS selector.
      for (const l of el.labels || []) {
        const t = labelText(l);
        if (t) return t;
      }

      if (el.placeholder) return el.placeholder;
      if (el.name) return el.name;

      // Select shows selected option
      if (tag === "SELECT" && el.selectedIndex >= 0) return el.options[el.selectedIndex]?.text || "";
    }

    // Links and buttons - text content
    if (tag === "A" || tag === "BUTTON" || el.getAttribute("role") === "button") {
      const txt = el.innerText?.trim();
      if (txt && txt.length < 60) return txt;

      // Icon button - svg title
      const svg = el.querySelector("svg");
      if (svg) {
        const t = svg.querySelector("title")?.textContent;
        if (t) return t;
        // Use href from <use>
        const use = svg.querySelector("use");
        if (use) {
          const href = use.getAttribute("href") || use.getAttribute("xlink:href");
          if (href) return href.split("#").pop();
        }
      }
    }

    // Generic: short text
    const txt = el.innerText?.trim();
    if (txt && txt.length < 60) return txt;

    // title attribute
    if (el.title) return el.title;

    // img alt
    if (tag === "IMG" && el.alt) return el.alt;

    return "";
  }

  // Compact type
  function typ(el) {
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role");
    const t = el.type?.toLowerCase();

    if (tag === "button" || role === "button") return "btn";
    if (tag === "a" || role === "link") return "link";
    if (tag === "input") {
      if (t === "checkbox" || role === "checkbox") return "chk";
      if (t === "radio" || role === "radio") return "rad";
      if (t === "submit" || t === "button") return "btn";
      return "in";
    }
    if (tag === "textarea") return "in";
    if (tag === "select" || role === "combobox") return "sel";
    if (el.getAttribute("contenteditable") === "true" || role === "textbox") return "in";
    if (role === "tab") return "tab";
    if (role === "menuitem") return "mnu";
    return "el";
  }

  // Region detection (compact: 1-4 chars)
  function rgn(el) {
    const c = el.closest("[role='banner'],header,[role='navigation'],nav,[role='main'],main,[role='contentinfo'],footer,aside,[role='complementary'],[role='dialog'],[aria-modal='true']");
    if (c) {
      const r = c.getAttribute("role"), t = c.tagName.toLowerCase();
      if (r === "banner" || t === "header") return "hdr";
      if (r === "navigation" || t === "nav") return "nav";
      if (r === "main" || t === "main") return "main";
      if (r === "contentinfo" || t === "footer") return "ftr";
      if (r === "complementary" || t === "aside") return "side";
      if (r === "dialog" || c.getAttribute("aria-modal") === "true") return "dlg";
    }
    // Heuristic
    const rect = el.getBoundingClientRect();
    if (rect.top < 80) return "hdr";
    if (rect.left < 200 && rect.top > 80) return "side";
    if (rect.top > vh - 80) return "ftr";
    return "main";
  }

  function interactive(el) {
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role");
    const s = getComputedStyle(el);

    if (["a", "button", "input", "select", "textarea", "details", "summary"].includes(tag)) return true;
    if (["button", "link", "checkbox", "menuitem", "tab", "textbox", "combobox", "radio", "switch", "option"].includes(role)) return true;
    if (el.getAttribute("contenteditable") === "true") return true;
    if (s.cursor === "pointer" && (el.onclick || el.getAttribute("onclick"))) return true;
    return false;
  }

  // Skip SVG internals and nested interactive children
  function skip(el, seen) {
    const tag = el.tagName.toLowerCase();
    if (["path", "use", "g", "circle", "rect", "line", "polygon", "svg", "defs", "clippath"].includes(tag)) return true;

    // Skip if parent already captured
    let p = el.parentElement;
    for (let i = 0; i < 3 && p; i++) {
      if (seen.has(p)) return true;
      p = p.parentElement;
    }
    return false;
  }

  function walk(root, out, seen) {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, { acceptNode: verdict });

    let n = w.currentNode;
    while (n) {
      if (n !== root && interactive(n) && !skip(n, seen) && shown(n) &&
          !(modal && !composedContains(modal, n))) {
        const label = lbl(n).replace(/\s+/g, " ").trim().slice(0, 50);
        const t = typ(n);

        // Skip unlabeled generic elements
        if (!label && t === "el") { n = w.nextNode(); continue; }

        const i = S.idOf(n);
        seen.add(n);

        const o = { id: i, t: t, l: label || `[${n.tagName.toLowerCase()}]`, r: rgn(n) };

        // Link target.
        //
        // Without this the tree gives an agent an id and a label and no way to
        // learn where a link goes, so "open that article" becomes: click it and
        // hope, or guess the URL. Observed doing exactly that — a link rendered
        // as `[a]` with no text, then a click that timed out, then three
        // invented URL slugs that 404ed, then a curl fallback. The href was in
        // the DOM the whole time.
        //
        // Absolute, because a relative one is not usable without also knowing
        // the page URL, and `n.href` resolves it for us. Skipped when it says
        // nothing: javascript: handlers, bare "#" anchors, and a href equal to
        // the current page are all noise on a token budget.
        if (t === "link" && n.tagName === "A") {
          const raw = n.getAttribute("href");
          const resolved = n.href;
          if (raw && resolved && !/^javascript:/i.test(raw) && raw !== "#") {
            let u = null;
            try { u = new URL(resolved); } catch (e) { u = null; }
            // A link to the page you are already on, fragment or not, is a skip
            // link or a tab anchor. It cannot take you anywhere.
            const samePage =
              u && u.origin === location.origin && u.pathname === location.pathname &&
              u.search === location.search;
            if (u && !samePage) {
              // Same-origin links are emitted as a path. The origin is already
              // known from the page and repeating it on every link is most of
              // what this field costs — measured at +116% on the whole tree for
              // a 125-link search page.
              const short = u.origin === location.origin
                ? u.pathname + u.search + u.hash
                : resolved;
              o.h = short.length > 200 ? short.slice(0, 200) : short;
            }
          }
        }

        // Input type (only non-text)
        if (t === "in" && n.tagName === "INPUT" && n.type && !["text", "search"].includes(n.type)) {
          o.it = n.type;
        }

        // Current value
        if (["in", "sel"].includes(t) && n.value?.trim()) {
          o.v = n.value.trim().slice(0, 30);
        }

        // Checked state
        if (["chk", "rad"].includes(t) && n.checked !== undefined) {
          o.ck = n.checked;
        }

        // Required, and disabled: :disabled includes controls inside a
        // disabled <fieldset>; aria-disabled is how custom widgets say it.
        if (n.required || n.getAttribute("aria-required") === "true") o.req = true;
        if ((n.matches && n.matches(":disabled")) || n.closest("[aria-disabled='true']")) o.dis = true;

        // Offscreen
        const rect = n.getBoundingClientRect();
        if (rect.bottom < 0 || rect.top > vh || rect.right < 0 || rect.left > vw) {
          o.off = true;
        }

        out.push(o);
      }

      const sr = shadowOf(n);
      if (sr) { roots.push(sr); walk(sr, out, seen); }
      n = w.nextNode();
    }
  }

  const out = [], seen = new Set(), roots = [document];
  if (document.body) walk(document.body, out, seen);

  // File inputs are usually hidden behind a styled button, which is why the
  // walk above skips them, but they are what upload_file and fill_form need.
  // Listed with hid:true so it is clear they cannot be clicked.
  for (const root of roots) {
    for (const f of root.querySelectorAll("input[type=file]")) {
      if (seen.has(f) || f.disabled) continue;
      seen.add(f);
      const o = { id: S.idOf(f), t: "in", l: lbl(f).replace(/\s+/g, " ").trim().slice(0, 50) || "[input]",
                  r: rgn(f), it: "file" };
      if (!shown(f)) o.hid = true;
      out.push(o);
    }
  }
  return out;
}
