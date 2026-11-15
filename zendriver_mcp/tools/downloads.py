# download tools - list the files the browser downloaded, wait for the next one
import asyncio
from typing import Annotated, Any

from pydantic import Field

from zendriver_mcp.artifacts import publish
from zendriver_mcp.errors import Code, ZendriverMCPError
from zendriver_mcp.registry import Effect
from zendriver_mcp.response import Artifact, Result
from zendriver_mcp.tools.base import ToolBase


class DownloadList(Result):
    downloads: list[dict[str, Any]] = Field(
        description="Downloads this session, oldest first: url, filename, state (inProgress, completed, canceled), received and total bytes, path and uri once completed."
    )
    directory: str | None = Field(default=None, description="Where completed files are saved.")


class DownloadResult(Result):
    url: str = Field(description="Where the file came from.")
    filename: str = Field(description="The name the page gave it.")
    state: str = Field(description="completed or canceled.")
    size: int = Field(description="Bytes received.")
    path: str | None = Field(default=None, description="The file on the server's disk.")
    files: list[Artifact] | None = Field(
        default=None, description="The file as a browser://artifacts/ resource."
    )


class DownloadTools(ToolBase):
    """tools for files the browser downloads"""

    GROUP = "downloads"

    def _register_tools(self) -> None:
        self._register(self.list_downloads, Effect.READ, "List downloads", page_scoped=False)
        self._register(
            self.wait_for_download, Effect.READ, "Wait for download", page_scoped=False, timeout=130
        )

    async def list_downloads(self) -> DownloadList:
        """List the files the browser has downloaded this session, with their progress.

        Each is saved under the page's suggested name and published as a
        resource when complete. Returns them oldest first.
        """
        d = self.session.downloads
        items = [x.as_dict() for x in d.items.values()]
        return DownloadList(
            summary=f"{len(items)} download(s)" + (f" in {d.dir}" if d.dir else ""),
            downloads=items,
            directory=str(d.dir) if d.dir else None,
        )

    async def wait_for_download(
        self,
        timeout: Annotated[
            float,
            Field(description="Maximum seconds to wait, up to 120. Example: 30"),
        ] = 30,
        url_contains: Annotated[
            str | None,
            Field(
                description="Only a download whose URL or filename contains this. Omit for any. Example: '.pdf'"
            ),
        ] = None,
    ) -> DownloadResult:
        """Wait for a download to finish and return its file.

        Returns the oldest finished download not yet returned, so one that
        finished before the call is not missed. Returns its URL, name, size
        and file.
        """
        d = self.session.downloads
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, min(timeout, 120.0))
        while True:
            item = d.next_unreported(url_contains)
            if item is not None:
                item.reported = True
                files = [publish(item.path)] if item.path else None
                return DownloadResult(
                    summary=f"Download {item.state}: {item.suggested} ({item.received} bytes)",
                    url=item.url,
                    filename=item.suggested,
                    state=item.state,
                    size=item.received,
                    path=item.path,
                    files=files,
                )
            left = deadline - loop.time()
            if left <= 0:
                pending = [x.suggested for x in d.items.values() if x.state == "inProgress"]
                raise ZendriverMCPError(
                    f"No download finished within {timeout:g}s"
                    + (f"; still running: {', '.join(pending)}" if pending else "."),
                    code=Code.TIMEOUT,
                    hint="Check that the click started one with list_downloads, or wait longer.",
                )
            await d.changed(min(left, 1.0))
