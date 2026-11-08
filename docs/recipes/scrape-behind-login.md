# Log in once, then reuse the session

When a login involves 2FA or a CAPTCHA, let a person do it once in the visible browser.
Save the cookies, and every later run starts already signed in.

## First run: log in and save

```text
start_browser {}
navigate {"url": "https://example.com/login"}
# a person signs in in the browser window, including any 2FA
wait_for_element {"selector": "#dashboard", "timeout": 120}
call_tool export_cookies {"file_path": "~/.sessions/example.json"}
→ {"count": 23, "files": [{"path": "/home/me/.sessions/example.json", "uri": "browser://artifacts/example.json", ...}]}
```

The export covers the whole browser cookie jar, HttpOnly session cookies included. The
file is written with mode 0600.

Alternatively, `start_browser {"user_data_dir": "/path/to/profile"}` keeps a complete
persistent profile with cookies, storage and saved logins.

## Later runs: reuse

```text
start_browser {}
call_tool import_cookies {"file_path": "~/.sessions/example.json"}
navigate {"url": "https://example.com/dashboard"}
get_text_content {}
```

## When it stops working

Cookies expire, and Chrome caps any cookie's lifetime at 400 days. Watch for 401 responses
in `get_network_logs`, then repeat the first run. Exporting again at the end of each run
keeps the latest refreshed session.
