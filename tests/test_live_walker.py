"""What get_interaction_tree reports, against real pages in a real Chrome.

Each page below is a case the walker used to get wrong. Labels came from a
single-id getElementById for aria-labelledby (lists of ids, and ids inside a
shadow root, were missed) and from an unescaped `label[for="..."]` selector (an
id with a quote threw and failed the whole walk). Hidden-ness was decided per
element with the subtree rejected, so a `visibility:visible` child of a hidden
parent vanished, and `inert` content, which nobody can click, was listed.
Closed shadow roots were invisible because `el.shadowRoot` is null for them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.live_harness import data_url, live, sc, with_browser

pytestmark = live

GUARD = """<script>
window.log = [];
new MutationObserver((ms) => ms.forEach((m) => m.type === 'attributes' &&
  log.push('attr:' + m.attributeName))).observe(document, {attributes: true, subtree: true});
</script>"""

DELTA_PAGE = """
<button id="go" onclick="old.remove(); keep.textContent = 'Kept, renamed';
  const b = document.createElement('button'); b.textContent = 'Fresh'; document.body.append(b)">Go</button>
<button id="old">Old</button><button id="keep">Kept</button><button>Same</button>"""

PAGES = {
    "labelledby_list": """
<span id="a">First</span><span id="b">Name</span>
<input aria-labelledby="a b">""",
    "labelledby_shadow": """
<x-field></x-field>
<script>
customElements.define('x-field', class extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: 'open'}).innerHTML =
    '<span id="lbl">Shadow label</span><input aria-labelledby="lbl">'; }
});
</script>""",
    "label_for_quote": """
<label for='a"b'>Quoted</label><input id='a"b'>
<button>After</button>""",
    "label_for_shadow": """
<x-f2></x-f2>
<script>
customElements.define('x-f2', class extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: 'open'}).innerHTML =
    '<label for="x">In shadow</label><input id="x">'; }
});
</script>""",
    "hidden_states": """
<div inert><button>Inert button</button></div>
<details><summary>More</summary><button>Inside closed details</button></details>
<div aria-hidden="true"><button>Aria hidden</button></div>
<div style="visibility:hidden"><button style="visibility:visible">Peek</button></div>
<button>Plain</button>""",
    "flags": """
<input name="email" required>
<input name="phone" aria-required="true">
<fieldset disabled><input name="locked"></fieldset>
<button aria-disabled="true">Soft off</button>
<input type="file" name="doc" style="display:none">""",
    "closed_shadow_default": """
<x-closed2></x-closed2>
<script>
customElements.define('x-closed2', class extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: 'closed'}).innerHTML =
    '<button>Closed inside</button>'; }
});
</script>""",
    "closed_shadow": """
<x-closed></x-closed>
<script>
customElements.define('x-closed', class extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: 'closed'}).innerHTML =
    '<button>Closed inside</button>'; }
});
</script>""",
}


async def _scenario(c: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, body in PAGES.items():
        sc(await c.call_tool("navigate", {"url": data_url(GUARD + body), "settle": 1}))
        args = {"closed_shadow": True} if name == "closed_shadow" else {}
        result = await c.call_tool("get_interaction_tree", args)
        if result.is_error:
            out[name] = {"error": result.structured_content}
            continue
        tree = result.structured_content["elements"]
        log = sc(await c.call_tool("execute_javascript", {"script": "return window.log"}))
        out[name] = {"tree": tree, "log": log["value"] if "value" in log else log}

    # only_changed after an action: one button added, one removed, one relabelled.
    sc(await c.call_tool("navigate", {"url": data_url(GUARD + DELTA_PAGE), "settle": 1}))
    first = sc(await c.call_tool("get_interaction_tree", {}))
    ids = {e["l"]: e["id"] for e in first["elements"]}
    sc(await c.call_tool("click", {"selector": str(ids["Go"])}))
    out["delta"] = {
        "before": ids,
        "after": sc(await c.call_tool("get_interaction_tree", {"only_changed": True})),
    }
    return out


@pytest.fixture(scope="module")
def walked() -> dict[str, Any]:
    return asyncio.run(with_browser(_scenario))


def _tree(walked: dict[str, Any], page: str) -> list[dict[str, Any]]:
    entry = walked[page]
    assert "error" not in entry, entry
    assert not entry["log"], f"the walk mutated the page: {entry['log']}"
    return entry["tree"]


def _labels(walked: dict[str, Any], page: str) -> list[str]:
    return [e["l"] for e in _tree(walked, page)]


def test_aria_labelledby_joins_every_id(walked: dict[str, Any]) -> None:
    assert "First Name" in _labels(walked, "labelledby_list")


def test_aria_labelledby_resolves_inside_a_shadow_root(walked: dict[str, Any]) -> None:
    assert "Shadow label" in _labels(walked, "labelledby_shadow")


def test_label_for_with_a_quote_in_the_id(walked: dict[str, Any]) -> None:
    labels = _labels(walked, "label_for_quote")
    assert "Quoted" in labels
    assert "After" in labels


def test_label_for_inside_a_shadow_root(walked: dict[str, Any]) -> None:
    assert "In shadow" in _labels(walked, "label_for_shadow")


def test_unreachable_elements_are_left_out(walked: dict[str, Any]) -> None:
    labels = _labels(walked, "hidden_states")
    assert "Plain" in labels
    assert "Inert button" not in labels
    assert "Inside closed details" not in labels
    assert "Aria hidden" not in labels


def test_visible_child_of_a_hidden_parent_is_listed(walked: dict[str, Any]) -> None:
    assert "Peek" in _labels(walked, "hidden_states")


def test_required_disabled_and_hidden_file_flags(walked: dict[str, Any]) -> None:
    by_label = {e["l"]: e for e in _tree(walked, "flags")}
    assert by_label["email"].get("req") is True
    assert by_label["phone"].get("req") is True
    assert by_label["locked"].get("dis") is True
    assert by_label["Soft off"].get("dis") is True
    # A styled upload button usually hides the real <input type=file>; it is
    # still the element upload_file and fill_form must target.
    assert by_label["doc"].get("hid") is True
    assert by_label["doc"].get("it") == "file"


def test_closed_shadow_root_content_is_listed(walked: dict[str, Any]) -> None:
    assert "Closed inside" in _labels(walked, "closed_shadow")


def test_closed_shadow_is_opt_in(walked: dict[str, Any]) -> None:
    assert "Closed inside" not in _labels(walked, "closed_shadow_default")


def test_only_changed_reports_exactly_what_the_click_did(walked: dict[str, Any]) -> None:
    before, after = walked["delta"]["before"], walked["delta"]["after"]
    labels = {e["l"]: e["id"] for e in after["elements"]}
    assert set(labels) == {"Kept, renamed", "Fresh"}, after
    assert labels["Kept, renamed"] == before["Kept"], "an edited element keeps its id"
    assert after["removed"] == [before["Old"]]
