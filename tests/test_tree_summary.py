"""The summary view: regions in page order, repeats folded, ids kept."""

from __future__ import annotations

from zendriver_mcp.tree_summary import NARROW_LIMIT, WIDE_LIMIT, counts, summarize


def _link(i: int, label: str, r: str | None = "main", rid: int | None = 100) -> dict:
    e: dict = {"id": i, "t": "link", "l": label}
    if r:
        e["r"] = r
        e["rid"] = rid
    return e


def test_regions_in_page_order_with_landmark_ids() -> None:
    lines = summarize(
        [
            _link(1, "Home", "hdr", 10),
            {"id": 2, "t": "in", "l": "user", "r": "form:login", "rid": 20, "req": True},
            {"id": 3, "t": "btn", "l": "Log in", "r": "form:login", "rid": 20},
            _link(4, "Docs", "hdr", 10),
        ]
    )
    assert lines == [
        "hdr @10 | link: 1 Home; 4 Docs",
        "form:login @20 | in: 2 user* | btn: 3 Log in",
    ]


def test_identical_labels_fold_into_the_first_id_with_a_count() -> None:
    els = [{"id": i, "t": "btn", "l": "Add to cart", "r": "main", "rid": 5} for i in range(10, 40)]
    assert summarize(els) == ["main @5 | btn: 10 Add to cart x30"]


def test_overflow_counts_every_hidden_element() -> None:
    els = [_link(i, f"Story {i}") for i in range(NARROW_LIMIT + 5)]
    els += [_link(100 + i, "hide") for i in range(3)]
    line = summarize(els)[0]
    assert f"{NARROW_LIMIT - 1} Story {NARROW_LIMIT - 1}; +8 more" in line


def test_navigation_shows_more_than_main() -> None:
    nav = [_link(i, f"N{i}", "nav:Primary", 1) for i in range(WIDE_LIMIT + 1)]
    assert summarize(nav)[0].endswith("+1 more")


def test_no_landmark_is_the_page_region_without_an_id() -> None:
    assert summarize([_link(1, "Top", None)]) == ["page | link: 1 Top"]


def test_frames_are_kept_apart() -> None:
    lines = summarize([_link(1, "A"), {**_link(2, "B"), "f": 1}])
    assert lines == ["main @100 | link: 1 A", "f1:main @100 | link: 2 B"]


def test_fields_that_tell_entries_apart() -> None:
    line = summarize(
        [
            {"id": 1, "t": "in", "l": "Email", "it": "email", "v": "a@b.c"},
            {"id": 2, "t": "chk", "l": "Remember", "ck": True},
            {
                "id": 3,
                "t": "sel",
                "l": "Country",
                "v": "Canada",
                "o": {"n": 6, "s": ["Canada", "Mexico", "Peru", "Chile"]},
            },
            {"id": 4, "t": "btn", "l": "Pay", "dis": True},
        ]
    )[0]
    assert '1 Email (email) ="a@b.c"' in line
    assert "2 Remember [x]" in line
    assert '3 Country ="Canada" (6 options: Canada, Mexico, Peru, Chile, +2)' in line
    assert "4 Pay (disabled)" in line


def test_counts() -> None:
    assert counts([_link(1, "a"), _link(2, "b"), {"id": 3, "t": "in", "l": "q"}]) == (
        "3 elements: 2 link, 1 in"
    )
