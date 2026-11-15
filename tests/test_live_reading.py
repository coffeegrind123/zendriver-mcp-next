"""Reading a page as Markdown, in a real Chrome.

Plain innerText flattens a page: headings, list nesting, link targets and table
columns are gone, and an agent reading documentation or a results table has to
guess the structure back. format="markdown" keeps it, from the page's main
content, without hidden text.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.live_harness import Sites, live, sc, with_browser

pytestmark = live

ARTICLE = """<!doctype html><title>doc</title>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<main>
<h1>Install guide</h1>
<p>Read the <a href="/docs/start">getting started</a> page first. <strong>Important:</strong> back up.</p>
<ul><li>Linux<ul><li>Debian</li><li>Fedora</li></ul></li><li>macOS</li></ul>
<ol start="3"><li>Third</li><li>Fourth</li></ol>
<table><tr><th>Option</th><th>Default</th></tr><tr><td>mode</td><td>a|b</td></tr></table>
<pre><code class="language-sh">uv sync
uv run app</code></pre>
<blockquote>Quoted line</blockquote>
<p style="display:none">Hidden paragraph</p>
<details><summary>More</summary><p>Inside closed details</p></details>
<p>Inline <code>x = 1</code> and an <img src="/logo.png" alt="Logo"> image.</p>
<x-card><span slot="title">Slotted title</span></x-card>
</main>
<footer>Footer text</footer>
<script>
customElements.define('x-card', class extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: 'open'}).innerHTML =
    '<h2><slot name="title"></slot></h2><p>Shadow body</p>'; }
});
</script>"""


async def _scenario(c: Any, sites: Sites) -> dict[str, Any]:
    sc(await c.call_tool("navigate", {"url": f"{sites.a}/doc.html", "settle": 1}))
    md = sc(await c.call_tool("get_text_content", {"format": "markdown", "max_chars": 20000}))
    table = sc(await c.call_tool("get_text_content", {"format": "markdown", "selector": "table"}))
    plain = sc(await c.call_tool("get_text_content", {"selector": "nav"}))
    return {"md": md["text"], "table": table["text"], "plain": plain["text"], "base": sites.a}


@pytest.fixture(scope="module")
def run() -> dict[str, Any]:
    sites = Sites({"/doc.html": ARTICLE})
    try:
        return asyncio.run(with_browser(lambda c: _scenario(c, sites)))
    finally:
        sites.close()


def test_reads_the_main_content_only(run: dict[str, Any]) -> None:
    md = run["md"]
    assert md.startswith("# Install guide"), md
    assert "Home" not in md and "Footer text" not in md


def test_structure_is_kept(run: dict[str, Any]) -> None:
    md, base = run["md"], run["base"]
    assert f"[getting started]({base}/docs/start)" in md
    assert "**Important:**" in md
    assert "- Linux\n  - Debian\n  - Fedora\n- macOS" in md
    assert "3. Third\n4. Fourth" in md
    assert "| Option | Default |\n| --- | --- |\n| mode | a\\|b |" in md
    assert "```sh\nuv sync\nuv run app\n```" in md
    assert "> Quoted line" in md
    assert "`x = 1`" in md
    assert f"![Logo]({base}/logo.png)" in md


def test_hidden_content_is_left_out(run: dict[str, Any]) -> None:
    md = run["md"]
    assert "Hidden paragraph" not in md
    assert "Inside closed details" not in md
    assert "More" in md


def test_shadow_roots_and_slots_are_followed(run: dict[str, Any]) -> None:
    assert "## Slotted title" in run["md"]
    assert "Shadow body" in run["md"]


def test_selector_reads_one_element(run: dict[str, Any]) -> None:
    assert run["table"].startswith("| Option | Default |")
    assert "Install guide" not in run["table"]
    assert run["plain"].split() == ["Home", "About"]
