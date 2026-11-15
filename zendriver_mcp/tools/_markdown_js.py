"""Visible content as Markdown, computed in our isolated world.

A FUNCTION DECLARATION ``function(el)``: given the document element it reads
the page's one ``main`` / ``[role=main]`` (or one ``article``) when there is
one, else the body; given any other element, that element. Hidden content
(``display:none``, closed ``<details>``, ``visibility:hidden`` text) is left
out, form controls are left out, open shadow roots and slots are followed, and
links and images keep absolute URLs.
"""

from __future__ import annotations

MARKDOWN_FN = r"""function(el) {
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "CANVAS", "IFRAME",
    "HEAD", "OBJECT", "EMBED", "SELECT", "OPTION", "DATALIST", "TEXTAREA", "INPUT", "META", "LINK"]);
  const BLOCK = new Set(["ADDRESS", "ARTICLE", "ASIDE", "DETAILS", "DIALOG", "DIV", "DL", "DD", "DT",
    "FIELDSET", "FIGCAPTION", "FIGURE", "FOOTER", "FORM", "HEADER", "HGROUP", "MAIN", "NAV",
    "SECTION", "SUMMARY", "LEGEND", "CAPTION"]);
  const squash = (s) => s.replace(/\s+/g, " ").trim();
  const clean = (s) => s.replace(/[ \t]+\n/g, "\n").replace(/\n[ \t]+\n/g, "\n\n")
    .replace(/\n{3,}/g, "\n\n").trim();
  // checkVisibility() is false for display:contents boxes (a <slot>, layout
  // wrappers), whose children render normally.
  const shown = (e) => !e.checkVisibility || e.checkVisibility() ||
    getComputedStyle(e).display === "contents";

  let root = el;
  if (el === document.documentElement) {
    const mains = [...document.querySelectorAll("main,[role=main]")].filter(shown);
    const articles = [...document.querySelectorAll("article")].filter(shown);
    root = mains.length === 1 ? mains[0] : articles.length === 1 ? articles[0] : document.body;
  }
  if (!root) return "";

  function kids(n) {
    if (n.shadowRoot) return [...n.shadowRoot.childNodes];
    if (n.tagName === "SLOT") {
      const a = n.assignedNodes({ flatten: true });
      return a.length ? a : [...n.childNodes];
    }
    return [...n.childNodes];
  }

  function table(n, ctx) {
    const rows = [...n.rows].filter(shown).map((r) =>
      [...r.cells].map((c) => squash(inner(c, ctx)).replace(/\|/g, "\\|")));
    if (!rows.length) return "";
    const width = Math.max(...rows.map((r) => r.length));
    const line = (r) => "| " + Array.from({ length: width }, (_, i) => r[i] || "").join(" | ") + " |";
    const out = [line(rows[0]), "| " + Array(width).fill("---").join(" | ") + " |"];
    for (const r of rows.slice(1)) out.push(line(r));
    return "\n\n" + out.join("\n") + "\n\n";
  }

  function list(n, ctx) {
    let i = parseInt(n.getAttribute("start") || "1", 10) || 1;
    const items = [...n.children].filter((c) => c.tagName === "LI" && shown(c));
    const out = items.map((li) => {
      const marker = n.tagName === "OL" ? `${i++}. ` : "- ";
      const body = clean(inner(li, ctx)).replace(/\n{2,}/g, "\n");
      const pad = " ".repeat(marker.length);
      return marker + body.split("\n").map((l, k) => (k === 0 ? l : pad + l)).join("\n");
    });
    return "\n\n" + out.join("\n") + "\n\n";
  }

  function inner(n, ctx) {
    return kids(n).map((k) => walk(k, ctx)).join("");
  }

  function walk(n, ctx) {
    if (n.nodeType === 3) {
      const p = n.parentElement;
      if (p && getComputedStyle(p).visibility !== "visible") return "";
      return ctx.pre ? n.data : n.data.replace(/\s+/g, " ");
    }
    if (n.nodeType !== 1) return "";
    const tag = n.tagName;
    if (SKIP.has(tag) || !shown(n)) return "";
    switch (tag) {
      case "BR": return "\n";
      case "HR": return "\n\n---\n\n";
      case "H1": case "H2": case "H3": case "H4": case "H5": case "H6": {
        const t = squash(inner(n, ctx));
        return t ? `\n\n${"#".repeat(+tag[1])} ${t}\n\n` : "";
      }
      case "P": return `\n\n${inner(n, ctx).trim()}\n\n`;
      case "A": {
        const t = squash(inner(n, ctx));
        const h = n.getAttribute("href");
        if (!t) return "";
        if (!h || h.startsWith("#") || /^javascript:/i.test(h)) return t;
        return `[${t}](${n.href})`;
      }
      case "IMG": return n.alt ? `![${squash(n.alt)}](${n.src})` : "";
      case "STRONG": case "B": {
        const t = inner(n, ctx);
        return t.trim() ? `**${t.trim()}**` : t;
      }
      case "EM": case "I": {
        const t = inner(n, ctx);
        return t.trim() ? `*${t.trim()}*` : t;
      }
      case "CODE": {
        if (ctx.pre) return inner(n, ctx);
        const t = inner(n, ctx);
        return t ? "`" + t.replace(/`/g, "\\`") + "`" : "";
      }
      case "PRE": {
        const code = n.querySelector("code") || n;
        const lang = (String(code.className).match(/language-([\w+-]+)/) || [])[1] || "";
        const body = inner(n, { ...ctx, pre: true }).replace(/\n+$/, "");
        return "\n\n```" + lang + "\n" + body + "\n```\n\n";
      }
      case "BLOCKQUOTE": {
        const t = clean(inner(n, ctx));
        return "\n\n" + t.split("\n").map((l) => (l ? "> " + l : ">")).join("\n") + "\n\n";
      }
      case "UL": case "OL": return list(n, ctx);
      case "TABLE": return table(n, ctx);
      default:
        return BLOCK.has(tag) ? `\n\n${inner(n, ctx).trim()}\n\n` : inner(n, ctx);
    }
  }

  return clean(walk(root, { pre: false }));
}"""
