"""Network logs, filters and response bodies, in a real Chrome.

The log listed what was fetched but not what came back: to read the data behind
a page an agent had to scrape it back out of the DOM. Now each entry carries a
request_id, and get_response_body returns the body itself (text paged, binary
as a file), with both sets of headers and the request body.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

from tests.live_harness import Sites, live, sc, with_browser

pytestmark = live

# A 1x1 PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=="
)

PAGE = """<!doctype html><title>net</title><img src="/pixel.png">
<script>
fetch('/api/data.json').then(r => r.json()).then(d => document.title = d.name);
fetch('/missing.json');
fetch('/api/echo', {method: 'POST', headers: {'X-Test': 'yes'}, body: JSON.stringify({q: 'hello'})});
</script>"""


async def _scenario(c: Any, sites: Sites) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sc(await c.call_tool("navigate", {"url": f"{sites.a}/net.html", "settle": 3}))
    out["all"] = sc(
        await c.call_tool("call_tool", {"name": "get_network_logs", "arguments": {"limit": 50}})
    )
    out["errors"] = sc(
        await c.call_tool(
            "call_tool", {"name": "get_network_logs", "arguments": {"errors_only": True}}
        )
    )
    out["api"] = sc(
        await c.call_tool(
            "call_tool",
            {
                "name": "get_network_logs",
                "arguments": {"url_contains": "/api/", "types": ["Fetch"]},
            },
        )
    )
    by = {e["url"].rsplit("/", 1)[-1]: e for e in out["all"]["entries"]}
    body = lambda rid, **kw: c.call_tool(  # noqa: E731
        "call_tool", {"name": "get_response_body", "arguments": {"request_id": rid, **kw}}
    )
    out["json"] = sc(await body(by["data.json"]["request_id"]))
    out["paged"] = sc(await body(by["data.json"]["request_id"], max_chars=5, offset=2))
    out["echo"] = sc(await body(by["echo"]["request_id"]))
    out["png"] = sc(await body(by["pixel.png"]["request_id"]))
    out["unknown"] = (await body("nope")).structured_content
    return out


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    sites = Sites(
        {
            "/net.html": PAGE,
            "/api/data.json": json.dumps({"name": "Ada", "items": [1, 2, 3]}),
            "/pixel.png": PNG,
        }
    )
    try:
        return asyncio.run(with_browser(lambda c: _scenario(c, sites)))
    finally:
        sites.close()


def test_entries_carry_request_ids_and_sizes(run: dict[str, Any]) -> None:
    entries = run["all"]["entries"]
    assert all(e.get("request_id") for e in entries), entries
    data = next(e for e in entries if e["url"].endswith("/data.json"))
    assert data.get("size", 0) > 0


def test_filters(run: dict[str, Any]) -> None:
    errors = [e["url"].rsplit("/", 1)[-1] for e in run["errors"]["entries"]]
    assert "missing.json" in errors and "data.json" not in errors
    api = sorted(e["url"].rsplit("/", 1)[-1] for e in run["api"]["entries"])
    assert api == ["data.json", "echo"]


def test_text_body_is_returned_and_paged(run: dict[str, Any]) -> None:
    j = run["json"]
    assert json.loads(j["text"]) == {"name": "Ada", "items": [1, 2, 3]}
    assert j["status"] == 200 and j["binary"] is False
    assert "content-type" in {k.lower() for k in j["headers"]}
    p = run["paged"]
    assert p["text"] == j["text"][2:7]
    assert p["next_offset"] == 7


def test_request_body_and_headers_are_returned(run: dict[str, Any]) -> None:
    e = run["echo"]
    assert json.loads(e["post_data"]) == {"q": "hello"}
    assert {k.lower(): v for k, v in e["request_headers"].items()}["x-test"] == "yes"
    assert json.loads(e["text"]) == {"q": "hello"}


def test_binary_body_is_saved_as_a_file(run: dict[str, Any]) -> None:
    png = run["png"]
    assert png["binary"] is True and png.get("text") is None
    assert Path(png["files"][0]["path"]).read_bytes() == PNG


def test_an_unknown_id_says_where_ids_come_from(run: dict[str, Any]) -> None:
    assert run["unknown"]["code"] == "invalid_argument"
    assert "get_network_logs" in run["unknown"]["hint"]
