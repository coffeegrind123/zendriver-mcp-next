# Contributing

## Setup

```sh
uv sync --dev
```

## Before opening a pull request

```sh
uv run ruff check . && uv run ruff format --check .
uv run mypy zendriver_mcp
uv run pytest
uv run python scripts/gen_tool_docs.py --check
DISPLAY=:99 ZENDRIVER_MCP_LIVE=1 uv run pytest tests/test_live.py   # needs Chrome + a display
```

## Adding or changing a tool

The tests enforce these rules (`tests/test_tool_standards.py`):

- Register the tool with `self._register(fn, Effect.X, "Title", ...)`. Choose the effect
  honestly: `READ` observes only, `WRITE` makes an ordinary state change, `DESTRUCTIVE`
  discards state. Set `reach=Reach.WEB` if the tool can talk to sites, and
  `page_scoped=False` if it does not act on one tab.
- Return a `Result` subclass (`zendriver_mcp/response.py`) with described fields. Optional
  fields default to `None`.
- Fail by raising an error from `zendriver_mcp/errors.py`, with a hint that says what to do
  next. Never return an error as a string.
- Reach elements only through `zendriver_mcp/dom.py` (`self.resolve`, `self.element_call`,
  `self.dom.click`). Pass values as arguments, never interpolate them into JavaScript, and
  never write to the DOM.
- Describe every parameter, with an example.
- Run `scripts/gen_tool_docs.py` and commit the updated `docs/tools.md`.

## Commit messages

Use `area: summary` in the imperative mood, at most 50 characters (for example
`dom: resolve ids in nested frames`). The body explains what changed and why.
