# Get past a Cloudflare challenge

A headed Chrome with trusted input usually passes Cloudflare without help. When an
interactive Turnstile checkbox does appear, check for it first and then solve it.

```text
navigate                        {"url": "https://protected.example"}
call_tool is_cloudflare_challenge_present   {"timeout": 5}
→ {"present": true, ...}
call_tool bypass_cloudflare     {"timeout": 20, "click_delay": 4}
→ {"solved": true, "attempts": 1, "elapsed": 6.2}
```

`bypass_cloudflare` reports progress for each attempt. Run it through
`jobs(action="start")` if your client's request timeout is shorter than the challenge.

## Tips

- **Don't call the solver when there is no challenge.** It waits the whole `timeout` and
  then fails with `challenge_unsolved`. Check first.
- **Keep `click_delay` realistic.** 3 to 5 seconds looks like a person reading the page.
- **Still blocked?** The challenge is only one signal. IP reputation matters too, so try
  `configure_proxy` with a residential exit.
