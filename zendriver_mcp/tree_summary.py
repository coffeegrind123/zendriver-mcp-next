"""The summary view of an interaction tree: one line per region, repeats collapsed.

The list view costs about 20 tokens per element: 2.7k for the first 150 of
Hacker News, 10.5k for a whole Wikipedia article. Most of that is repetition (a
"hide" link per story, an "edit" link per section) and the whole page at once,
when an agent usually wants one region. The summary groups elements by the
landmark they sit in, lists each kind once per region, and folds identical
labels into one entry with a count:

    nav:Primary @7 | link: 12 Docs; 13 Blog; 14 Pricing; +40 more
    form:login @31 | in: 32 user*; 33 password (password)* | btn: 34 Log in
    main @40 | btn: 41 Add to cart x30 | link: 50 Item one; ...

Every shown entry keeps its id, so it can be clicked straight away; "@31" is
the landmark's own id, for get_interaction_tree(within="31") to expand one
region in full. An entry folded as "x30" names the first of the thirty.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

# Kinds in the order a region lists them: what can be filled first, links last.
KIND_ORDER = ("in", "sel", "chk", "rad", "btn", "tab", "mnu", "link", "el")

# How many distinct entries one kind shows per region. Navigation, forms and
# dialogs are where an agent acts, so they show more.
WIDE_LIMIT = 12
NARROW_LIMIT = 8
_WIDE_REGIONS = ("nav", "hdr", "dlg", "form", "search")

PAGE_REGION = "page"
MAX_OUTLINE = 8
MAX_OPTIONS = 4


def _suffix(e: dict[str, Any]) -> str:
    """What sets an entry apart besides its label: type, value, state, options."""
    out = ""
    it = e.get("it")
    if it:
        out += f" ({it})"
    if e.get("v") and e.get("t") != "sel":
        out += f' ="{e["v"]}"'
    if "ck" in e:
        out += " [x]" if e["ck"] else " [ ]"
    o = e.get("o")
    if isinstance(o, dict) and o.get("n"):
        shown = [str(x) for x in (o.get("s") or [])][:MAX_OPTIONS]
        more = f", +{o['n'] - len(shown)}" if o["n"] > len(shown) else ""
        sel = f' ="{e["v"]}"' if e.get("v") else ""
        out += f"{sel} ({o['n']} options: {', '.join(shown)}{more})"
    if e.get("req"):
        out += "*"
    if e.get("dis"):
        out += " (disabled)"
    return out


def _region_key(e: dict[str, Any]) -> tuple[str, int | None]:
    """The region's display name (frame-prefixed inside an iframe) and landmark id."""
    r = e.get("r") or PAGE_REGION
    f = e.get("f")
    rid = e.get("rid")
    return (f"f{f}:{r}" if f else r), (rid if isinstance(rid, int) else None)


def _is_wide(e: dict[str, Any]) -> bool:
    return str(e.get("r") or "").split(":", 1)[0] in _WIDE_REGIONS


def summarize(elements: list[dict[str, Any]]) -> list[str]:
    """One line per region, in page order."""
    regions: OrderedDict[tuple[str, int | None], dict[str, list[dict[str, Any]]]] = OrderedDict()
    for e in elements:
        kinds = regions.setdefault(_region_key(e), {})
        kinds.setdefault(e.get("t") or "el", []).append(e)

    lines: list[str] = []
    for (name, rid), kinds in regions.items():
        first = next(iter(kinds.values()))[0]
        limit = WIDE_LIMIT if _is_wide(first) else NARROW_LIMIT
        parts = [name + (f" @{rid}" if rid is not None else "")]
        ordered = [k for k in KIND_ORDER if k in kinds] + [k for k in kinds if k not in KIND_ORDER]
        for kind in ordered:
            folded: OrderedDict[str, list[int]] = OrderedDict()
            for e in kinds[kind]:
                token = f"{e.get('l', '')}{_suffix(e)}"
                folded.setdefault(token, []).append(int(e["id"]))
            entries = []
            for token, ids in list(folded.items())[:limit]:
                count = f" x{len(ids)}" if len(ids) > 1 else ""
                entries.append(f"{ids[0]} {token}{count}")
            hidden = sum(len(ids) for ids in list(folded.values())[limit:])
            if hidden:
                entries.append(f"+{hidden} more")
            parts.append(f"{kind}: " + "; ".join(entries))
        lines.append(" | ".join(parts))
    return lines


def counts(elements: list[dict[str, Any]]) -> str:
    """'229 elements: 228 link, 1 in'."""
    by: dict[str, int] = {}
    for e in elements:
        k = e.get("t") or "el"
        by[k] = by.get(k, 0) + 1
    ordered = sorted(by.items(), key=lambda kv: -kv[1])
    return f"{len(elements)} elements: " + ", ".join(f"{n} {k}" for k, n in ordered)
