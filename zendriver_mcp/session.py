# Browser session management for Zendriver MCP server.
# Includes CDP event listeners for network and console logging.

import asyncio
import functools
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import zendriver as zd
from zendriver import cdp

from zendriver_mcp import dom as _dom
from zendriver_mcp.errors import (
    BrowserNotStartedError,
    PageNotLoadedError,
    TabForbiddenError,
    TabNotFoundError,
)
from zendriver_mcp.launch import (
    connection_max_tries,
    launch_budget,
    launch_supervised,
    preflight_display,
    resolve_headless,
    tool_timeout_budget,
)

logger = logging.getLogger(__name__)

# Slow container Chrome can take several seconds to open the CDP port, so each
# connect attempt waits a full second rather than zendriver's default 0.25s.
BROWSER_CONNECTION_TIMEOUT = 1.0

# How long the liveness probe waits for Chrome's DevTools HTTP endpoint to say
# anything at all. Short on purpose: it runs on a call that has ALREADY spent its
# whole budget, and the failure it looks for is not a slow answer but no answer.
CDP_PROBE_TIMEOUT = 2.0

__all__ = ["CALL_TAB", "CALLER", "BrowserSession", "ResetCallback", "tool_timeout_budget"]

# The tab ONE tool call acts on, when it passed `tab=`. A ContextVar rather than
# swapping the active tab: concurrent calls on different tabs (a shared HTTP
# server, or a client that pipelines) each see their own, and the active tab
# the user picked with switch_tab never moves under them.
CALL_TAB: ContextVar["zd.Tab | None"] = ContextVar("zendriver_call_tab", default=None)

# Who is calling, on a shared HTTP server with tokens: the token's id. None on
# stdio (one client per process) and on an unauthenticated server. Tabs record
# the caller that opened them, and one caller cannot reach another's tabs.
CALLER: ContextVar[str | None] = ContextVar("zendriver_caller", default=None)

# A reset callback is invoked on every stop_browser to clear any
# session-bound state a tool was holding (interception handlers,
# accessibility uid tables, trace buffers, screencast handles, ...).
ResetCallback = Callable[[], None]

# A request interceptor: answers a paused request and returns True, or returns
# False to let the session continue it unchanged.
Interceptor = Callable[[cdp.fetch.RequestPaused], Awaitable[bool]]


async def _cdp_answers(host: str, port: int) -> bool:
    """Did Chrome's DevTools HTTP endpoint send us a status line?

    Raw sockets rather than an HTTP client because the failure being detected is
    a connection that OPENS and then stays silent — every client library models
    that as "still waiting", which is exactly the answer that was useless for 22
    hours. Reading one line is enough: bytes arriving at all means the browser's
    main thread is running its message loop.
    """
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(
            f"GET /json/version HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Connection: close\r\n\r\n".encode()
        )
        await writer.drain()
        return (await reader.readline()).startswith(b"HTTP/")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # a half-dead socket must not mask the verdict
            pass


