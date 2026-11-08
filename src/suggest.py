"""'Did you mean' suggestions for names a caller got slightly wrong.

A model that mistypes a tool name, tab id or device profile recovers in one
step when the error names the closest real value; without it, it guesses again
or goes searching.
"""

from __future__ import annotations

MAX_SUGGESTIONS = 3
MIN_SUBSTRING = 3


def _distance(a: str, b: str) -> int:
    """Levenshtein distance, two-row form."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def closest(name: str, candidates: list[str], limit: int = MAX_SUGGESTIONS) -> list[str]:
    """Candidates close to ``name``: case-insensitive, substring counts as close.

    The threshold scales with length, so 'tab1' finds 'tab_1' but 'x' does not
    find every one-letter-off name in the catalogue.
    """
    key = name.lower().strip()
    if not key:
        return []
    scored: list[tuple[int, str]] = []
    for cand in candidates:
        c = cand.lower()
        # Substring counts only for names long enough to mean something:
        # otherwise 'a' "matches" every candidate that contains an a.
        if min(len(key), len(c)) >= MIN_SUBSTRING and (key in c or c in key):
            scored.append((0, cand))
            continue
        d = _distance(key, c)
        if d <= max(2, len(key) // 3):
            scored.append((d, cand))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [c for _, c in scored[:limit]]


def did_you_mean(name: str, candidates: list[str]) -> str | None:
    """A ready-to-show hint, or None when nothing is close."""
    found = closest(name, candidates)
    if not found:
        return None
    return "Did you mean " + " or ".join(repr(f) for f in found) + "?"
