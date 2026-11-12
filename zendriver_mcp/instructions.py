"""The server's ``instructions``: what a model reads before its first call.

Claude Code truncates server instructions at 4,096 characters, so this stays
well under (tests/test_instructions.py enforces 3,500) and puts what matters
first. Every tool name in it must exist: the test checks that too, because the
old INSTRUCTIONS.md named nine tools that did not.
"""

INSTRUCTIONS = """\
Controls one real, headed Chrome (zendriver, CDP) that is built to look like a person's browser, \
not automation. One browser per server; tabs are shared with anyone else using this server.

Loop: navigate(url) -> get_interaction_tree -> click / type_text / press_key -> \
get_text_content or screenshot. navigate waits for the page to settle; do not add a wait after it. \
Use wait_for_element after a click that loads something.

Browser lifecycle: start_browser first (unless the server autostarts), stop_browser when done. \
Headless is refused: every launch is headed, because headless is what bot detection looks for.

Elements: tools take a CSS selector or a numeric id from get_interaction_tree. Ids stay valid \
while the element exists and expire when the page navigates; a stale id fails with code stale_id, \
so re-run get_interaction_tree. Clicks are real mouse events at the element's centre; if something \
covers it the click fails with element_obscured and names the cover (pass force=true to click anyway).

Tabs: every page tool takes an optional tab="tab_N" to act on that tab for one call without \
switching. list_tabs shows tabs the page opened itself (popups, target=_blank) too.

More tools: only the core loop is listed. Find the rest with search_tools (cookies, storage, \
network logs and mocking, blocking, headers, proxy, emulation of device/network/geolocation/timezone/\
locale, permissions, accessibility tree, tracing, heap snapshots, Lighthouse, screencasts, \
Cloudflare challenges, security audit, human-like input, shadow DOM). Run one with call_tool, \
a known sequence with batch (later steps read earlier results as "$N.field"), and anything slow \
with jobs(action="start") then jobs(action="wait").

Results: every tool returns structured fields plus a text summary. Errors carry a code and a hint; \
follow the hint rather than retrying blindly. Files a tool writes (screenshots to disk, traces, \
videos, exports) come back as browser://artifacts/ resource links.

execute_javascript runs in the page's own JavaScript world and is visible to the page; prefer the dedicated \
tools, which run where the page cannot see them.
"""
