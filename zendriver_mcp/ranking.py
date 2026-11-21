"""Rank interaction-tree elements against a plain-words description.

find_elements' default ranker: word matching over what the walker already
knows of each element (label, kind, input type, region, link target, value),
weighted by how rare a word is among the candidates. It needs no model and no
page access, so it costs nothing a page could notice, and it is what an
optional neural reranker (folate_rank.py) narrows from.

"Sign in link in header" scores an element by its label words ("sign in" is
folded to "login", as "log in" is), adds a bonus when its kind is a link and its
region the header, and ranks a label that IS the description above one that
merely contains it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

# Words that say nothing about which element is meant.
STOP_WORDS = frozenset(
    "a an the to in on at for of with by from into onto and or my your this that "
    "please click tap press type enter fill select choose open find hit use go "
    "element item thing one".split()
)

# Words that name an element's kind, mapped to the walker's `t`.
KIND_WORDS = {
    "button": "btn",
    "btn": "btn",
    "link": "link",
    "anchor": "link",
    "input": "in",
    "field": "in",
    "box": "in",
    "textbox": "in",
    "textarea": "in",
    "checkbox": "chk",
    "radio": "rad",
    "dropdown": "sel",
    "select": "sel",
    "combobox": "sel",
    "tab": "tab",
    "menuitem": "mnu",
}

# Words that name a region, mapped to the start of the walker's `r`.
REGION_WORDS = {
    "header": "hdr",
    "banner": "hdr",
    "top": "hdr",
    "nav": "nav",
    "navigation": "nav",
    "navbar": "nav",
    "menu": "nav",
    "main": "main",
    "footer": "ftr",
    "bottom": "ftr",
    "sidebar": "side",
    "aside": "side",
    "dialog": "dlg",
    "modal": "dlg",
    "popup": "dlg",
    "form": "form",
    "search": "search",
}

# How much a word found in each field counts.
LABEL_WEIGHT = 3.0
TYPE_WEIGHT = 1.5
REGION_NAME_WEIGHT = 1.0
VALUE_WEIGHT = 0.75
HREF_WEIGHT = 0.5

# Kinds fall into two groups: ones you fill and ones you click. Asking for an
# input and getting a link is a strong miss; asking for a button and getting a
# link is not (sites style links as buttons, and tabs are usually links), so
# only a mismatch across the groups costs.
FILLABLE = frozenset({"in", "sel", "chk", "rad"})
GROUP_MISMATCH_FACTOR = 0.5

KIND_BONUS = 1.5
REGION_BONUS = 1.0
EXACT_LABEL_BONUS = 3.0
PHRASE_BONUS = 1.5
# Every word of the label is in the description: "More" for "more stories
# link", against a story titled "Fewer slides, more dragons".
COVERED_LABEL_BONUS = 1.5
DISABLED_PENALTY = 0.5

# A partial word match (prefix, or one typo) counts this much of a full one.
PARTIAL = 0.6
_MIN_PARTIAL_LEN = 4

# A kind or region word counts this much as a label word. Such words are often
# the label itself ("Search", "Main menu"), so they are not dropped, but they
# weigh less than the words that only a label can carry.
STRUCTURE_TERM_WEIGHT = 0.5

_WORD = re.compile(r"[a-z0-9]+")

# Phrasings of the same action, folded into one word on both sides, so "sign
# in" finds "Log in with GitHub" and "create account" finds "Sign up".
_SYNONYMS = (
    (re.compile(r"\b(?:sign|log)[\s-]?(?:in|on)\b"), "login"),
    (re.compile(r"\b(?:sign[\s-]?up|register|create\s+(?:an\s+)?account)\b"), "signup"),
)


def words(text: str) -> list[str]:
    t = text.lower()
    for pattern, canon in _SYNONYMS:
        t = pattern.sub(canon, t)
    return _WORD.findall(t)


def _stem(w: str) -> str:
    """Plural to singular, enough to match 'results' to 'result'."""
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _one_edit(a: str, b: str) -> bool:
    """Whether a and b differ by at most one insertion, deletion or substitution."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(a) == len(b):
            i += 1
        j += 1
    return edits + (len(b) - j) <= 1


@dataclass(frozen=True)
class Query:
    terms: tuple[tuple[str, float], ...]
    kinds: frozenset[str]
    regions: frozenset[str]
    phrase: str


