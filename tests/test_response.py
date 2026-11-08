"""Tests for the tool result models and the error conversion (src/response.py)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from src.errors import Code, ElementNotFoundError, InvalidArgumentError
from src.response import Artifact, FileResult, Result, error_result, structured_of


class Counted(Result):
    count: int = Field(description="n")
    note: str | None = Field(default=None, description="optional")


def test_summary_only_renders_as_the_summary() -> None:
    r = Result(summary="done").to_call_result()
    assert r.content[0].text == "done"
    assert r.structured_content == {"summary": "done"}
    assert not r.is_error


def test_fields_render_as_compact_json_after_the_summary() -> None:
    r = Counted(summary="saved", count=3).to_call_result()
    assert r.content[0].text == 'saved\n{"count":3}'
    # None fields are omitted from both forms.
    assert r.structured_content == {"summary": "saved", "count": 3}


def test_files_become_resource_links(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text("{}")
    art = Artifact(
        path=str(f), uri="browser://artifacts/x.json", mime_type="application/json", size=2
    )
    r = FileResult(summary="wrote", files=[art]).to_call_result()
    links = [c for c in r.content if c.type == "resource_link"]
    assert [link.uri for link in links] == ["browser://artifacts/x.json"]
    assert r.structured_content["files"][0]["path"] == str(f)
    # The file list is in the links, not repeated in the text.
    assert "browser://artifacts" not in r.content[0].text


def test_attached_images_follow_the_text() -> None:
    res = Result(summary="shot")
    res.attach_image(b"\x89PNG", "image/png")
    r = res.to_call_result()
    assert [c.type for c in r.content] == ["text", "image"]


def test_deliberate_error_keeps_code_and_hint() -> None:
    r = error_result(ElementNotFoundError("#a"))
    assert r.is_error
    assert r.structured_content["code"] == Code.ELEMENT_NOT_FOUND.value
    assert r.structured_content["hint"]
    assert "Hint:" in r.content[0].text


def test_only_our_own_validation_is_invalid_argument() -> None:
    """A library's ValueError is not the caller's mistake (zendriver raises
    "list.remove(x): x not in list" from inside unrelated commands)."""
    a = error_result(InvalidArgumentError("bad")).structured_content
    b = error_result(ValueError("list.remove(x): x not in list")).structured_content
    assert a["code"] == Code.INVALID_ARGUMENT.value
    assert b["code"] == Code.INTERNAL.value
    assert b["error"] == "ValueError: list.remove(x): x not in list"


def test_unexpected_error_keeps_its_type_and_message() -> None:
    r = error_result(AttributeError("'NoneType' object has no attribute 'url'"))
    assert r.structured_content["code"] == Code.INTERNAL.value
    assert r.structured_content["error"].startswith("AttributeError: ")


def test_structured_of_reads_models_and_call_results() -> None:
    assert structured_of(Counted(summary="s", count=1)) == {"summary": "s", "count": 1}
    assert structured_of(Counted(summary="s", count=1).to_call_result())["count"] == 1
    assert structured_of("plain") is None
