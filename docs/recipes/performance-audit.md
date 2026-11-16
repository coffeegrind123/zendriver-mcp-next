# Audit a page behind a login

Lighthouse normally audits a page as a stranger would see it. To audit a logged-in page,
reuse the session's cookies, then run Lighthouse as a background job. An audit takes a
minute or more, so this keeps it inside your client's request timeout.

```text
start_browser {}
call_tool load_cookies {"file_path": "~/.sessions/example.json"}
navigate {"url": "https://example.com/dashboard"}
jobs {"action": "start", "tool": "run_lighthouse",
      "arguments": {"url": "https://example.com/dashboard", "form_factor": "mobile",
                    "categories": ["performance", "accessibility"]}}
→ {"jobs": [{"id": "job_3f2a9c01de", "state": "working"}]}
jobs {"action": "wait", "job_id": "job_3f2a9c01de", "timeout": 120}
→ {"jobs": [{"state": "completed",
             "result": {"scores": {"performance": 74, "accessibility": 92}, "files": [...]}}]}
```

The report file appears in `files` both as a path and as a `browser://artifacts/` URI, which
you can read with `resources/read`.

## Trace a single interaction

To profile one interaction rather than a whole page load, record a trace:

```text
call_tool start_trace {}
call_tool human_click {"selector": "#buy-now"}
wait_for_element {"selector": "#thanks"}
call_tool stop_trace {"file_path": "/tmp/buy-trace.json"}
```

Load the JSON in Chrome DevTools' Performance panel to see the flame chart.
