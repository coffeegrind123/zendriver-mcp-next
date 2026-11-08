# Architecture

## Call flow

```
client ──► MCPServer (MCP Python SDK 2.3; 2026-07-28 and 2025 handshake versions)
             │  HTTP only: AuthMiddleware sets the caller from the bearer token
             ▼
           ToolBase._register wrapper (one per tool)
             · resolves the optional `tab` argument into a per-call tab
             · runs the tool under its time budget; a wedged Chrome is detected and replaced
             · Result  → structuredContent + text (+ images, resource links)
             · raise   → isError {error, code, hint}
             ▼
           tool module (zendriver_mcp/tools/*.py)
             ▼
           dom.py (element access)   session.py (browser, tabs, logs, Fetch)
             ▼
           zendriver ──CDP──► Chrome (always headed)
```

## Tool surface

`tools/list` is fixed for the life of the process. The 2026-07-28 spec forbids varying it
per connection, and agents cannot use tools that appear and disappear. The default surface
is the core browse loop plus five meta-tools:

| Meta-tool | Purpose |
|---|---|
| `search_tools` | Lexical search over every tool's name, group and description, at `names`, `signatures` or `full` detail |
| `describe_tool` | Input schema, output schema and annotations of one tool |
| `call_tool` | Runs any tool, with the same validation, error model and results as calling it directly |
| `batch` | Runs up to 50 steps in order. `"$N.path"` and `"${N.path}"` read earlier steps' structured results. Stops at the first failure unless told not to. Nothing is rolled back, because a browser cannot undo a click. |
| `jobs` | Runs a tool in the background (`start`), then `get`, `wait` (up to 120 s), `cancel` or `list`. Progress the tool reports is recorded on the job, and finished jobs are kept for an hour. |

Core tools carry `_meta["anthropic/alwaysLoad"]`, so Claude Code's deferred tool search
keeps them loaded. `ZENDRIVER_MCP_SURFACE=full` lists everything instead.

## Results and errors

Every tool returns a pydantic `Result` subclass. Its JSON Schema is the tool's
`outputSchema`. The text content is a summary line followed by the remaining fields as
compact JSON; tools whose payload is prose (page text, HTML) render it as plain text.

Failures are exceptions carrying a stable `code` (`zendriver_mcp/errors.py`), sent as
`isError` results with `{error, code, hint}`. Hints say what to do next, and mistyped names
(tools, tabs, devices, permissions) get "did you mean" suggestions. Unexpected exceptions
keep their type and message, because a bare "internal error" gives an agent nothing to work
with.

## Element access

All DOM work goes through `zendriver_mcp/dom.py`:

- **Isolated world.** Helper scripts run in a `Page.createIsolatedWorld` context. It shares
  the DOM with the page, but not the page's JavaScript heap, so a page that wraps
  `querySelector` or reads globals sees nothing. This was measured on Chrome before it was
  relied on: creating the world again with the same name returns the same context, so state
  persists; after a navigation the context is gone ("Cannot find context"), which is exactly
  when element ids should expire.
- **Element ids.** `get_interaction_tree` numbers elements in a Map inside that world. An
  element keeps its id for as long as it exists, and an id is never reused. A stale id fails
  with `stale_id`. Nothing is written into the DOM.
- **Clicks** use CDP's own geometry (`DOM.scrollIntoViewIfNeeded`, `DOM.getContentQuads`),
  then send `Input.dispatchMouseEvent` move, press and release events. The page receives
  trusted pointer, mouse and click events. Before clicking, the server checks what is
  actually under the point. A covering element fails with `element_obscured` and is named
  in the error. A label or wrapper of the target counts as the target, so custom checkboxes
  still work. `force=true` clicks anyway.
- **Arguments** reach scripts as CDP call arguments, never by interpolating strings into
  JavaScript.

`execute_js` deliberately runs in the page's own world, so it can read page globals.

## Tabs and callers

Tabs get ids `tab_N` that are never reused. Tabs the page opens itself are adopted the next
time the tab list is read, and they inherit the owner of the tab that opened them. Log
entries record their tab.

Over HTTP, each token's name is the caller. A caller sees and drives only its own tabs, and
has its own active tab. On stdio there is a single caller.

The optional `tab` argument sets the tab for one call, through a context variable. The
active tab doesn't move, and concurrent calls on different tabs don't interfere.

## Fetch ownership

Proxy authentication and request interception both need CDP's Fetch domain, and a tab has
only one Fetch configuration. The session owns it. Interception registers a per-tab
interceptor, and the session's single `RequestPaused` handler asks the interceptor first,
then continues the request otherwise. Every paused request is answered exactly once, on the
tab that paused it.

## Robustness

- Every call has a time budget (`ZENDRIVER_MCP_TOOL_TIMEOUT`). If the budget runs out and
  Chrome no longer answers its DevTools endpoint, the session is discarded and the error
  says so.
- A Chrome that dies at launch is reported with Chrome's own stderr, not as a timeout. A
  missing X display is caught before launch.
- `zendriver_mcp/compat.py` patches two zendriver failure modes: CDP parser errors that left
  callers waiting forever, and a race when removing domains that raised from unrelated
  commands.
