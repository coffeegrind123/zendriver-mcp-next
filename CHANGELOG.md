# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Tools whose names were two characters apart are renamed, because such pairs are a measured
  cause of a model calling the wrong one: `get_cookies` -> `read_page_cookies`, `set_cookie`
  -> `write_cookie`, `get_local_storage` -> `read_local_storage`, `set_local_storage` ->
  `write_local_storage`, `export_cookies` -> `save_cookies`, `import_cookies` ->
  `load_cookies`. Calls to an old name fail with `unknown_tool` and a hint naming the new one.
- Tool schemas no longer carry pydantic's `title` annotations or the `anyOf [X, null]` form
  for optional parameters: an optional parameter may be omitted (an explicit null is still
  accepted). Every tool, listed or reached through `call_tool`, states its `required` list.
- On Python 3.12, tool descriptions no longer carry their source indentation.
- `execute_js` is renamed `execute_javascript`. Calls to the old name through
  `call_tool`, `batch` or `jobs` fail with `unknown_tool` and a hint naming the new one.

### Added
- Iframes, same-origin and cross-site, nested at any depth: `get_interaction_tree` walks them
  (their elements carry a frame number `f`, and the result lists the frames), ids, clicks,
  typing and the obstruction check work inside them, a CSS selector that matches nothing in
  the page is looked up in the frames, and `get_text_content` appends each frame's text.
  Hidden and zero-size frames are skipped.
- `fill_form` fills every kind of field the way a person does, with trusted input:
  checkboxes, switches and radios (clicked only if the state must change, through the label
  when the control has no box), radio and checkbox groups by option name, `<select>` by arrow
  keys, multi-selects by Control-click, ARIA comboboxes and listboxes, ranges, dates, times,
  `datetime-local` and files. Fields can be found by `label`, ranked exact, prefix, whole
  word, substring, across frames; a tie is reported as `ambiguous`. Each field reports
  `filled`, `missing`, `ambiguous` or `failed`, its kind, and whether input was trusted.
- `get_interaction_tree(closed_shadow=true)` also walks closed shadow roots, without hooking
  anything in the page. Off by default: it roughly doubles the walk.
- The interaction tree marks required fields (`req`), lists file inputs hidden behind a
  styled button (`hid`), and counts controls in a disabled `<fieldset>` or under
  `aria-disabled` as disabled.
- Error code `ambiguous`.
- JavaScript dialogs: a tool on a tab with an open `alert`, `confirm`, `prompt` or
  `beforeunload` dialog fails at once with the new code `dialog_open`, naming it, instead of
  running into its time budget; so does the click that opened one. `handle_dialog`,
  `list_dialogs` and `set_dialog_policy` (manual, accept, dismiss). The dialog tools stay in
  every profile.
- Downloads are captured into a directory of the server's under the page's suggested name, and
  published as resources: `list_downloads`, `wait_for_download`.
- `get_interaction_tree(only_changed=true)`: only the elements added or changed since the tab's
  previous tree, and `removed` ids. Exact, because ids are stable.
- `get_text_content(format="markdown", selector=...)`: headings, nested lists, absolute links,
  tables, code blocks and quotes, from the page's main content, without hidden text, through
  open shadow roots and slots.
- Network entries carry `request_id` and `size`; `get_network_logs` filters by `errors_only`,
  `url_contains` and `types`; `get_response_body` returns a response's body (text paged,
  binary as a file) with both sets of headers and the request body.
- `wait_for_element(state=visible|attached|hidden|detached)`, `wait_for_text` (or `gone`),
  `wait_for_url` (substring, glob or `/regex/`).
- `open_tabs`: several URLs in new tabs at once, each reported, the active tab unchanged.
- `screenshot(full_page=true)` (scrolled and stitched, not `captureBeyondViewport`, which the
  page can observe as a resize) and `screenshot(selector=...)`; `save_pdf`.
- `save_session` / `load_session`: cookies plus localStorage. localStorage for sites not
  open at import is written on their next load, before their own scripts, once.
