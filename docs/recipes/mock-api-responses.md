# Mock an API response

Use this to see how a UI handles an empty list, a 500, or an offline backend, without
touching the backend. Rules belong to the tab they were created on, and each tool's result
reports how many requests a rule has matched.

```text
call_tool mock_response {"url_pattern": "*/api/orders", "status": 200, "body": "[]",
                         "headers": {"Content-Type": "application/json"}}
→ {"rule": {"id": "rule_001", "match_count": 0, ...}, "active_rules": 1}
navigate {"url": "https://app.example/orders"}
call_tool list_interceptions {}
→ {"rules": [{"id": "rule_001", "match_count": 1, ...}]}
```

## Simulate a failure

```text
call_tool fail_requests {"url_pattern": "*/api/analytics/*", "error_reason": "InternetDisconnected"}
```

`error_reason` takes CDP's network error names: `Failed`, `Aborted`, `TimedOut`,
`AccessDenied`, `ConnectionClosed`, `ConnectionReset`, `ConnectionRefused`,
`ConnectionAborted`, `ConnectionFailed`, `NameNotResolved`, `InternetDisconnected`,
`AddressUnreachable`, `BlockedByClient` and `BlockedByResponse`.

## Remove rules

```text
call_tool stop_interception {"rule_id": "rule_001"}   # one rule
call_tool stop_interception {}                        # all rules
```

When a tab's last rule is removed, interception on that tab is switched off.
Interception also works alongside an authenticated proxy: the server handles both on the
tab's one Fetch session, so each request is answered exactly once.
