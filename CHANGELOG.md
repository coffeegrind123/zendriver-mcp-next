# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `execute_js` takes a `timeout` (1-115 s) for scripts that poll or retry, and a timeout
  now warns that the script may still be running in the page.

### Fixed
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
