"""Lighthouse wrapper: run the Lighthouse CLI against the active browser.

Lighthouse is a Node package; users must install it separately
(``npm i -g lighthouse``). The tool connects via the browser's existing
remote-debugging port, so we don't spin up a second browser.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from typing import Annotated, Literal

from mcp.server.mcpserver import Context
from pydantic import Field

from zendriver_mcp.artifacts import publish, resolve_artifact_path
from zendriver_mcp.errors import LighthouseNotInstalledError, ZendriverMCPError
from zendriver_mcp.registry import Effect, Reach
from zendriver_mcp.response import FileResult, Result
from zendriver_mcp.tools.base import ToolBase

_DEFAULT_CATEGORIES = [
    "performance",
    "accessibility",
    "best-practices",
    "seo",
]

Category = Literal["performance", "accessibility", "best-practices", "seo", "pwa"]

# Lighthouse logs its phases to stderr as "LH:status <message>" at its default
# log level. These become progress messages; the count is only a rough guide.
_STATUS = re.compile(r"LH:status\s+(.*)")
_EXPECTED_STEPS = 25


class LighthouseAvailability(Result):
    available: bool = Field(description="Whether the lighthouse CLI is on PATH.")
    path: str | None = Field(default=None, description="Where it is.")
    version: str | None = Field(default=None, description="Its --version output.")


class LighthouseReport(FileResult):
    url: str = Field(description="The audited URL.")
    form_factor: str = Field(description="'mobile' or 'desktop'.")
    scores: dict[str, int] = Field(description="Category id -> score 0-100.")
    report_path: str = Field(description="The JSON report on disk.")


class LighthouseTools(ToolBase):
    """Run Lighthouse audits against the current tab."""

    GROUP = "lighthouse"

    def _register_tools(self) -> None:
        # Full Lighthouse audits routinely take 60-120s; allow up to 5 min.
        # Not page-scoped: Lighthouse opens its own tab in this browser.
        self._register(
            self.run_lighthouse,
            Effect.WRITE,
            "Run Lighthouse audit",
            reach=Reach.WEB,
            page_scoped=False,
            timeout=300,
        )
        self._register(
            self.check_lighthouse_available,
            Effect.READ,
            "Check Lighthouse installed",
            page_scoped=False,
            timeout=10,
        )

    async def check_lighthouse_available(self) -> LighthouseAvailability:
        """Report whether the ``lighthouse`` CLI is installed, and its version.

        Use before run_lighthouse to tell the user to install it
        (`npm i -g lighthouse`, needs Node) instead of failing mid-task.
        """
        binary = shutil.which("lighthouse")
        if not binary:
            return LighthouseAvailability(
                summary="Lighthouse is not installed. Install with `npm i -g lighthouse` (requires Node).",
                available=False,
            )
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        version = out.decode().strip()
        return LighthouseAvailability(
            summary=f"Lighthouse {version} at {binary}",
            available=True,
            path=binary,
            version=version,
        )

    async def run_lighthouse(
        self,
        ctx: Context,
        url: Annotated[
            str,
            Field(description="Absolute URL to audit. Example: 'https://example.com'"),
        ],
        categories: Annotated[
            list[Category] | None,
            Field(
                description="Categories to audit. Omit for performance, accessibility, best-practices and seo. Example: ['performance']"
            ),
        ] = None,
        form_factor: Annotated[
            Literal["mobile", "desktop"],
            Field(description="Device class Lighthouse emulates. Example: 'desktop'"),
        ] = "mobile",
        output_path: Annotated[
            str,
            Field(
                description="Where to save the JSON report, under $HOME, the temp dir or ZENDRIVER_MCP_ARTIFACT_ROOT; an existing file is overwritten. Omit for a temp file. Example: '/tmp/lh.json'"
            ),
        ] = "",
    ) -> LighthouseReport:
        """Audit a URL with Lighthouse, in this browser, and return the scores.

        Lighthouse attaches over the browser's debugging port and loads the URL
        in a tab of its own, so the active tab is left alone. Takes 30-120s;
        progress messages name each Lighthouse phase. The full JSON report is
        saved and published as a browser://artifacts/ resource. Requires the
        lighthouse CLI (check_lighthouse_available).
        """
        binary = shutil.which("lighthouse")
        if not binary:
            raise LighthouseNotInstalledError

        port = self._extract_debug_port()
        if port is None:
            raise ZendriverMCPError("Could not determine the browser's remote debugging port.")

        report_target = resolve_artifact_path(
            output_path, default_prefix="lighthouse", default_ext="json"
        )

        args = [
            binary,
            url,
            f"--port={port}",
            "--output=json",
            f"--output-path={report_target}",
            f"--form-factor={form_factor}",
            "--only-categories=" + ",".join(categories or _DEFAULT_CATEGORIES),
            # Default log level (not --quiet): its "LH:status" lines are the
            # progress messages. We connect to the running browser via --port,
            # so no --chrome-flags (Lighthouse would otherwise spawn a second
            # Chrome despite the port attachment).
        ]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        started = time.monotonic()
        tail: list[str] = []
        try:
            assert proc.stderr is not None
            step = 0
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                tail = (tail + [line])[-20:]
                m = _STATUS.search(line)
                if m:
                    step += 1
                    await ctx.report_progress(
                        min(step, _EXPECTED_STEPS),
                        _EXPECTED_STEPS,
                        f"{m.group(1)[:120]} ({time.monotonic() - started:.0f}s)",
                    )
            await proc.wait()
        except BaseException:
            # Cancelled (the client gave up, or the budget ran out) or failed:
            # never leave a Lighthouse process driving the browser.
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        if proc.returncode != 0:
            detail = " | ".join(t for t in tail if t.strip())[-500:]
            raise ZendriverMCPError(f"Lighthouse failed ({proc.returncode}): {detail}")

        report = json.loads(report_target.read_text())
        scores = {
            name: round((entry.get("score") or 0) * 100)
            for name, entry in report.get("categories", {}).items()
        }
        summary = ", ".join(f"{k} {v}" for k, v in scores.items())
        return LighthouseReport(
            summary=f"Lighthouse {form_factor} audit of {url}: {summary}",
            files=[publish(report_target, "application/json")],
            url=url,
            form_factor=form_factor,
            scores=scores,
            report_path=str(report_target),
        )

    def _extract_debug_port(self) -> int | None:
        """Find the remote debugging port the browser is listening on.

        Zendriver keeps the port on the websocket URL used by the primary
        connection; we fall back to parsing the browser's stored config when
        that isn't exposed.
        """
        browser = self.session.browser
        ws_url = getattr(browser.connection, "websocket_url", None) or getattr(
            browser, "websocket_url", None
        )
        if isinstance(ws_url, str) and ":" in ws_url:
            try:
                return int(ws_url.rsplit(":", 1)[-1].split("/", 1)[0])
            except ValueError:
                return None
        config_port = getattr(getattr(browser, "config", None), "port", None)
        if isinstance(config_port, int):
            return config_port
        return None
