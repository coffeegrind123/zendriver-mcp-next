# utility tools - screenshot, js execution, waiting, security audit
import asyncio
import fnmatch
import io
import os
import re
import tempfile
import time
from datetime import datetime
from typing import Annotated, Any, Literal

from PIL import Image as PILImage
from pydantic import BaseModel, Field

from zendriver_mcp import capture
from zendriver_mcp.artifacts import publish, resolve_artifact_path
from zendriver_mcp.dom import World
from zendriver_mcp.errors import (
    Code,
    ElementNotFoundError,
    InvalidArgumentError,
    StaleIdError,
    ToolTimeoutError,
    ZendriverMCPError,
)
from zendriver_mcp.registry import Effect, Reach
from zendriver_mcp.response import Artifact, Result
from zendriver_mcp.tools.base import DEFAULT_TOOL_TIMEOUT, JS_TIMEOUT_SECONDS, ToolBase

# execute_javascript may wait this long for a script, leaving the tool's own budget room
# to report the timeout instead of being cut off by it.
MAX_JS_TIMEOUT = max(JS_TIMEOUT_SECONDS, DEFAULT_TOOL_TIMEOUT - 5.0)

# Cap the returned screenshot width; vision tokens scale with pixel area, so a
# 1920px capture costs ~3.5x a 1024px one for no readability gain. Downscaling
# applies only to the returned image — save_path files keep full resolution.
MAX_SCREENSHOT_WIDTH = 1024


class ScreenshotResult(Result):
    width: int = Field(description="Width in pixels of the returned image.")
    height: int = Field(description="Height in pixels of the returned image.")
    full_width: int = Field(description="Width in pixels of the full-resolution capture.")
    full_height: int = Field(description="Height in pixels of the full-resolution capture.")
    format: str = Field(description="Format of the returned image: always 'jpeg'.")
    downscaled: bool = Field(description="Whether the returned image was shrunk to save tokens.")
    files: list[Artifact] | None = Field(
        default=None, description="The full-resolution file, when save_path was given."
    )


class JsResult(Result):
    value: Any = Field(default=None, description="The expression's value, as JSON (null if none).")
    has_value: bool = Field(description="False when the expression produced undefined/null.")

    def render(self) -> str:
        import json

        if not self.has_value:
            return "(no return value)"
        return json.dumps(self.value, separators=(",", ":"), default=str, ensure_ascii=False)


class PdfResult(Result):
    size: int = Field(description="Bytes written.")
    files: list[Artifact] = Field(description="The PDF, as a file and a resource.")


class WaitResult(Result):
    seconds: float = Field(description="Seconds waited.")


class ElementWaitResult(Result):
    selector: str = Field(description="The selector or id waited for.")
    found: bool = Field(
        description="Whether the element is in the DOM now (false once a hidden or detached wait is met by its absence)."
    )
    elapsed: float = Field(description="Seconds until the state was reached.")
    state: str | None = Field(default=None, description="The state waited for.")


class TextWaitResult(Result):
    text: str = Field(description="The text waited for.")
    gone: bool = Field(description="Whether the wait was for the text to disappear.")
    elapsed: float = Field(description="Seconds until it appeared (or disappeared).")


class UrlWaitResult(Result):
    url: str = Field(description="The page's URL when it matched.")
    elapsed: float = Field(description="Seconds until it matched.")


def url_matches(url: str, pattern: str) -> bool:
    """A URL against a pattern: '/regex/', a glob with '*', or a substring."""
    if len(pattern) > 1 and pattern.startswith("/") and pattern.endswith("/"):
        return re.search(pattern[1:-1], url) is not None
    if "*" in pattern:
        return fnmatch.fnmatchcase(url, pattern)
    return pattern in url


# Visible text of the document, for wait_for_text.
_BODY_TEXT_JS = "document.body ? document.body.innerText : ''"

# How often waits re-check the page.
_POLL = 0.25


class AuditCheck(BaseModel):
    check: str = Field(description="What was checked.")
    status: Literal["PASS", "WARN", "FAIL", "INFO"] = Field(description="Outcome.")
    detail: str = Field(description="What was found.")


