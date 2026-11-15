"""fill_form against every kind of control, in a real Chrome.

The old fill_form typed text into whatever it was given, so a checkbox, a
<select>, a radio group, a date or a combobox came back "filled" and was not.
Here each kind is filled by its label in one call, and the page's own listener
records every event and whether it was trusted: only the colour input, which
has no trusted route, may show untrusted events, and fill_form must say so.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

import pytest

from tests.live_harness import Sites, foreign_attrs, live, sc, with_browser

pytestmark = live

GUARD = """<script>
window.log = [];
for (const t of ['input', 'change', 'click']) addEventListener(t, (e) =>
  log.push(t + ':' + e.isTrusted + ':' + (e.target.id || e.target.name || e.target.tagName)), true);
new MutationObserver((ms) => ms.forEach((m) => m.type === 'attributes' && m.attributeName !== 'aria-expanded' &&
  !(m.target.id === 'citylist') &&
  log.push('attr:' + m.attributeName + '@' + (m.target.id || m.target.tagName)))).observe(document, {attributes: true, subtree: true});
</script>"""

FORM = f"""<!doctype html><title>form</title>{GUARD}
<body style="font:15px sans-serif">
<form id="f" onsubmit="return false">
<p><label>Full name <input name="name"></label></p>
<p><label for="em">Email address</label> <input id="em" type="email"></p>
<p><label>Email confirmation <input id="em2"></label></p>
<p><textarea id="msg" aria-label="Message"></textarea></p>
<p><div id="bio" contenteditable="true" aria-label="Bio" style="border:1px solid;min-height:20px"></div></p>
<p><label><input type="checkbox" id="agree"> I agree to the terms</label></p>
<p><label><input type="checkbox" id="news" style="opacity:0;position:absolute;width:0;height:0"><span>Newsletter</span></label></p>
<fieldset id="size"><legend>Size</legend>
  <label><input type="radio" name="size" id="s-s"> Small</label>
  <label><input type="radio" name="size" id="s-l"> Large</label>
  <label><input type="radio" name="size" id="s-xl"> Extra large</label></fieldset>
<fieldset><legend>Contact by</legend>
  <label><input type="checkbox" id="c-phone"> Phone</label>
  <label><input type="checkbox" id="c-sms" checked> SMS</label>
  <label><input type="checkbox" id="c-post"> Post</label></fieldset>
<p><label>Country <select id="country"><option value="">Choose</option><option>Denmark</option>
  <option disabled>Estonia</option><option>Finland</option><option>France</option></select></label></p>
<p><label>Languages <select id="langs" multiple size="4"><option>English</option><option>Finnish</option>
  <option>Swedish</option><option>German</option></select></label></p>
<p><label>Volume <input type="range" id="vol" min="0" max="100" step="5" value="50"></label></p>
<p><label>Birthday <input type="date" id="bd"></label></p>
<p><label>Alarm <input type="time" id="al"></label></p>
<p><label>Lunch <input type="time" id="lunch"></label> <label>Night <input type="time" id="night"></label></p>
<p><label>Meeting <input type="datetime-local" id="meet"></label></p>
<p><label>Favourite colour <input type="color" id="col"></label></p>
<p><label>CV <input type="file" id="cv"></label></p>
<div><span id="citylbl">City</span>
  <input id="city" role="combobox" aria-labelledby="citylbl" aria-controls="citylist" aria-expanded="false">
  <ul id="citylist" role="listbox" style="list-style:none;padding:0;margin:0"></ul></div>
<p><label>Disabled field <input id="dis" disabled></label></p>
</form>
<script>
const CITIES = ['Helsinki', 'Hamburg', 'Espoo'];
const city = document.getElementById('city'), list = document.getElementById('citylist');
function show() {{
  const q = city.value.toLowerCase();
  list.replaceChildren(...CITIES.filter((c) => c.toLowerCase().includes(q)).map((c) => {{
    const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = c;
    li.style.padding = '4px'; li.style.cursor = 'pointer';
    li.addEventListener('click', () => {{ city.value = c; list.replaceChildren(); }});
    return li;
  }}));
}}
city.addEventListener('focus', show); city.addEventListener('input', show);
</script>
</body>"""

PAY = f"""<!doctype html><title>pay</title>{GUARD}
<pre id="logview"></pre><script>
const push = log.push.bind(log);
log.push = (x) => {{ push(x); document.getElementById('logview').textContent = 'LOG ' + log.join(' ') +
  ' VALUE ' + document.getElementById('card').value; }};