class BrowserSession:
    """Singleton class to manage browser session across all tool calls."""

    default_browser_path: str | None = None
    _instance: "BrowserSession | None" = None
    _browser: zd.Browser | None = None
    # The active tab, per caller: on a shared HTTP server each token has its
    # own (one client's new_tab or switch_tab must not move another's), and on
    # stdio there is one caller, None. Read and written through `_page`.
    _active: dict[str | None, zd.Tab] = {}
    _tabs: dict[str, zd.Tab] = {}
    _tab_owner: dict[str, str | None] = {}
    _tab_counter: int = 0
    _network_logs: list[dict[str, Any]] = []
    _console_logs: list[dict[str, Any]] = []
    _pending_requests: dict[str, dict[str, Any]] = {}
    _cdp_enabled_tabs: dict[int, bool] = {}  # Track tabs with CDP listeners
    _proxy_username: str | None = None
    _proxy_password: str | None = None
    _loaded_extensions: list[dict[str, str]] = []
    _reset_callbacks: list[ResetCallback] = []
    _tab_listeners: list[Callable[[], None]] = []
    _interceptors: dict[int, "Interceptor"] = {}
    _fetch_state: dict[int, tuple[bool | None, list[tuple[Any, Any]]]] = {}

    def __new__(cls) -> "BrowserSession":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._tabs = {}
            cls._active = {}
            cls._tab_owner = {}
            cls._network_logs = []
            cls._console_logs = []
            cls._pending_requests = {}
            cls._cdp_enabled_tabs = {}
            cls._reset_callbacks = []
            cls._tab_listeners = []
            cls._interceptors = {}
            cls._fetch_state = {}
        return cls._instance

    def register_reset_callback(self, callback: ResetCallback) -> None:
        """Register a zero-arg callback that clears a tool's session state.

        Called once during ``stop_browser`` so the next session starts clean.
        Stateful tools register from their ``__init__`` (via ToolBase, which
        auto-registers any ``_reset_state`` method they define).
        """
        if callback not in self._reset_callbacks:
            self._reset_callbacks.append(callback)

    def _run_reset_callbacks(self) -> None:
        for cb in self._reset_callbacks:
            try:
                cb()
            except Exception:  # one bad tool shouldn't block the rest
                logger.exception("reset callback failed: %r", cb)

    @classmethod
    def get_instance(cls) -> "BrowserSession":
        """Get the singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def browser(self) -> zd.Browser:
        """Get the browser instance."""
        if self._browser is None:
            raise BrowserNotStartedError()
        return self._browser

    def is_running(self) -> bool:
        """Whether a browser is currently open, without raising if it is not."""
        return self._browser is not None

    def is_dead(self) -> bool:
        """A browser was started and its Chrome process is gone.

        The distinction matters: with no browser at all, tools raise
        ``BrowserNotStartedError`` and say so. When Chrome dies underneath a
        live session the object is still here, so every call fails instead with
        a websocket error — ``no close frame received or sent`` — and keeps
        failing until someone notices.
        """
        return self._browser is not None and self._browser.stopped

    async def is_unreachable(self, timeout: float = CDP_PROBE_TIMEOUT) -> bool:
        """True when a browser we still hold no longer answers CDP.

        ``is_dead()`` asks a different question — whether the Chrome process
        EXITED — and a hang is not an exit. Measured 2026-09-02 in the pi
        container: Chrome's GPU process crashed, ``chrome_crashpad_handler``
        ptrace-attached to snapshot it and never finished, and the browser's main
        thread blocked in ``anon_pipe_write`` behind the same stuck handler. The
        DevTools port then ACCEPTED every TCP connection and sent zero bytes,
        forever — ``curl /json/version`` returned nothing in 22 s, and one caller
        waited 1859 s. ``browser.stopped`` was False the whole time, because the
        process was alive, so nothing upstream ever considered it broken and every
        tool call timed out identically for 22 hours.

        The discriminator is the endpoint, not the process. A refused connection
        (Chrome gone) and a silent one (Chrome wedged) both answer True here; a
        real HTTP status line answers False.

        Deliberately conservative: when the browser's websocket URL cannot be
        parsed we return False rather than claim it is broken, because discarding
        a healthy session costs a relaunch and a lost tab.
        """
        browser = self._browser
        if browser is None:
            return False
        if browser.stopped:
            return True
        url = getattr(browser, "websocket_url", None)
        if not url:
            return False
        parsed = urlparse(url)
        host, port = parsed.hostname, parsed.port
        if not host or not port:
            return False
        try:
            return not await asyncio.wait_for(_cdp_answers(host, port), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError, OSError):
            return True

    async def discard_if_unreachable(self, timeout: float = CDP_PROBE_TIMEOUT) -> bool:
        """Drop a wedged browser so the NEXT call is not the same 25s timeout.

        Returns whether anything was discarded. Separate from ``discard()`` so the
        caller can say in its error message that this happened.
        """
        if not await self.is_unreachable(timeout):
            return False
        logger.warning(
            "Chrome is alive but no longer answering CDP; discarding the session "
            "so the next call starts a fresh browser"
        )
        self.discard()
        return True

    def discard(self) -> None:
        """Drop a dead browser's state without touching the dead process.

        ``stop()`` awaits ``browser.stop()``, which cannot work when Chrome is
        already gone. This clears the same state so a fresh ``start()`` begins
        clean, including the per-tool reset callbacks.
        """
        self._browser = None
        self._forget_active()
        self._tabs = {}
        self._tab_owner = {}
        self._interceptors = {}
        self._fetch_state = {}
        _dom.forget()
        self._tabs_changed()
        self._network_logs = []
        self._console_logs = []
        self._pending_requests = {}
        self._cdp_enabled_tabs = {}
        self._loaded_extensions = []
        self._run_reset_callbacks()

    @property
    def _page(self) -> zd.Tab | None:
        return self._active.get(CALLER.get())

    @_page.setter
    def _page(self, value: zd.Tab | None) -> None:
        if value is None:
            self._active.pop(CALLER.get(), None)
        else:
            self._active[CALLER.get()] = value

    def _forget_active(self, tab: zd.Tab | None = None) -> None:
        """Drop every caller's active tab (all, or those pointing at ``tab``)."""
        if tab is None:
            self._active = {}
            return
        for caller in [c for c, t in self._active.items() if t is tab]:
            del self._active[caller]

    @property
    def page(self) -> zd.Tab:
        """The tab this call acts on: its ``tab=`` argument, else the active tab."""
        override = CALL_TAB.get()
        if override is not None:
            return override
        if self._page is None:
            raise PageNotLoadedError()
        return self._page

    @page.setter
    def page(self, value: zd.Tab) -> None:
        """Set the current page/tab."""
        self._page = value

    def is_browser_started(self) -> bool:
        """Check if browser is started."""
        return self._browser is not None

    def has_page(self) -> bool:
        """Check if a page is loaded."""
        return self._page is not None

    # Unpacked extensions auto-provisioned into extensions/<dir> on first launch.
    # asset_suffix picks the release asset; wrapper is the top-level directory
    # inside that zip, or None when the zip extracts flat (no wrapper dir).
    EXTENSIONS = (
        # uBlock Origin Lite, not uBlock Origin: gorhill/uBlock's .chromium.zip is
        # still manifest v2, and Chrome 150 refuses it outright ("Cannot install
        # extension because it uses an unsupported manifest version"), so the old
        # ublock/ dir could never have been blocking anything here. uBOL is the
        # MV3 build from the same project.
        {
            "dir": "ubol",
            "repo": "uBlockOrigin/uBOL-home",
            "asset_suffix": ".chromium.zip",
            "wrapper": None,
        },
        {
            "dir": "isdcac",
            "repo": "OhMyGuus/I-Still-Dont-Care-About-Cookies",
            "asset_suffix": "-chrome-source.zip",
            "wrapper": None,
        },
    )

    @staticmethod
    def _ensure_extension(spec: dict[str, Any]) -> str | None:
        """Download+unpack an extension if absent. Returns its dir, or None on failure.

        Extraction goes to a sibling temp dir that is renamed into place only once
        complete, so an interrupted download can never leave a half-populated
        directory that later launches would treat as already-installed.
        """
        import json
        import shutil
        import tempfile
        import urllib.request
        import zipfile

        ext_root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extensions"
        )
        ext_dir = os.path.join(ext_root, spec["dir"])
        if os.path.isfile(os.path.join(ext_dir, "manifest.json")):
            return ext_dir
        if os.path.isdir(ext_dir):
            shutil.rmtree(ext_dir, ignore_errors=True)  # salvage a broken install

        os.makedirs(ext_root, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=spec["dir"] + ".", dir=ext_root)
        zip_path = os.path.join(staging, "download.zip")
        try:
            api = "https://api.github.com/repos/%s/releases/latest" % spec["repo"]
            with urllib.request.urlopen(api, timeout=60) as r:
                meta = json.loads(r.read())
            url = next(
                a["browser_download_url"]
                for a in meta["assets"]
                if a["name"].endswith(spec["asset_suffix"])
            )
            urllib.request.urlretrieve(url, zip_path)
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(staging)
            os.remove(zip_path)

            src = staging if spec["wrapper"] is None else os.path.join(staging, spec["wrapper"])
            if not os.path.isfile(os.path.join(src, "manifest.json")):
                raise RuntimeError(
                    "%s: no manifest.json under %s (asset layout changed?)" % (spec["dir"], src)
                )
            os.rename(src, ext_dir)
            return ext_dir
        except Exception as e:
            # Never fail the launch over an extension — log loudly and carry on
            # unextended, but say WHICH one and why, so a silent no-adblock
            # session is not mistaken for a working one.
            print(
                "[zendriver-mcp] extension '%s' unavailable: %r" % (spec["dir"], e),
                file=sys.stderr,
                flush=True,
            )
            return None
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def _load_extensions(self, ext_dirs: list[str]) -> None:
        """Install the provisioned extensions over CDP, after the browser is up.

        Reports each install by name/id (and each failure with Chrome's own
        reason) so a session running without an adblocker is never mistaken for
        a working one.
        """
        if not ext_dirs:
            return
        for ext_dir in ext_dirs:
            try:
                ext_id = await self._browser.connection.send(  # type: ignore[union-attr]
                    cdp.extensions.load_unpacked(ext_dir)
                )
                self._loaded_extensions.append({"path": ext_dir, "id": ext_id})
            except Exception as e:
                print(
                    "[zendriver-mcp] extension load failed for %s: %s" % (ext_dir, e),
                    file=sys.stderr,
                    flush=True,
                )
        if self._loaded_extensions:
            print(
                "[zendriver-mcp] extensions loaded: %s"
                % ", ".join(
                    "%s (%s)" % (os.path.basename(x["path"]), x["id"])
                    for x in self._loaded_extensions
                ),
                file=sys.stderr,
                flush=True,
            )

    async def start(
        self,
        headless: bool = False,
        user_data_dir: str | None = None,
        proxy: str | None = None,
        browser_args: list[str] | None = None,
        browser_executable_path: str | None = None,
        low_memory: bool = False,
    ) -> zd.Browser:
        """Start the browser with configuration.

        ``headless`` is accepted and ignored -- every launch is headed. This is
        the choke point every caller reaches (the two lifecycle tools, the two
        proxy tools and the autostart path), which is why the policy is applied
        here rather than in any of their signatures. See ``resolve_headless``.
        """
        if self._browser is None:
            headless = resolve_headless(headless)

            # Before spawning anything, and before the extension provisioning
            # below can go to the network: a headed launch with no reachable
            # X server is the failure this whole path used to report as a
            # timeout. See zendriver_mcp/launch.py for the measurements.
            preflight_display()

            args = list(browser_args or [])

            # Opt-in only: these are needed on root/container hosts (tiny /dev/shm
            # crashes Chrome's renderer; no GPU under Xvfb) but they're DETECTABLE
            # as automation (software WebGL etc.), so they stay off by default and
            # the caller enables them per-launch via low_memory.
            if low_memory:
                for _a in (
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                ):
                    if _a not in args:
                        args.append(_a)

            # Extensions are NOT passed via --load-extension: Chrome 137+ ignores
            # that switch outright (verified on 150.0.7871.124 — even a one-file
            # MV3 test extension never appears, and the old
            # --disable-features=DisableLoadExtensionCommandLineSwitch escape
            # hatch no longer exists). The supported route is the CDP Extensions
            # domain, which needs this flag at launch; the actual install happens
            # in _load_extensions() once the browser is up.
            ext_dirs = [d for d in (self._ensure_extension(s) for s in self.EXTENSIONS) if d]
            if ext_dirs and "--enable-unsafe-extension-debugging" not in args:
                args.append("--enable-unsafe-extension-debugging")

            if proxy:
                parsed = urlparse(proxy)
                if parsed.username:
                    self._proxy_username = parsed.username
                    self._proxy_password = parsed.password or ""
                else:
                    self._proxy_username = None
                    self._proxy_password = None
                proxy_host = parsed.hostname or ""
                proxy_port = parsed.port or 8080
                scheme = parsed.scheme or "http"
                if scheme == "socks5":
                    args.append(f"--proxy-server=socks5://{proxy_host}:{proxy_port}")
                else:
                    args.append(f"--proxy-server={proxy_host}:{proxy_port}")

            exe = browser_executable_path or self.default_browser_path

            # Not zd.start(): it builds the Browser internally and only returns
            # it once the connect loop is over, so there is no way to watch the
            # child process while it runs. launch_supervised() constructs the
            # Browser first, then supervises the same start() -- see zendriver_mcp/launch.py
            # for what that buys and what it cost to find out.
            budget = launch_budget()
            config = zd.Config(
                user_data_dir=user_data_dir,
                headless=headless,
                browser_executable_path=exe,
                browser_args=args if args else None,
                sandbox=False,
                # Slow container Chrome can take several seconds to open the CDP
                # port; zendriver's default (0.25s x 10 = 2.5s) gives up too early.
                # The try count is derived so the loop cannot outlive the budget:
                # a connect loop that runs past the tool timeout gets cancelled
                # before it can report anything, which is silence, not an error.
                browser_connection_timeout=BROWSER_CONNECTION_TIMEOUT,
                browser_connection_max_tries=connection_max_tries(
                    budget, BROWSER_CONNECTION_TIMEOUT
                ),
            )
            self._browser = await launch_supervised(config, budget)

            self._loaded_extensions = []
            await self._load_extensions(ext_dirs)

            # Clear state on new session
            self._network_logs = []
            self._console_logs = []
            self._pending_requests = {}
            self._tabs = {}
            self._tab_owner = {}
            self._tab_counter = 0

            # Harden zendriver's Transaction.__call__ against parser/enum version skew so a
            # CDP response that fails to parse (new Chrome enum value, etc.) resolves the
            # caller's future with a typed error instead of hanging it forever. This supersedes
            # the older InvalidStateError-only patch. See zendriver_mcp/compat.py.
            from zendriver_mcp import compat  # noqa: F401  (idempotent, applies patch on import)

            # Set up CDP listeners on initial tab (needed for proxy auth before first navigate)
            main_tab = self._browser.main_tab
            if main_tab:
                self._page = main_tab
                self._track(main_tab, CALLER.get())
                await self._setup_cdp_listeners(main_tab)

        return self._browser

    async def stop(self) -> None:
        """Stop the browser and clean up."""
        if self._browser is not None:
            await self._browser.stop()
            self._browser = None
            self._forget_active()
            self._tabs = {}
            self._tab_owner = {}
            self._interceptors = {}
            self._fetch_state = {}
            _dom.forget()
            self._tabs_changed()
            self._network_logs = []
            self._console_logs = []
            self._pending_requests = {}
            self._cdp_enabled_tabs = {}
            self._loaded_extensions = []
            self._run_reset_callbacks()

    async def _setup_cdp_listeners(self, tab: zd.Tab) -> None:
        """Set up CDP event listeners for network and console logging."""
        tab_id = id(tab)
        if self._cdp_enabled_tabs.get(tab_id):
            return  # Already set up

        try:
            # Enable Network domain
            await tab.send(cdp.network.enable())

            # Enable Runtime domain for console
            await tab.send(cdp.runtime.enable())

            # Fetch for proxy auth, if credentials are set (and for request
            # interception, which registers through set_request_interceptor).
            await self._ensure_fetch(tab)

            # Add event handlers, each bound to this tab's id so every log entry
            # says which tab it came from. functools.partial of a coroutine
            # function is still one to zendriver's dispatcher (it awaits it); a
            # lambda would be run in a thread and its coroutine never awaited.
            tid = self.tab_id_of(tab)
            tab.add_handler(
                cdp.network.RequestWillBeSent, functools.partial(self._on_request_sent, tab_id=tid)
            )
            tab.add_handler(
                cdp.network.ResponseReceived,
                functools.partial(self._on_response_received, tab_id=tid),
            )
            tab.add_handler(
                cdp.network.LoadingFailed, functools.partial(self._on_loading_failed, tab_id=tid)
            )
            tab.add_handler(
                cdp.runtime.ConsoleAPICalled, functools.partial(self._on_console_api, tab_id=tid)
            )

            # Mark tab as CDP-enabled
            self._cdp_enabled_tabs[tab_id] = True
        except Exception:
            # CDP not available or failed, continue without logging
            pass

    async def _on_request_sent(
        self, event: cdp.network.RequestWillBeSent, *, tab_id: str | None = None
    ) -> None:
        """Handle outgoing network requests."""
        request_id = str(event.request_id)
        self._pending_requests[request_id] = {
            "url": event.request.url,
            "method": event.request.method,
            "timestamp": datetime.now().isoformat(),
            "type": str(event.type_) if event.type_ else "unknown",
            "tab": tab_id,
        }

    async def _on_response_received(
        self, event: cdp.network.ResponseReceived, *, tab_id: str | None = None
    ) -> None:
        """Handle network responses."""
        request_id = str(event.request_id)
        pending = self._pending_requests.pop(request_id, {})

        log_entry = {
            "url": event.response.url,
            "method": pending.get("method", "GET"),
            "status": event.response.status,
            "status_text": event.response.status_text,
            "type": str(event.type_) if event.type_ else pending.get("type", "unknown"),
            "mime_type": event.response.mime_type,
            "timestamp": datetime.now().isoformat(),
            "tab": tab_id,
        }

        self._network_logs.append(log_entry)

        # Keep only last 1000 entries
        if len(self._network_logs) > 1000:
            self._network_logs = self._network_logs[-1000:]

    async def _on_loading_failed(
        self, event: cdp.network.LoadingFailed, *, tab_id: str | None = None
    ) -> None:
        """Handle failed network requests."""
        request_id = str(event.request_id)
        pending = self._pending_requests.pop(request_id, {})

        if pending:
            log_entry = {
                "url": pending.get("url", "unknown"),
                "method": pending.get("method", "GET"),
                "status": 0,
                "status_text": f"FAILED: {event.error_text or event.blocked_reason or 'unknown'}",
                "blocked_reason": str(event.blocked_reason.value) if event.blocked_reason else None,
                "canceled": bool(event.canceled),
                "type": pending.get("type", "unknown"),
                "timestamp": datetime.now().isoformat(),
                "tab": tab_id,
            }
            self._network_logs.append(log_entry)

    async def _on_console_api(
        self, event: cdp.runtime.ConsoleAPICalled, *, tab_id: str | None = None
    ) -> None:
        """Handle console API calls."""
        args_text = []
        for arg in event.args:
            if arg.value is not None:
                args_text.append(str(arg.value))
            elif arg.preview and arg.preview.properties:
                # serialize object properties for better display
                props = {p.name: p.value for p in arg.preview.properties if p.value}
                args_text.append(str(props) if props else arg.description or str(arg.type_))
            elif arg.description:
                args_text.append(arg.description)
            else:
                args_text.append(str(arg.type_))

        log_entry = {
            "type": str(event.type_),
            "text": " ".join(args_text),
            "timestamp": datetime.now().isoformat(),
            "tab": tab_id,
        }

        self._console_logs.append(log_entry)

        # Keep only last 500 entries
        if len(self._console_logs) > 500:
            self._console_logs = self._console_logs[-500:]

    # ------------------------------------------------------------------
    # Fetch: one owner per tab
    #
    # Proxy authentication and request interception both need the Fetch domain,
    # and a tab has exactly one Fetch configuration. Two independent owners
    # each calling Fetch.enable and each answering RequestPaused meant every
    # paused request was answered twice, and the later enable silently dropped
    # the auth handling. So the session owns Fetch: interception registers a
    # per-tab interceptor, and the one RequestPaused handler asks it first.

    async def set_request_interceptor(self, tab: zd.Tab, interceptor: Interceptor | None) -> None:
        """Install (or remove, with None) the request interceptor for one tab.

        The interceptor gets every paused request on that tab and returns True
        when it answered it; otherwise the session continues it unchanged.
        """
        key = id(tab)
        if interceptor is None:
            self._interceptors.pop(key, None)
        else:
            self._interceptors[key] = interceptor
        await self._ensure_fetch(tab)

    async def _ensure_fetch(self, tab: zd.Tab) -> None:
        key = id(tab)
        want_auth = bool(self._proxy_username)
        wanted = want_auth or key in self._interceptors
        state = self._fetch_state.get(key)
        if not wanted:
            if state is not None:
                for event_type, handler in state[1]:
                    handlers = tab.handlers.get(event_type)
                    if handlers and handler in handlers:
                        handlers.remove(handler)
                self._fetch_state.pop(key, None)
                try:
                    await tab.send(cdp.fetch.disable())
                except Exception:
                    pass  # tab already gone
            return
        if state is None:
            hooks = [
                (
                    cdp.fetch.RequestPaused,
                    functools.partial(self._on_fetch_request_paused, tab=tab),
                ),
                (cdp.fetch.AuthRequired, functools.partial(self._on_fetch_auth_required, tab=tab)),
            ]
            for event_type, handler in hooks:
                tab.add_handler(event_type, handler)
            domains = getattr(tab, "enabled_domains", None)
            if domains is not None and cdp.fetch not in domains:
                domains.append(cdp.fetch)
            state = (None, hooks)
        if state[0] != want_auth:
            # Re-enabling replaces the configuration, so the auth flag follows
            # the proxy without losing interception (or the other way round).
            await tab.send(
                cdp.fetch.enable(
                    handle_auth_requests=want_auth,
                    patterns=[cdp.fetch.RequestPattern(url_pattern="*")],
                )
            )
        self._fetch_state[key] = (want_auth, state[1])

    async def _on_fetch_request_paused(
        self, event: cdp.fetch.RequestPaused, *, tab: zd.Tab
    ) -> None:
        """Answer a paused request on the tab that paused it, exactly once."""
        interceptor = self._interceptors.get(id(tab))
        try:
            if interceptor is not None and await interceptor(event):
                return
            await tab.send(cdp.fetch.continue_request(request_id=event.request_id))
        except Exception:
            pass  # tab closed or browser stopped mid-request

    async def _on_fetch_auth_required(self, event: cdp.fetch.AuthRequired, *, tab: zd.Tab) -> None:
        """Answer a proxy authentication challenge."""
        try:
            if self._proxy_username:
                response = cdp.fetch.AuthChallengeResponse(
                    response="ProvideCredentials",
                    username=self._proxy_username,
                    password=self._proxy_password or "",
                )
            else:
                response = cdp.fetch.AuthChallengeResponse(response="Default")
            await tab.send(
                cdp.fetch.continue_with_auth(
                    request_id=event.request_id, auth_challenge_response=response
                )
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # tabs
    #
    # Ids are "tab_<n>", handed out once and never reused. Tabs the PAGE opens
    # (target=_blank, window.open) get ids too, on the next look at the tab
    # list, and inherit the owner of the tab that opened them.

    def _track(self, tab: zd.Tab, owner: str | None) -> str:
        for tid, t in self._tabs.items():
            if t is tab:
                return tid
        self._tab_counter += 1
        tid = f"tab_{self._tab_counter}"
        self._tabs[tid] = tab
        self._tab_owner[tid] = owner
        self._tabs_changed()
        return tid

    def add_tab_listener(self, listener: Callable[[], None]) -> None:
        """Called (synchronously) whenever the set of open tabs changes."""
        if listener not in self._tab_listeners:
            self._tab_listeners.append(listener)

    def _tabs_changed(self) -> None:
        for listener in self._tab_listeners:
            try:
                listener()
            except Exception:
                logger.exception("tab listener failed: %r", listener)

    def tab_id_of(self, tab: zd.Tab) -> str | None:
        for tid, t in self._tabs.items():
            if t is tab:
                return tid
        return None

    def _sync_tabs(self) -> None:
        """Adopt tabs the page opened, and forget tabs that were closed."""
        if self._browser is None:
            return
        live = list(self._browser.tabs)
        for tid in [tid for tid, t in self._tabs.items() if t not in live]:
            gone = self._tabs.pop(tid)
            self._tab_owner.pop(tid, None)
            _dom.forget(gone)
            self._forget_active(gone)
            self._tabs_changed()
        by_target = {getattr(t.target, "target_id", None): tid for tid, t in self._tabs.items()}
        for tab in live:
            if self.tab_id_of(tab) is not None:
                continue
            opener = getattr(tab.target, "opener_id", None)
            parent = by_target.get(opener)
            owner = self._tab_owner.get(parent) if parent else CALLER.get()
            self._track(tab, owner)

    def _visible(self, tab_id: str) -> bool:
        caller = CALLER.get()
        owner = self._tab_owner.get(tab_id)
        return caller is None or owner is None or owner == caller

    def tab_for_call(self, tab_id: str) -> zd.Tab:
        """Resolve a ``tab=`` argument, enforcing ownership on shared servers."""
        self._sync_tabs()
        tab = self._tabs.get(tab_id)
        if tab is None:
            raise TabNotFoundError(tab_id, [t for t in self._tabs if self._visible(t)])
        if not self._visible(tab_id):
            raise TabForbiddenError(tab_id)
        return tab

    async def navigate(self, url: str, new_tab: bool = False) -> zd.Tab:
        """Navigate the call's tab to a URL, or open it in a new tab."""
        browser = self.browser
        current = CALL_TAB.get() or self._page
        if new_tab or current is None:
            # Blank first, listeners, THEN the URL: listeners attached after
            # the load miss the document request itself.
            tab = await browser.get("about:blank", new_tab=new_tab)
            self._track(tab, CALLER.get())
            await self._setup_cdp_listeners(tab)
            await tab.get(url)
            if CALL_TAB.get() is None:
                self._page = tab
            return tab
        if not self._cdp_enabled_tabs.get(id(current)):
            await self._setup_cdp_listeners(current)
        await current.get(url)
        return current

    async def create_tab(self, url: str | None = None) -> tuple[str, zd.Tab]:
        """Create a new tab and return its ID."""
        browser = self.browser
        # Blank first so the listeners see the page's own document request.
        tab = await browser.get("about:blank", new_tab=True)
        tab_id = self._track(tab, CALLER.get())
        await self._setup_cdp_listeners(tab)
        if url:
            await tab.get(url)
        return tab_id, tab

    async def switch_tab(self, tab_id: str) -> zd.Tab:
        """Make a tab the active one."""
        tab = self.tab_for_call(tab_id)
        self._page = tab
        await tab.bring_to_front()
        return tab

    async def close_tab(self, tab_id: str) -> None:
        """Close a specific tab."""
        tab = self.tab_for_call(tab_id)
        await tab.close()
        del self._tabs[tab_id]
        self._tab_owner.pop(tab_id, None)
        self._cdp_enabled_tabs.pop(id(tab), None)
        _dom.forget(tab)
        self._tabs_changed()

        # If the closed tab was this caller's active one, move to another it can
        # see; anyone else who had it active simply has none until they choose.
        was_mine = self._page is tab
        self._forget_active(tab)
        if was_mine:
            visible = [t for tid, t in self._tabs.items() if self._visible(tid)]
            self._page = visible[0] if visible else None

    def get_all_tabs(self) -> dict[str, str]:
        """Open tabs this caller can see, with their URLs."""
        self._sync_tabs()
        result = {}
        for tab_id, tab in self._tabs.items():
            if self._visible(tab_id):
                result[tab_id] = getattr(tab, "url", "unknown")
        return result

    def active_tab_id(self) -> str | None:
        page = CALL_TAB.get() or self._page
        return self.tab_id_of(page) if page is not None else None

    async def wait_for_document_ready(self, timeout: float = 10.0) -> tuple[bool, float]:
        """Block until the document reports `readyState == "complete"`.

        Network idle alone is not enough to say a page has loaded, and trusting
        it produced a real failure: `navigate` returned "network idle after 0.5s"
        — exactly `idle_time`, meaning the very first sample already looked
        quiet — and the next `get_text_content` threw
        `TypeError: Cannot read properties of null (reading 'innerText')`,
        because `document.body` did not exist yet.

        The race is that CDP network events for a navigation do not necessarily
        appear before the first poll. An idle detector that has not yet seen the
        page start cannot tell "finished" from "not begun": both are a request
        count that is not moving. Document readiness can tell them apart, so it
        is checked first and the idle wait then measures what happens after.
        """
        start = time.monotonic()
        while True:
            # BOUND THE EVALUATE, not just the loop. The deadline below is only
            # tested BETWEEN iterations, so a single evaluate that never returns
            # makes `timeout` unreachable and the caller's `settle` meaningless --
            # the call then runs until the tool budget cancels it, which is how a
            # 3-second settle produced a 25-second timeout. run_js() already
            # guards evaluate for exactly this reason (see its docstring); this
            # path called page.evaluate directly and bypassed it.
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                return False, time.monotonic() - start
            try:
                state = await asyncio.wait_for(
                    self.page.evaluate("document.readyState"), timeout=remaining
                )
            except (asyncio.TimeoutError, TimeoutError):
                # Out of time inside the evaluate, so the deadline is reached by
                # definition; reporting it here keeps the caller's contract.
                return False, time.monotonic() - start
            except Exception:
                # Mid-navigation the context can be torn down; that is not a
                # failure, it is the page being replaced. Try again.
                state = None
            if state == "complete":
                return True, time.monotonic() - start
            if time.monotonic() - start >= timeout:
                return False, time.monotonic() - start
            await self.page.wait(0.1)

    async def wait_for_network_idle(
        self, timeout: float = 10.0, idle_time: float = 0.5
    ) -> tuple[bool, float, int]:
        """Block until no new network request has been seen for ``idle_time`` seconds.

        Returns ``(idle, elapsed, requests)``. ``idle`` is False when ``timeout``
        was reached with the network still busy — that is a normal outcome for a
        page that polls or streams, not an error.

        This is the shared implementation behind both the ``wait_for_network``
        tool and the settle step in ``navigate``. It lives on the session because
        those two must not drift apart: a navigate that settled differently from
        an explicit wait would make the explicit wait look broken.
        """
        start = time.monotonic()
        last_count = len(self._network_logs)
        idle_start: float | None = None

        while True:
            now = time.monotonic()
            elapsed = now - start
            current_count = len(self._network_logs)

            if current_count == last_count:
                if idle_start is None:
                    idle_start = now
                elif now - idle_start >= idle_time:
                    return True, elapsed, current_count
            else:
                idle_start = None
                last_count = current_count

            if elapsed >= timeout:
                return False, elapsed, current_count

            await self.page.wait(0.1)

    def get_network_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get recent network logs."""
        return self._network_logs[-limit:]

    def get_console_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get recent console logs."""
        return self._console_logs[-limit:]

    def clear_logs(self) -> None:
        """Clear all logs."""
        self._network_logs = []
        self._console_logs = []
        self._pending_requests = {}