class AuditResult(Result):
    url: str = Field(description="The audited page.")
    checks: list[AuditCheck] = Field(description="Every check with its outcome.")
    fails: int = Field(description="Number of FAIL checks.")
    warnings: int = Field(description="Number of WARN checks.")
    passed: int = Field(description="Number of PASS checks.")

    def render(self) -> str:
        lines = [self.summary, f"URL: {self.url}"]
        lines += [f"[{c.status}] {c.check}: {c.detail}" for c in self.checks]
        return "\n".join(lines)


# Visibility as the user would judge it, evaluated in the isolated world on the
# element itself (no selector interpolation).
_VISIBLE_FN = """function(el) {
  const s = getComputedStyle(el), r = el.getBoundingClientRect();
  return s.display !== "none" && s.visibility !== "hidden" && s.opacity !== "0"
    && r.width > 0 && r.height > 0;
}"""

_WHY_HIDDEN_FN = """function(el) {
  const s = getComputedStyle(el), r = el.getBoundingClientRect();
  if (s.display === "none") return "display:none";
  if (s.visibility === "hidden") return "visibility:hidden";
  if (s.opacity === "0") return "opacity:0";
  if (!(r.width > 0 && r.height > 0)) return "zero size";
  return null;
}"""

# Elements whose id contains the selector's word, for a "did you mean" after a
# timeout. Runs in the isolated world; the needle travels as a CDP argument.
_SIMILAR_IDS_JS = """(needle) => {
  const out = [];
  if (!needle) return out;
  for (const el of document.querySelectorAll("[id]")) {
    if (el.id.toLowerCase().includes(needle)) out.push("#" + el.id);
    if (out.length >= 3) break;
  }
  return out;
}"""


