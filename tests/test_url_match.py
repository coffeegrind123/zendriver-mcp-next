"""wait_for_url's pattern forms: substring, glob over the whole URL, /regex/."""

from __future__ import annotations

import pytest

from zendriver_mcp.tools.utils import url_matches

URL = "https://shop.example.com/checkout/done?order=42"


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("/checkout/done", True),  # starts with "/" but does not end with it: a substring
        ("checkout", True),
        ("cart", False),
        ("https://*.example.com/*", True),
        ("*.example.org/*", False),
        ("/order=\\d+/", True),
        ("/order=[a-z]+/", False),
    ],
)
def test_patterns(pattern: str, expected: bool) -> None:
    assert url_matches(URL, pattern) is expected