- `block_resources`: block images, media, fonts or other resource types on a tab.
- Opt-in submit guard (`ZENDRIVER_MCP_SUBMIT_GUARD=1`): clicks, Enter presses (HTML implicit
  submission, including `form=` default buttons) and `submit_form` that would submit a form,
  or press a send/pay/buy/delete/publish control, fail with `submit_blocked` until
  `allow_next_submit` lets one through. Search forms are exempt. Read-only checks in the
  isolated world; nothing in the page is patched.
- `execute_javascript` takes a `timeout` (1-115 s) for scripts that poll or retry, and a timeout
  now warns that the script may still be running in the page.

### Fixed
- `scroll(amount=N)` scrolled N percent of the window height, not N pixels as documented
  (measured: 300 scrolled to the bottom of a 2600 px page). It now scrolls N CSS pixels.
- A path with a NUL byte, or naming an existing directory, given to a tool that writes a file
  failed as an internal error; both are now refused with `invalid_argument`.
- After a cross-site navigation, a cached isolated-world context id could resolve to a
  different context in the new page (measured: an extension's content-script world), so
  `get_interaction_tree` failed with "element store missing" and helper scripts could run
  outside our world. A context is now reused only for the document it was made in.
- An element id from a page that had navigated could name a new element once the new page
  was walked. Ids are now never reused within a tab.
- Interaction-tree labels: `aria-labelledby` with several ids, ids inside a shadow root, and
  `label[for]` with an id that is not a valid CSS selector (which failed the whole walk).
- The interaction tree listed `inert` and `aria-hidden` content and controls outside an open
  modal dialog, and dropped a visible child of a `visibility:hidden` parent.
- An extensions directory that could not be written (a sandbox, or a wheel installed in
  site-packages) failed the whole browser launch. Extensions now live in the user cache,
  fall back to the temp dir, and are loaded read-only when already installed.
- Inside a read-only sandbox the extensions still failed to load: Chrome needs to write
  into an unpacked extension (indexed rulesets) and refuses one holding `_metadata`.
  Provisioning strips `_metadata`, and a read-only install is copied somewhere writable.
- Tests that stood up a silent endpoint took a minute each on Python 3.12.

## [0.5.0] - 2026-11-08

First release as a standalone project.

### Added
- MCP Python SDK 2.3. One server answers the 2026-07-28 protocol (stateless,
  `server/discover`, `subscriptions/listen`) and the 2024-11-05 to 2025-11-25 handshake.
- A fixed tool list: the core browse loop plus `search_tools`, `describe_tool`,
  `call_tool`, `batch` (with `$N` result references) and `jobs` (background runs with
  progress). `ZENDRIVER_MCP_SURFACE=full` lists every tool directly.
- Annotations and titles on every tool, typed `outputSchema` with `structuredContent`,
  and `isError` results carrying `{error, code, hint}` and "did you mean" suggestions.
- Server instructions, resources (`browser://artifacts/*`, `browser://tabs`, per-tab info,
  console and network), completions, `resources/list_changed`, and three prompts.
- An optional `tab` argument on every page tool. Tabs opened by the page are adopted, and
  each caller has its own active tab.
- HTTP security: hashed bearer tokens, per-token tab ownership, Host/Origin allow-lists,
  per-token rate limits, and a refusal to bind a public address without tokens.
- Progress notifications and cancellation for long tools (Lighthouse, heap snapshots,
  traces, screencast export, waits, Cloudflare).
- A generated tool reference (`docs/tools.md`) and an opt-in live test against real Chrome.

### Changed
- Element access runs in an isolated JavaScript world. Element ids are kept there instead
  of in a `data-*` attribute, and clicks are trusted CDP input with an obstruction check.
- `execute_js` accepts statement blocks that use `return`, as well as plain expressions.
- `press_key` sends real key events; `submit_form` uses `requestSubmit()`.
- `get_cookies` / `set_cookie` use CDP and so include HttpOnly cookies.
- Proxy authentication and request interception share one Fetch session per tab.
- The package is now `zendriver_mcp`, with the console script `zendriver-mcp`.

### Fixed
- Requests paused by interception in a background tab were answered on the active tab
  and hung.
- A new tab's own document request was missing from its network log.
- `stop_trace` and `stop_screencast` acted on the active tab instead of the recording tab.
- A zendriver race raised "list.remove(x): x not in list" from unrelated commands.
- `get_page_info` reported a stale title for new tabs.