class UtilityTools(ToolBase):
    """utility tools for screenshots, js, waiting, and security"""

    GROUP = "utils"

    def _register_tools(self) -> None:
        """register utility tools"""
        self._register(self.screenshot, Effect.READ, "Screenshot")
        self._register(self.save_pdf, Effect.WRITE, "Save page as PDF")
        self._register(self.execute_javascript, Effect.WRITE, "Run JavaScript", reach=Reach.WEB)
        self._register(self.wait, Effect.READ, "Wait", page_scoped=False)
        self._register(self.wait_for_element, Effect.READ, "Wait for element")
        self._register(self.wait_for_text, Effect.READ, "Wait for text")
        self._register(self.wait_for_url, Effect.READ, "Wait for URL")
        self._register(self.run_security_audit, Effect.READ, "Page security audit")

    async def screenshot(
        self,
        save_path: Annotated[
            str | None,
            Field(
                description="Path to also write the image to on the server's filesystem, at FULL resolution. The extension picks the format: .png, .gif, and .bmp are saved losslessly, anything else as JPEG. Omit to return the image without writing a file. Example: '/tmp/page.png'"
            ),
        ] = None,
        full_resolution: Annotated[
            bool,
            Field(
                description="Return the image without downscaling. By default the returned image is capped at 1024px wide to save vision tokens; set true when you need to read fine detail. Example: false"
            ),
        ] = False,
        full_page: Annotated[
            bool,
            Field(
                description="The whole page: scrolled through and stitched (fixed headers repeat), at most 20000 px. Example: true"
            ),
        ] = False,
        selector: Annotated[
            str | None,
            Field(description="Only this element (selector or tree id). Example: '#chart'"),
        ] = None,
    ) -> ScreenshotResult:
        """Take a screenshot of the visible viewport as an image you can look at directly.

        The fastest way to understand a page's actual layout when selectors and
        text are not enough. Set window_size and device_scale_factor on
        start_browser first — the default viewport is roughly 800x600, which is
        why screenshots come out small. The returned image is JPEG quality 60,
        downscaled to 1024px wide unless full_resolution is set; save_path files
        keep full resolution and are also published as a resource. Returns the
        image plus its returned and full dimensions and any saved file.
        """
        if full_page and selector:
            raise InvalidArgumentError("Pass full_page or selector, not both.")
        note_extra = ""
        if selector:
            png, cut = await capture.element_png(self.dom, await self.resolve(selector))
            note_extra = f" of {selector}" + (", cut to the viewport" if cut else "")
        elif full_page:
            png, cut = await capture.full_page_png(self.dom)
            note_extra = " of the full page" + (
                f", cut at {capture.MAX_FULL_PAGE_HEIGHT}px" if cut else ""
            )
        else:
            png = await capture.viewport_png(self.dom)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(png)

        try:
            # compress to JPEG (under 1MB limit); downscale the RETURNED image to
            # cap vision-token cost, while save_path keeps the full resolution.
            with PILImage.open(tmp_path) as img:
                full_res = img.convert("RGB")
                returned = full_res
                if not full_resolution and full_res.width > MAX_SCREENSHOT_WIDTH:
                    new_height = round(full_res.height * MAX_SCREENSHOT_WIDTH / full_res.width)
                    returned = full_res.resize(
                        (MAX_SCREENSHOT_WIDTH, new_height), PILImage.Resampling.LANCZOS
                    )

                buffer = io.BytesIO()
                returned.save(buffer, format="JPEG", quality=60, optimize=True)
                jpeg_data = buffer.getvalue()

                files: list[Artifact] | None = None
                # The path is sandboxed to $HOME / tempdir / $ZENDRIVER_MCP_ARTIFACT_ROOT
                # so an agent-supplied path cannot overwrite arbitrary files.
                if save_path:
                    ext = os.path.splitext(save_path)[1].lower()
                    lossless = ext in {".png", ".gif", ".bmp"}
                    resolved = resolve_artifact_path(
                        save_path,
                        default_prefix="screenshot",
                        default_ext="png" if lossless else "jpg",
                    )
                    if lossless:
                        with PILImage.open(tmp_path) as orig:
                            orig.save(str(resolved))
                    elif returned is full_res:
                        resolved.write_bytes(jpeg_data)
                    else:
                        full_buffer = io.BytesIO()
                        full_res.save(full_buffer, format="JPEG", quality=60, optimize=True)
                        resolved.write_bytes(full_buffer.getvalue())
                    files = [publish(resolved)]

                downscaled = returned is not full_res
                note = f", downscaled from {full_res.width}x{full_res.height}" if downscaled else ""
                saved = f", saved to {files[0].path}" if files else ""
                result = ScreenshotResult(
                    summary=f"Screenshot{note_extra} {returned.width}x{returned.height}{note}{saved}",
                    width=returned.width,
                    height=returned.height,
                    full_width=full_res.width,
                    full_height=full_res.height,
                    format="jpeg",
                    downscaled=downscaled,
                    files=files,
                )
                result.attach_image(jpeg_data, "image/jpeg")
                return result
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    async def save_pdf(
        self,
        save_path: Annotated[
            str | None,
            Field(
                description="Where to write the PDF, under $HOME or the temp dir. Omit for a temp file. Example: '/tmp/invoice.pdf'"
            ),
        ] = None,
        landscape: Annotated[
            bool, Field(description="Landscape pages instead of portrait. Example: false")
        ] = False,
        background: Annotated[
            bool,
            Field(description="Print background colours and images. Example: true"),
        ] = True,
    ) -> PdfResult:
        """Save the page as a PDF, as Chrome's Print to PDF would.

        Uses the page's print stylesheet. Returns the file and a resource.
        """
        data = await capture.pdf(self.dom, landscape=landscape, background=background)
        path = resolve_artifact_path(save_path or "", default_prefix="page", default_ext="pdf")
        path.write_bytes(data)
        files = [publish(path, "application/pdf")]
        return PdfResult(
            summary=f"Saved PDF ({len(data)} bytes) to {path}", size=len(data), files=files
        )

    async def execute_javascript(
        self,
        script: Annotated[
            str,
            Field(
                description="JavaScript to run in the page: an expression ('document.title'), or statements that end in a return ('const n = document.links.length; return n'). Promises are awaited. Example: 'return [...document.querySelectorAll(\"h2\")].map(h => h.textContent)'"
            ),
        ],
        timeout: Annotated[
            float,
            Field(
                description=f"Seconds to wait for the result, promises included, between 1 and {MAX_JS_TIMEOUT:.0f}. Raise it for scripts that poll or retry. Example: 45"
            ),
        ] = JS_TIMEOUT_SECONDS,
    ) -> JsResult:
        """Execute JavaScript in the page's own context and return its value as JSON.

        The escape hatch for anything the dedicated tools do not cover: reading
        page globals, calling page functions, extracting structured data in one
        pass. Takes an expression, or a statement block that uses `return` (run
        as the body of an async function, so `await` works too). It runs in the
        page's main world, so it sees the page's variables and the page could
        in principle observe it; prefer the dedicated tools where they apply.
        Nothing is rolled back: whatever the script changed before it threw
        stays, and a script that times out may still be running in the page.
        """
        if not 1 <= timeout <= MAX_JS_TIMEOUT:
            raise InvalidArgumentError(
                f"timeout must be between 1 and {MAX_JS_TIMEOUT:.0f} seconds, got {timeout}."
            )
        try:
            try:
                value = await self.run_js(script, timeout=timeout, world=World.MAIN)
            except ToolTimeoutError:
                raise
            except ZendriverMCPError as e:
                # "Illegal return statement" (or await outside async): the caller
                # wrote a statement block. Run it as an async function body, which
                # is what they meant, rather than making them wrap it.
                text = str(e)
                if "Illegal return" not in text and "await is only valid" not in text:
                    raise
                body = "(async () => {\n" + script + "\n})()"
                value = await self.run_js(body, timeout=timeout, world=World.MAIN)
        except ToolTimeoutError as e:
            # Not cancellable: CDP has no way to stop a running promise, so the
            # script carries on in the page after we stop waiting. For a script
            # that clicks or submits, running it again could repeat the action.
            raise ZendriverMCPError(
                f"The script did not finish within {timeout:g}s.",
                code=Code.TIMEOUT,
                hint="It may still be running in the page. If it has side effects (clicks, "
                "submits, purchases), check the page state before running it again. Raise "
                f"`timeout` (up to {MAX_JS_TIMEOUT:.0f}) for scripts that legitimately take longer.",
            ) from e
        if value is None:
            return JsResult(summary="(no return value)", value=None, has_value=False)
        return JsResult(summary="JavaScript returned a value", value=value, has_value=True)

    async def wait(
        self,
        seconds: Annotated[
            float, Field(description="How long to pause, in seconds. Example: 2.0")
        ] = 1.0,
    ) -> WaitResult:
        """Pause for a fixed number of seconds before the next tool runs.

        A blunt fallback for animations and rate limits. Prefer
        wait_for_element, wait_for_network, or wait_for_request wherever the
        thing being waited for can be named — they are faster and do not fail
        when the page is slower than the guess. Returns a confirmation naming the
        duration waited.
        """
        if seconds < 0:
            raise InvalidArgumentError("seconds must be >= 0")
        await asyncio.sleep(seconds)
        return WaitResult(summary=f"Waited {seconds}s", seconds=seconds)

    async def wait_for_element(
        self,
        selector: Annotated[
            str,
            Field(
                description="CSS selector, or a numeric id from get_interaction_tree, to wait for. Example: '#results .item'"
            ),
        ],
        timeout: Annotated[
            float,
            Field(
                description="Maximum seconds to wait before giving up. The default suits single-page apps. Example: 30.0"
            ),
        ] = 30.0,
        visible: Annotated[
            bool,
            Field(
                description="Require the element to be visible, not merely present in the DOM. Set false to accept a hidden element. Example: true"
            ),
        ] = True,
        state: Annotated[
            Literal["visible", "attached", "hidden", "detached"] | None,
            Field(
                description="visible, attached (in the DOM), hidden (not shown or gone) or detached (gone, e.g. a spinner). Overrides visible. Example: 'detached'"
            ),
        ] = None,
    ) -> ElementWaitResult:
        """Wait until an element appears on the page, polling until it does.

        The right way to synchronise with a single-page app after a click or
        navigation — more reliable than wait with a guessed duration. state
        hidden or detached waits for it to go away instead. Returns how long it
        took, or an error naming why it never matched.
        """
        want = state or ("visible" if visible else "attached")
        if want in ("hidden", "detached"):
            return await self._wait_gone(selector, timeout, want)
        visible = want == "visible"
        start = time.monotonic()
        last: Exception | None = None
        handle = None
        while True:
            try:
                handle = await self.dom.resolve(selector)
                if not visible or await self.element_call(handle, _VISIBLE_FN):
                    elapsed = round(time.monotonic() - start, 2)
                    return ElementWaitResult(
                        summary=f"Element found: {selector} after {elapsed}s",
                        selector=selector,
                        found=True,
                        elapsed=elapsed,
                        state=want,
                    )
            except StaleIdError:
                raise
            except ZendriverMCPError as exc:
                if exc.code is Code.INVALID_ARGUMENT or "Invalid CSS selector" in str(exc):
                    raise
                last = exc
            if time.monotonic() - start >= timeout:
                break
            await asyncio.sleep(0.25)

        if handle is not None and visible:
            why = await self.element_call(handle, _WHY_HIDDEN_FN)
            raise ZendriverMCPError(
                f"Timeout after {timeout}s: {selector} exists but is not visible ({why}).",
                code=Code.ELEMENT_HIDDEN,
                hint="Pass visible=false to accept it hidden, or do whatever reveals it first.",
            )

        needle = "".join(ch for ch in selector.lower() if ch.isalnum() or ch in "-_")
        similar = await self.dom.evaluate(f"({_SIMILAR_IDS_JS})({needle!r})") if needle else []
        hint = (
            f"Similar ids on the page: {', '.join(similar)}." if similar else None
        ) or "Check the selector with find_element, or raise timeout."
        raise ElementNotFoundError(f"{selector} (waited {timeout}s)", hint=hint) from last

    async def _wait_gone(self, selector: str, timeout: float, want: str) -> ElementWaitResult:
        start = time.monotonic()
        while True:
            present = True
            try:
                handle = await self.dom.resolve(selector)
                if want == "hidden" and not await self.element_call(handle, _VISIBLE_FN):
                    elapsed = round(time.monotonic() - start, 2)
                    return ElementWaitResult(
                        summary=f"{selector} is hidden after {elapsed}s",
                        selector=selector,
                        found=True,
                        elapsed=elapsed,
                        state=want,
                    )
            except ZendriverMCPError as exc:
                if exc.code is Code.INVALID_ARGUMENT or "Invalid CSS selector" in str(exc):
                    raise
                if exc.code not in (Code.ELEMENT_NOT_FOUND, Code.STALE_ID):
                    raise
                present = False
            if not present:
                elapsed = round(time.monotonic() - start, 2)
                return ElementWaitResult(
                    summary=f"{selector} is gone after {elapsed}s",
                    selector=selector,
                    found=False,
                    elapsed=elapsed,
                    state=want,
                )
            if time.monotonic() - start >= timeout:
                raise ZendriverMCPError(
                    f"Timeout after {timeout}s: {selector} is still "
                    + ("shown." if want == "hidden" else "in the page."),
                    code=Code.TIMEOUT,
                    hint="Raise timeout, or check what keeps it there with get_interaction_tree.",
                )
            await asyncio.sleep(_POLL)

    async def wait_for_text(
        self,
        text: Annotated[
            str,
            Field(
                description="Text to wait for, matched as a case-sensitive substring of the visible text. Example: 'Order confirmed'"
            ),
        ],
        gone: Annotated[
            bool,
            Field(
                description="Wait for the text to disappear instead, e.g. 'Loading...'. Example: false"
            ),
        ] = False,
        timeout: Annotated[
            float,
            Field(description="Maximum seconds to wait. Example: 30.0"),
        ] = 30.0,
    ) -> TextWaitResult:
        """Wait until some text shows on the page, or until it disappears.

        For results with no stable selector. Reads the visible text of the page
        and its iframes. Returns how long it took; a timeout is an error.
        """
        start = time.monotonic()
        while True:
            seen = text in await self._visible_text()
            if seen != gone:
                elapsed = round(time.monotonic() - start, 2)
                return TextWaitResult(
                    summary=f"{text!r} {'gone' if gone else 'shown'} after {elapsed}s",
                    text=text,
                    gone=gone,
                    elapsed=elapsed,
                )
            if time.monotonic() - start >= timeout:
                raise ZendriverMCPError(
                    f"Timeout after {timeout}s: {text!r} is "
                    + ("still shown." if gone else "not on the page."),
                    code=Code.TIMEOUT,
                    hint="Check the exact wording with get_text_content (the match is case-sensitive).",
                )
            await asyncio.sleep(_POLL)

    async def _visible_text(self) -> str:
        parts = [str(await self.run_js(_BODY_TEXT_JS) or "")]
        for frame in (await self.dom.frames())[1:]:
            try:
                parts.append(str(await self.dom.evaluate_in(frame, _BODY_TEXT_JS) or ""))
            except Exception:  # a frame navigating mid-read
                continue
        return "\n".join(parts)

    async def wait_for_url(
        self,
        pattern: Annotated[
            str,
            Field(
                description="A substring, a glob with * over the whole URL, or a /regex/. Example: '/checkout/done'"
            ),
        ],
        timeout: Annotated[
            float,
            Field(description="Maximum seconds to wait. Example: 30.0"),
        ] = 30.0,
    ) -> UrlWaitResult:
        """Wait until the tab's URL matches a pattern, after a redirect or a client-side route.

        history.pushState counts. Returns the URL that matched.
        """
        if len(pattern) > 1 and pattern.startswith("/") and pattern.endswith("/"):
            try:
                re.compile(pattern[1:-1])
            except re.error as exc:
                raise InvalidArgumentError(f"Bad regular expression {pattern}: {exc}") from exc
        start = time.monotonic()
        url = ""
        while True:
            url = (await self.dom.main_frame()).url
            if url_matches(url, pattern):
                elapsed = round(time.monotonic() - start, 2)
                return UrlWaitResult(
                    summary=f"URL {url} after {elapsed}s", url=url, elapsed=elapsed
                )
            if time.monotonic() - start >= timeout:
                raise ZendriverMCPError(
                    f"Timeout after {timeout}s: the URL is still {url}.",
                    code=Code.TIMEOUT,
                    hint="A bare pattern is a substring; use * for a glob or /.../ for a regex.",
                )
            await asyncio.sleep(_POLL)

    async def run_security_audit(self) -> AuditResult:
        """Audit the loaded page's client-side security posture.

        Checks HTTPS, CSRF protection, password field handling, mixed content,
        inline scripts, Subresource Integrity, external resources, exposed
        secrets, and risky JavaScript patterns. Inspects only what the current
        page exposes to the browser — it is not a scan of the server. Returns
        one entry per check with PASS/WARN/FAIL/INFO and the counts.
        """
        page = self.session.page
        url = getattr(page, "url", None) or "unknown"
        checks: list[AuditCheck] = []

        def add(check: str, status: str, detail: str) -> None:
            checks.append(AuditCheck(check=check, status=status, detail=detail))  # type: ignore[arg-type]

        is_https = url.startswith("https://")
        add("HTTPS", "PASS" if is_https else "FAIL", "yes" if is_https else "no - INSECURE")

        # All of these only read the DOM, so they run in the isolated world.
        forms = await self.run_js("""
            (function() {
                const forms = document.forms;
                let hasCSRF = false, hasInsecurePassword = false, passwordForms = 0;
                for (let form of forms) {
                    if (form.querySelector('input[name*="csrf"], input[name*="token"], input[name="_token"]')) hasCSRF = true;
                    if (form.querySelector('input[type="password"]')) {
                        passwordForms++;
                        if ((form.getAttribute('method') || 'get').toLowerCase() === 'get') hasInsecurePassword = true;
                    }
                }
                return { count: forms.length, hasCSRF, hasInsecurePassword, passwordForms };
            })()
        """)
        add(
            "CSRF protection",
            "WARN" if forms["count"] > 0 and not forms["hasCSRF"] else "PASS",
            "detected" if forms["hasCSRF"] else "not detected",
        )
        add(
            "Password security",
            "FAIL" if forms["hasInsecurePassword"] else "PASS",
            "INSECURE - password form submits with GET" if forms["hasInsecurePassword"] else "ok",
        )
        add("Forms", "INFO", f"{forms['count']} total, {forms['passwordForms']} with passwords")

        mixed = await self.run_js("""
            (function() {
                if (location.protocol !== 'https:') return { check: false };
                const s = Array.from(document.scripts).filter(x => x.src?.startsWith('http://')).length;
                const c = Array.from(document.querySelectorAll('link[rel~="stylesheet"]')).filter(x => x.href?.startsWith('http://')).length;
                const i = Array.from(document.images).filter(x => x.src?.startsWith('http://')).length;
                return { check: true, scripts: s, styles: c, images: i, total: s + c + i };
            })()
        """)
        if mixed["check"]:
            add(
                "Mixed content",
                "FAIL" if mixed["total"] else "PASS",
                f"{mixed['scripts']} scripts, {mixed['styles']} styles, {mixed['images']} images over HTTP"
                if mixed["total"]
                else "none",
            )

        inline = await self.run_js('document.querySelectorAll("script:not([src])").length')
        add("Inline scripts", "INFO" if inline > 5 else "PASS", str(inline))

        no_integrity = await self.run_js(
            "Array.from(document.scripts).filter(s => s.src && !s.integrity).length"
        )
        add("Scripts without SRI", "WARN" if no_integrity else "PASS", str(no_integrity))

        external = await self.run_js("""
            (function() {
                const host = location.hostname;
                const h = u => { try { return new URL(u).hostname; } catch { return null; } };
                return {
                    scripts: Array.from(document.scripts).filter(s => s.src && h(s.src) !== host).length,
                    iframes: Array.from(document.querySelectorAll('iframe')).filter(f => f.src && h(f.src) !== host).length,
                };
            })()
        """)
        add(
            "External resources",
            "INFO" if external["scripts"] else "PASS",
            f"{external['scripts']} scripts, {external['iframes']} iframes from other hosts",
        )

        dangerous = await self.run_js("""
            (function() {
                const src = Array.from(document.scripts).map(s => s.textContent).join('\\n');
                return {
                    eval: (src.match(/eval\\s*\\(/g) || []).length,
                    innerHTML: (src.match(/\\.innerHTML\\s*=/g) || []).length,
                    documentWrite: (src.match(/document\\.write\\s*\\(/g) || []).length,
                };
            })()
        """)
        total = dangerous["eval"] + dangerous["innerHTML"] + dangerous["documentWrite"]
        add(
            "Dangerous JS patterns",
            "WARN" if total else "PASS",
            f"eval({dangerous['eval']}), innerHTML({dangerous['innerHTML']}), "
            f"document.write({dangerous['documentWrite']})"
            if total
            else "none detected",
        )

        sensitive = await self.run_js("""
            (function() {
                const html = document.documentElement.outerHTML;
                return {
                    awsKeys: (html.match(/AKIA[0-9A-Z]{16}/g) || []).length,
                    jwtTokens: (html.match(/eyJ[a-zA-Z0-9_-]*\\.eyJ[a-zA-Z0-9_-]*\\.[a-zA-Z0-9_-]*/g) || []).length,
                    privateKeys: (html.match(/-----BEGIN (RSA |EC |DSA |)PRIVATE KEY-----/g) || []).length,
                };
            })()
        """)
        stotal = sensitive["awsKeys"] + sensitive["jwtTokens"] + sensitive["privateKeys"]
        add(
            "Exposed secrets",
            "FAIL" if stotal else "PASS",
            f"AWS keys({sensitive['awsKeys']}), JWT tokens({sensitive['jwtTokens']}), "
            f"private keys({sensitive['privateKeys']})"
            if stotal
            else "none detected",
        )

        fails = sum(c.status == "FAIL" for c in checks)
        warns = sum(c.status == "WARN" for c in checks)
        passes = sum(c.status == "PASS" for c in checks)
        if fails:
            verdict = f"{fails} CRITICAL, {warns} WARNINGS, {passes} PASSED"
        elif warns:
            verdict = f"{warns} WARNINGS, {passes} PASSED"
        else:
            verdict = f"ALL {passes} CHECKS PASSED"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return AuditResult(
            summary=f"Security audit ({stamp}): {verdict}",
            url=url,
            checks=checks,
            fails=fails,
            warnings=warns,
            passed=passes,
        )