</script>
<label>Card number <input id="card"></label>"""

PAGES = {
    "/form.html": FORM,
    "/outer.html": "<!doctype html><title>outer</title><p>Checkout</p>"
    '<iframe src="{b}/pay.html" style="width:500px;height:200px"></iframe>',
    "/pay.html": PAY,
}

READ = """return {
  name: document.querySelector('[name=name]').value, em: em.value, em2: em2.value, msg: msg.value,
  bio: bio.textContent, agree: agree.checked, news: news.checked,
  size: [...document.querySelectorAll('[name=size]')].filter((r) => r.checked).map((r) => r.id),
  contact: ['c-phone', 'c-sms', 'c-post'].filter((i) => document.getElementById(i).checked),
  country: country.value, langs: [...langs.selectedOptions].map((o) => o.text), vol: vol.value,
  bd: bd.value, al: al.value, lunch: lunch.value, night: night.value, meet: meet.value, col: col.value, cv: cv.files.length ? cv.files[0].name : null,
  city: city.value, dis: dis.value, log: window.log };"""


async def _scenario(c: Any, sites: Sites, upload: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sc(await c.call_tool("navigate", {"url": f"{sites.a}/form.html", "settle": 2}))
    fields = [
        {"label": "Full name", "value": "Ada Lovelace"},
        {"label": "Email address", "value": "ada@example.com"},
        {"label": "Message", "value": "Hello"},
        {"label": "Bio", "value": "Mathematician"},
        {"label": "I agree to the terms", "checked": True},
        {"label": "Newsletter", "checked": True},
        {"label": "Size", "value": "Large"},
        {"label": "Contact by", "options": ["Phone", "Post"]},
        {"label": "Country", "value": "Finland"},
        {"label": "Languages", "options": ["English", "Swedish"]},
        {"label": "Volume", "value": 70},
        {"label": "Birthday", "value": "1815-12-10"},
        {"label": "Alarm", "value": "07:45"},
        {"label": "Lunch", "value": "12:30"},
        {"label": "Night", "value": "00:15"},
        {"label": "Meeting", "value": "2026-11-15T09:05"},
        {"label": "Favourite colour", "value": "#ff8800"},
        {"label": "CV", "files": [upload]},
        {"label": "City", "value": "Espoo"},
    ]
    out["fill"] = sc(
        await c.call_tool("call_tool", {"name": "fill_form", "arguments": {"fields": fields}})
    )
    out["page"] = sc(await c.call_tool("execute_javascript", {"script": READ}))["value"]

    out["refusals"] = sc(
        await c.call_tool(
            "call_tool",
            {
                "name": "fill_form",
                "arguments": {
                    "form_data": {"#em2": "legacy@example.com"},
                    "fields": [
                        {"label": "Email", "value": "x"},
                        {"label": "No such field", "value": "x"},
                        {"label": "Disabled field", "value": "x"},
                        {"label": "Country", "value": "Estonia"},
                        {"label": "Size", "value": "Medium"},
                    ],
                },
            },
        )
    )
    out["page2"] = sc(await c.call_tool("execute_javascript", {"script": READ}))["value"]

    # Backwards through the list, past a disabled option.
    out["back"] = sc(
        await c.call_tool(
            "call_tool",
            {
                "name": "fill_form",
                "arguments": {"fields": [{"target": "#country", "value": "Denmark"}]},
            },
        )
    )
    out["page3"] = sc(await c.call_tool("execute_javascript", {"script": READ}))["value"]

    sc(await c.call_tool("navigate", {"url": f"{sites.a}/outer.html", "settle": 2}))
    out["frame_fill"] = sc(
        await c.call_tool(
            "call_tool",
            {
                "name": "fill_form",
                "arguments": {"fields": [{"label": "Card number", "value": "4242 4242"}]},
            },
        )
    )
    out["frame_text"] = sc(await c.call_tool("get_text_content", {}))["text"]
    return out


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    sites = Sites(PAGES)
    fd, upload = tempfile.mkstemp(suffix=".pdf", prefix="cv-")
    os.write(fd, b"%PDF-1.4\n")
    os.close(fd)
    try:
        return asyncio.run(with_browser(lambda c: _scenario(c, sites, upload)))
    finally:
        sites.close()
        os.unlink(upload)


def _outcomes(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {o["field"]: o for o in result["fields"]}


def test_every_field_is_filled(run: dict[str, Any]) -> None:
    fill = run["fill"]
    bad = [o for o in fill["fields"] if o["status"] != "filled"]
    assert not bad, bad


def test_the_page_holds_the_values(run: dict[str, Any]) -> None:
    p = run["page"]
    assert p["name"] == "Ada Lovelace"
    assert p["em"] == "ada@example.com"
    assert p["msg"] == "Hello"
    assert p["bio"] == "Mathematician"
    assert p["agree"] is True
    assert p["news"] is True, "a hidden custom checkbox is set through its label"
    assert p["size"] == ["s-l"], "'Large' is Large, not Extra large"
    assert p["contact"] == ["c-phone", "c-post"], "exactly the options given; SMS is unchecked"
    assert p["country"] == "Finland"
    assert p["langs"] == ["English", "Swedish"]
    assert p["vol"] == "70"
    assert p["bd"] == "1815-12-10"
    assert p["al"] == "07:45"
    assert p["lunch"] == "12:30"
    assert p["night"] == "00:15"
    assert p["meet"] == "2026-11-15T09:05"
    assert p["col"] == "#ff8800"
    assert p["cv"] and p["cv"].startswith("cv-")
    assert p["city"] == "Espoo"


def test_input_is_trusted_except_where_reported(run: dict[str, Any]) -> None:
    outcomes = _outcomes(run["fill"])
    untrusted = {f for f, o in outcomes.items() if o["trusted"] is False}
    assert untrusted == {"label='Favourite colour'"}, untrusted
    log = run["page"]["log"]
    false = [e for e in log if ":false:" in e]
    assert all(e.endswith(":col") for e in false), false
    assert not foreign_attrs(log), log


def test_kinds_are_reported(run: dict[str, Any]) -> None:
    kinds = {f: o["kind"] for f, o in _outcomes(run["fill"]).items()}
    assert kinds["label='Size'"] == "group"
    assert kinds["label='Country'"] == "select"
    assert kinds["label='Languages'"] == "multiselect"
    assert kinds["label='City'"] == "combobox"
    assert kinds["label='Birthday'"] == "date"


def test_ambiguous_missing_and_failed_are_told_apart(run: dict[str, Any]) -> None:
    r = run["refusals"]
    by = _outcomes(r)
    assert by["#em2"]["status"] == "filled"
    assert run["page2"]["em2"] == "legacy@example.com"
    assert by["label='Email'"]["status"] == "ambiguous"
    assert "Email address" in by["label='Email'"]["detail"]
    assert by["label='No such field'"]["status"] == "missing"
    assert by["label='Disabled field'"]["status"] == "failed"
    assert "disabled" in by["label='Disabled field'"]["detail"]
    assert by["label='Country'"]["status"] == "failed"
    assert "disabled" in by["label='Country'"]["detail"]
    assert by["label='Size'"]["status"] == "failed"
    assert "Small" in by["label='Size'"]["detail"], "names the options that do exist"
    assert run["page2"]["country"] == "Finland", "a refused option leaves the field alone"
    assert run["page2"]["dis"] == ""


def test_select_moves_backwards_past_a_disabled_option(run: dict[str, Any]) -> None:
    (o,) = run["back"]["fields"]
    assert o["status"] == "filled" and o["trusted"] is True, o
    assert run["page3"]["country"] == "Denmark"


def test_a_field_in_a_cross_site_iframe_is_found_by_label(run: dict[str, Any]) -> None:
    assert run["frame_fill"]["filled"] == ["label='Card number'"], run["frame_fill"]
    text = run["frame_text"]
    assert "VALUE 4242 4242" in text, text
    assert "input:true:card" in text
    assert ":false:" not in text
