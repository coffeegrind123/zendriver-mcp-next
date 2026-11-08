"""Pagination contract for get_content / get_text_content."""

from __future__ import annotations

from src.tools.content import ContentTools


def test_first_slice_reports_next_offset() -> None:
    text = "abcdefghij" * 3  # 30 chars
    out = ContentTools._paginate(text, 10, 0).render()
    header, body = out.split("\n", 1)
    assert header == "[chars 0-10 of 30] (next: offset=10)"
    assert body == "abcdefghij"


def test_final_slice_has_no_next() -> None:
    text = "abcdefghij" * 3
    out = ContentTools._paginate(text, 10, 20).render()
    header, body = out.split("\n", 1)
    assert header == "[chars 20-30 of 30]"
    assert body == "abcdefghij"


def test_offset_clamps_past_end() -> None:
    text = "abcdefghij" * 3
    out = ContentTools._paginate(text, 10, 999).render()
    assert out.startswith("[chars 30-30 of 30]")


def test_whole_text_when_max_exceeds_length() -> None:
    text = "abcdefghij" * 3
    out = ContentTools._paginate(text, 1000, 0).render()
    header, body = out.split("\n", 1)
    assert header == "[chars 0-30 of 30]"
    assert body == text


def test_max_chars_floored_to_one() -> None:
    out = ContentTools._paginate("hello", 0, 0).render()
    header, body = out.split("\n", 1)
    assert header == "[chars 0-1 of 5] (next: offset=1)"
    assert body == "h"


def test_structured_fields_match_the_header() -> None:
    page = ContentTools._paginate("abcdefghij" * 3, 10, 10)
    assert (page.offset, page.end, page.total, page.next_offset) == (10, 20, 30, 20)
    last = ContentTools._paginate("abcdefghij" * 3, 10, 20)
    assert last.next_offset is None
    assert "next_offset" not in last.model_dump(exclude_none=True)
