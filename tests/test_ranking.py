"""find_elements' word-match ranker, on trees shaped like the walker's."""

from __future__ import annotations

from zendriver_mcp.ranking import parse, rank

PAGE = [
    {"id": 1, "t": "link", "l": "Sign in", "r": "hdr"},
    {"id": 2, "t": "btn", "l": "Sign in", "r": "form:login"},
    {"id": 3, "t": "in", "l": "Email address", "it": "email", "r": "form:login"},
    {"id": 4, "t": "in", "l": "Password", "it": "password", "r": "form:login"},
    {"id": 5, "t": "link", "l": "Forgot your password?", "r": "form:login"},
    {"id": 6, "t": "btn", "l": "Add to cart", "r": "main"},
    {"id": 7, "t": "btn", "l": "Add to cart", "r": "main"},
    {"id": 8, "t": "btn", "l": "Checkout", "r": "main"},
    {"id": 9, "t": "sel", "l": "Country", "r": "main"},
    {"id": 10, "t": "in", "l": "Search products", "r": "search"},
    {"id": 11, "t": "link", "l": "Privacy", "r": "ftr", "h": "/legal/privacy"},
    {"id": 12, "t": "btn", "l": "Pay now", "r": "main", "dis": True},
    {"id": 13, "t": "btn", "l": "Pay later", "r": "main"},
]


def _top(description: str) -> int:
    ranked = rank(description, PAGE)
    assert ranked, description
    return ranked[0][1]["id"]


def test_kind_word_picks_between_equal_labels() -> None:
    assert _top("Sign in link") == 1
    assert _top("Sign in button") == 2


def test_region_word_picks_between_equal_labels() -> None:
    assert _top("sign in in the header") == 1
    assert _top("sign in in the form") == 2


def test_exact_label_beats_containing_label() -> None:
    assert _top("password") == 4
    assert _top("forgot password link") == 5


def test_input_type_counts() -> None:
    assert _top("email input") == 3


def test_partial_words_and_one_typo() -> None:
    assert _top("checkou") == 8
    assert _top("chekout button") == 8


def test_plurals() -> None:
    assert _top("search product") == 10


def test_link_target_words_count() -> None:
    assert _top("legal privacy") == 11


def test_no_content_match_returns_nothing() -> None:
    assert rank("Subscribe button", PAGE) == []


def test_kind_alone_is_the_label_when_nothing_else_is_given() -> None:
    q = parse("Search")
    assert q.terms == (("search", 1.0),)
    assert _top("Search") == 10


def test_disabled_ranks_below_an_equal_enabled_one() -> None:
    assert _top("pay") == 13


def test_ties_keep_page_order() -> None:
    ranked = rank("add to cart", PAGE)
    assert [e["id"] for _s, e in ranked[:2]] == [6, 7]


SITE = [
    {"id": 1, "t": "link", "l": "crates.io", "r": "hdr"},
    {"id": 2, "t": "in", "l": "Search", "r": "search"},
    {"id": 3, "t": "link", "l": "Wikipedia", "r": "hdr"},
    {"id": 4, "t": "in", "l": "Search Wikipedia", "r": "form:searchform"},
    {"id": 5, "t": "btn", "l": "Main menu"},
    {"id": 6, "t": "link", "l": "Log in with GitHub", "r": "hdr"},
    {"id": 7, "t": "link", "l": "Sign up", "r": "hdr"},
    {"id": 8, "t": "link", "l": "login", "r": "hdr"},
]


def _site_top(description: str) -> int:
    ranked = rank(description, SITE)
    assert ranked, description
    return ranked[0][1]["id"]


def test_asking_for_a_field_does_not_return_a_link() -> None:
    assert _site_top("crates search input") == 2


def test_kind_and_region_words_still_count_as_label_words() -> None:
    # "Search" is both a region word and the label; "menu" both a region word
    # and part of "Main menu". Dropping them matched the wrong element.
    assert _site_top("search crates input") == 2
    assert _site_top("search wikipedia") == 4
    assert _site_top("main menu button") == 5


def test_login_and_signup_synonyms() -> None:
    assert _site_top("sign in") in (6, 8)
    assert _site_top("log in with github") == 6
    assert _site_top("create account") == 7
    assert _site_top("register") == 7


def test_a_label_wholly_in_the_description_beats_one_that_shares_a_word() -> None:
    page = [
        {"id": 1, "t": "link", "l": "Fewer slides, more dragons"},
        {"id": 2, "t": "link", "l": "More"},
    ]
    assert rank("more stories link", page)[0][1]["id"] == 2
