# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately, through GitHub's
[security advisory form](https://github.com/coffeegrind123/zendriver-mcp-next/security/advisories/new),
not in a public issue. Include the version or commit, how you ran the server (stdio or HTTP,
and with which options), and the steps to reproduce. You can expect an acknowledgement
within a week.

## Threat model

The server drives a real browser that may hold the user's logged-in sessions, so whoever can
call its tools can act as that user on the web.

- **stdio** (the default): only the process that started the server can talk to it.
- **Streamable HTTP**: anything that can reach the port could drive the browser. The server
  therefore:
  - requires bearer tokens, which are stored as SHA-256 hashes with mode 0600 and compared
    in constant time;
  - maps each token to its own tabs;
  - checks Host and Origin against DNS rebinding;
  - rate-limits each token;
  - refuses to bind a non-loopback address when no tokens are configured.
- **Files.** Tools that write files accept only paths under `$HOME`, the temp dir, or
  `ZENDRIVER_MCP_ARTIFACT_ROOT`. The artifact resources serve only files that this process
  wrote.
- **Out of scope.** What the agent chooses to do with the browser, and pages that attack
  the browser itself (keep Chrome up to date).
