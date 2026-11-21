# Zendriver MCP

An MCP server that gives an AI agent a real, headed Chrome, driven over the Chrome DevTools
Protocol, without the side effects that let a page notice automation.

- [Quick start and configuration](https://github.com/coffeegrind123/zendriver-mcp-next#quick-start):
  install, client setup, environment variables, HTTP mode.
- [Tool reference](tools.md): every tool, with its effect, parameters and summary. Generated
  from the code.
- [Architecture](architecture.md): how calls flow, the error model, element access, tabs, and
  what a page can observe.
- Recipes:
    - [Work a long page without reading all of it](recipes/long-pages.md)
    - [Get past a Cloudflare challenge](recipes/beat-cloudflare.md)
    - [Mock an API response](recipes/mock-api-responses.md)
    - [Audit a page behind a login](recipes/performance-audit.md)
    - [Log in once, reuse the session](recipes/scrape-behind-login.md)

## The loop

```text
start_browser → navigate → get_interaction_tree → click / type_text / press_key
             → get_text_content or screenshot → stop_browser
```

Only the core loop appears in `tools/list`. Everything else is reached with `search_tools`
and run with `call_tool`, `batch` (several steps, with later steps reading earlier results)
or `jobs` (background runs with progress). Every result is structured, and every failure
carries a `code` and a `hint`.
