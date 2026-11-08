# Zendriver MCP

[![CI](https://github.com/coffeegrind123/zendriver-mcp-next/actions/workflows/ci.yml/badge.svg)](https://github.com/coffeegrind123/zendriver-mcp-next/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![MCP](https://img.shields.io/badge/MCP-2026--07--28%20%7C%202025--11--25-6f42c1)

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives an AI agent
a real, headed Chrome, driven over the Chrome DevTools Protocol through
[zendriver](https://github.com/cdpdriver/zendriver), and built so the page it is looking at
cannot tell an agent is there.

- **98 browser tools** behind a small fixed tool list: navigation, elements, forms, tabs,
  cookies and storage, network logs and mocking, emulation, accessibility, tracing,
  Lighthouse, screencasts and more.
- **Hard to detect by design.** Helper scripts and element ids live in an isolated JavaScript
  world, nothing is written into the DOM, and clicks and keys are trusted input events.
- **Built for the current MCP spec.** It serves 2026-07-28 and the 2025 handshake versions
  from one server. Every tool returns structured results, carries annotations, and fails with
  an error code and a recovery hint.

---

## Contents

- [Quick start](#quick-start)
- [How an agent uses it](#how-an-agent-uses-it)
- [Tools](#tools)
- [What a page can and cannot see](#what-a-page-can-and-cannot-see)
- [Configuration](#configuration)
- [Running over HTTP](#running-over-http)
- [Protocol support](#protocol-support)
- [Architecture](#architecture)
- [Development](#development)
- [Acknowledgements](#acknowledgements)
- [License](#license)

## Quick start

**Requirements:** Python 3.10+, Google Chrome or Chromium, and a display. On a headless Linux
host, run Xvfb (`Xvfb :99 &` and `DISPLAY=:99`). The server never runs Chrome headless; see
[why](#what-a-page-can-and-cannot-see).

**Claude Code**

```sh
claude mcp add browser -- uvx --from git+https://github.com/coffeegrind123/zendriver-mcp-next zendriver-mcp
```

**Any MCP client** (stdio):

```json
{
  "mcpServers": {
    "browser": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/coffeegrind123/zendriver-mcp-next", "zendriver-mcp"],
      "env": { "DISPLAY": ":99" }
    }
  }
}
```

**From a checkout**

```sh
git clone https://github.com/coffeegrind123/zendriver-mcp-next
cd zendriver-mcp-next
uv sync
uv run zendriver-mcp            # stdio
uv run zendriver-mcp --help     # transports, tokens, options
```

## How an agent uses it

The server sends the model short instructions when it connects. The core loop is:

```
start_browser → navigate(url) → get_interaction_tree → click / type_text / press_key
             → get_text_content or screenshot → stop_browser
```

- **`navigate` waits for the page to settle.** It waits for `document.readyState` to reach
  complete, then for the network to go idle, and reports which of the two happened.
- **Elements** are addressed by CSS selector, or by the numeric id that
  `get_interaction_tree` assigns. Ids stay valid while the element exists. A stale id fails
  with code `stale_id` instead of hitting the wrong element.
- **Tabs.** Every page tool takes an optional `tab="tab_2"`, which acts on that tab for one
  call without switching to it. Tabs the page opens itself (popups, `target=_blank`) are
  picked up automatically.
- **Everything else is one search away.** `search_tools("block requests")` finds the tool,
  and `call_tool` runs it.
- **`batch`** runs a known sequence in one call. A later step can read an earlier result as
  `"$N.field"`, or as `"${N.field}"` inside a longer string.
- **`jobs`** runs a slow tool in the background (Lighthouse, heap snapshots, video export)
  and records its progress.

Every result has structured content plus a text summary. Every failure is an `isError` result
shaped like this:

```json
{ "error": "Something covers #buy at (640, 412): <div#cookie-banner>.",
  "code": "element_obscured",
  "hint": "Close the overlay on top (a cookie banner, a modal) or scroll so the element is unobstructed. force=true clicks at that point anyway." }
```

## Tools

The default `tools/list` has 19 entries: the core loop below, plus `search_tools`,
`describe_tool`, `call_tool`, `batch` and `jobs`. The core tools are tagged
`anthropic/alwaysLoad` so that Claude Code's tool search never defers them. Set
`ZENDRIVER_MCP_SURFACE=full` to list all 103 directly.

| Core (always listed) | Reached through `search_tools` |
|---|---|
| `start_browser` `stop_browser` `get_browser_status` `navigate` `get_interaction_tree` `click` `type_text` `press_key` `get_text_content` `screenshot` `execute_js` `wait_for_element` `list_tabs` `scroll` | tabs and history · element queries · shadow DOM · forms · human-like mouse and typing · per-page and whole-browser cookies · local storage · network and console logs · request blocking, extra headers, mocking and failure injection · proxy · user agent, locale, timezone, geolocation · device, CPU and network emulation · permissions · accessibility tree · tracing and heap snapshots · Lighthouse · screencast to MP4 · Cloudflare challenge handling · security audit |

The full list, with each tool's effect, parameters and summary, is in
**[docs/tools.md](docs/tools.md)**. That file is generated from the code.

**Resources.** Files that tools write are served back as resources, so a remote client can
fetch them:
- `browser://artifacts/{name}`: screenshots, traces, heap snapshots, videos, cookie exports;
- `browser://tabs`;
- `browser://tabs/{tab}/info`, `/console` and `/network`.

`{tab}` and `{name}` support completion. On 2026-07-28 clients, opening or closing a tab
publishes `resources/list_changed` to `subscriptions/listen` streams.

**Prompts.** `read_page`, `fill_form_task` and `audit_page`.

## What a page can and cannot see

Pages detect automation by looking for side effects. Each row below is a side effect that
other CDP tools leave behind and this server avoids. Each claim is checked by the live test
in `tests/test_live.py`, which records events from a listener inside the page itself.

| Signal a page can check | Common CDP tooling | This server |
|---|---|---|
| `event.isTrusted` on clicks and keys | `false` (`el.click()`, synthetic events) | `true`: real `Input.dispatchMouseEvent` and key events, with the full pointer sequence |
| Marker attributes in the DOM (`data-*` ids) | Written onto every element | None; ids live in an isolated world |
| Helper code in the page's JS world (wrapped `querySelector`, globals) | Runs in the main world | Runs in an isolated world the page cannot reach |
| Injected highlight elements and CSS rules | zendriver's `flash()` adds a `div` and `@keyframes` | Never called |
| `headless` user agent and rendering differences | Common | Refused: every launch is headed |
| `Runtime.enable` console-serialisation side channel | Historically observable | Measured not observable on Chrome 157 with the classic stack-getter probe |

The remaining exposure is documented, not hidden:
- `execute_js` runs in the page's own world by design. Prefer the dedicated tools.
- `select_option` sets a native `<select>` by script, so its `change` event is untrusted. For
  a trusted change, focus the select and use `press_key`.
- `start_browser(low_memory=true)` adds container-friendly flags such as `--disable-gpu`,
  which produce detectable software WebGL. Use it only where Chrome cannot start without it.

## Configuration

All settings are optional environment variables.

| Variable | Default | Effect |
|---|---|---|
| `ZENDRIVER_MCP_SURFACE` | `catalog` | `catalog` lists the core loop plus the meta-tools; `full` lists every tool. |
| `ZENDRIVER_MCP_PROFILE` | – | Restricts which tools exist: `minimal`, `browse`, `interact`, `scrape`, `stealth`, `audit`, `network`, `full`. |
| `ZENDRIVER_MCP_GROUPS` | – | Comma-separated tool groups to keep (the group names are in docs/tools.md). |
| `ZENDRIVER_MCP_ALLOW` / `ZENDRIVER_MCP_DENY` | – | Exact tool names to keep or remove. |
| `ZENDRIVER_MCP_AUTOSTART_BROWSER` | off | Starts Chrome on the first call that needs it, and replaces a browser that died. |
| `ZENDRIVER_MCP_TOOL_TIMEOUT` | `120` | Per-call time budget, in seconds. A hung CDP call returns an error rather than hanging the session. |
| `ZENDRIVER_MCP_LAUNCH_TIMEOUT` | budget − reserve | Time allowed for Chrome to start. |
| `ZENDRIVER_MCP_ARTIFACT_ROOT` | – | Extra directories, besides `$HOME` and the temp dir, that tools may write files to. |
| `ZENDRIVER_MCP_SKIP_DISPLAY_CHECK` | off | Skips the X display pre-flight check before launching. |
| `ZENDRIVER_MCP_CONFIG_DIR` | `~/.config/zendriver-mcp` | Where HTTP tokens are stored. |

`ZENDRIVER_MCP_GATEWAY=1` and `ZENDRIVER_MCP_PROFILE=gateway` are still accepted, and mean
`SURFACE=catalog`.

## Running over HTTP

stdio gives each client its own browser. To let several clients share one long-lived browser,
use Streamable HTTP:

```sh
zendriver-mcp --create-token laptop        # prints the token once; only its hash is stored
zendriver-mcp --transport streamable-http --host 127.0.0.1 --port 8931
claude mcp add --transport http browser http://127.0.0.1:8931/mcp \
  --header "Authorization: Bearer zmcp_..."
```

- **Tokens.** Bearer tokens are compared in constant time and stored as SHA-256 hashes with
  mode 0600. Each token's name becomes the caller identity, so one client cannot see or drive
  another client's tabs.
- **Without tokens.** The server runs unauthenticated on loopback only. It refuses to bind
  any other address.
- **Host and Origin checks** protect against DNS rebinding. Add LAN names with
  `--allowed-host`.
- **Rate limit.** 600 requests per minute per token by default (`--rate-limit`), answered
  with `429` and `Retry-After`.

## Protocol support

| Spec revision | Supported | Notes |
|---|---|---|
| 2026-07-28 | yes | Stateless, `server/discover`, `subscriptions/listen` |
| 2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05 | yes | Initialize handshake; `--stateful` keeps per-client sessions |

Features used:
- tool annotations and titles;
- `outputSchema` with `structuredContent`;
- `isError` results with codes;
- resources, resource templates and completions;
- prompts;
- progress notifications, and cancellation that stops child processes;
- `resources/list_changed`;
- server instructions, kept under Claude Code's 4,096-character limit.

The `jobs` tool gives every client, whatever it supports, a way to poll long-running work.

## Architecture

```
 MCP client ──stdio / Streamable HTTP──► MCPServer (SDK 2.3)
                                          │  auth middleware: token → caller (HTTP only)
                                          ▼
                     ┌──────────── catalog.py ────────────┐
                     │ core tools · search · call · batch │
                     │ jobs · resources · prompts         │
                     └──────────────┬─────────────────────┘
                                    ▼
             tools/*.py ── ToolBase._register: annotations, budget,
                                    tab handle, Result → structured + text,
                                    exception → {error, code, hint}
                                    ▼
             dom.py: isolated world, element ids, trusted input, CDP geometry
             session.py: browser, tabs and owners, logs, Fetch (proxy auth + interception)
                                    ▼
                         zendriver ──CDP──► headed Chrome
```

## Development

```sh
uv sync --dev
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest                                   # unit, contract and protocol tests
DISPLAY=:99 ZENDRIVER_MCP_LIVE=1 uv run pytest tests/test_live.py   # real Chrome
uv run python scripts/gen_tool_docs.py          # regenerate docs/tools.md
```

The test suite enforces the conventions every tool must follow:
- every tool has a title, annotations, an output schema and described parameters;
- no tool returns an error as a success string;
- no selector is interpolated into page JavaScript;
- the default surface stays under a size budget;
- the instructions name only tools that exist and stay under the size limit.

CI runs all of this, plus the live test under Xvfb.

## Acknowledgements

Built on [zendriver](https://github.com/cdpdriver/zendriver) and the
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk). The tool set started
from [Zendriver-MCP](https://github.com/ShubhamChoulkar/Zendriver-MCP) by Shubham Choulkar;
the MCP layer, element access, error model, HTTP security and test suite were rewritten for
this project.

## License

[MIT](LICENSE)