def parse(description: str) -> Query:
    """Split a description into weighted label words, kind words and region words.

    Kind and region words ("button", "header", "search") also count as label
    words at STRUCTURE_TERM_WEIGHT, or fully when they are all there is.
    """
    ws = [w for w in words(description) if w not in STOP_WORDS]
    kinds = frozenset(KIND_WORDS[w] for w in ws if w in KIND_WORDS)
    regions = frozenset(REGION_WORDS[w] for w in ws if w in REGION_WORDS)
    structural = {w for w in ws if w in KIND_WORDS or w in REGION_WORDS}
    only_structure = all(w in structural for w in ws)
    terms: dict[str, float] = {}
    for w in ws:
        weight = 1.0 if only_structure or w not in structural else STRUCTURE_TERM_WEIGHT
        stem = _stem(w)
        terms[stem] = max(terms.get(stem, 0.0), weight)
    return Query(
        terms=tuple(terms.items()),
        kinds=kinds,
        regions=regions,
        phrase=" ".join(ws),
    )


def _fields(e: dict[str, Any]) -> list[tuple[list[str], float]]:
    region = str(e.get("r") or "")
    region_name = region.split(":", 1)[1] if ":" in region else ""
    href = str(e.get("h") or "")
    return [
        ([_stem(w) for w in words(str(e.get("l") or ""))], LABEL_WEIGHT),
        ([_stem(w) for w in words(str(e.get("it") or ""))], TYPE_WEIGHT),
        ([_stem(w) for w in words(region_name)], REGION_NAME_WEIGHT),
        ([_stem(w) for w in words(str(e.get("v") or ""))], VALUE_WEIGHT),
        ([_stem(w) for w in words(href)], HREF_WEIGHT),
    ]


def _match(term: str, field: list[str]) -> float:
    best = 0.0
    for w in field:
        if w == term:
            return 1.0
        if len(term) >= _MIN_PARTIAL_LEN and (w.startswith(term) or term.startswith(w)):
            if min(len(w), len(term)) >= _MIN_PARTIAL_LEN:
                best = PARTIAL
        elif len(term) > _MIN_PARTIAL_LEN and _one_edit(term, w):
            best = max(best, PARTIAL)
    return best


def rank(description: str, elements: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
    """Elements that match any content word, best first, with their scores.

    Ties keep page order. An element that matches no content word is left out,
    however well its kind or region fits: "Checkout button" must not return
    every button.
    """
    q = parse(description)
    if not q.terms:
        return []
    fields = [_fields(e) for e in elements]

    # Inverse document frequency over the candidates: "cart" on a page with
    # thirty "Add to cart" buttons says less than "checkout" on one.
    n = len(elements)
    idf: dict[str, float] = {}
    for t, _weight in q.terms:
        df = sum(1 for fs in fields if any(_match(t, f) for f, _w in fs))
        idf[t] = math.log(1 + n / df) if df else 0.0

    scored: list[tuple[float, int, dict[str, Any]]] = []
    for i, (e, fs) in enumerate(zip(elements, fields, strict=True)):
        score = 0.0
        for t, weight in q.terms:
            score += idf[t] * weight * max(_match(t, f) * w for f, w in fs)
        if score <= 0:
            continue
        label = " ".join(w for w in words(str(e.get("l") or "")) if w not in STOP_WORDS)
        if label and label == q.phrase:
            score += EXACT_LABEL_BONUS
        elif q.phrase and len(q.terms) > 1 and q.phrase in label:
            score += PHRASE_BONUS
        elif label and set(_stem(w) for w in label.split()) <= {t for t, _w in q.terms}:
            score += COVERED_LABEL_BONUS
        if e.get("t") in q.kinds:
            score += KIND_BONUS
        elif q.kinds and all((k in FILLABLE) != (e.get("t") in FILLABLE) for k in q.kinds):
            score *= GROUP_MISMATCH_FACTOR
        region = str(e.get("r") or "").split(":", 1)[0]
        if region and region in q.regions:
            score += REGION_BONUS
        if e.get("dis"):
            score -= DISABLED_PENALTY
        scored.append((score, i, e))

    scored.sort(key=lambda s: (-s[0], s[1]))
    return [(round(s, 3), e) for s, _i, e in scored]
