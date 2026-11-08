"""Screencast: CDP-driven frame capture to disk.

Uses ``Page.startScreencast`` which emits ``screencastFrame`` events with
base64-encoded JPEG/PNG data. We write each frame to a directory and ack the
frame so Chrome keeps sending new ones.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp.server.mcpserver import MCPServer as FastMCP
from pydantic import Field
from zendriver import cdp

from src.artifacts import publish, resolve_artifact_dir, resolve_artifact_path
from src.errors import Code, InvalidArgumentError, ZendriverMCPError
from src.registry import Effect
from src.response import FileResult, Result
from src.tools.base import ToolBase

FrameHandler = Callable[[cdp.page.ScreencastFrame], Awaitable[None]]

FFMPEG_HINT = "Install it with `apt install ffmpeg` or `brew install ffmpeg`."


class ScreencastStarted(Result):
    directory: str = Field(description="Directory the frames are written to.")
    format: str = Field(description="'jpeg' or 'png'.")
    quality: int = Field(description="JPEG quality 0-100.")
    every_nth_frame: int = Field(description="1 = every frame Chrome paints.")
    tab: str | None = Field(default=None, description="Id of the tab being recorded.")


class ScreencastStopped(Result):
    directory: str = Field(
        description="Directory holding the frames; pass it to export_screencast_mp4."
    )
    frame_count: int = Field(description="Frames written.")
    format: str = Field(description="File extension of the frames: 'jpg' or 'png'.")


class FfmpegAvailability(Result):
    available: bool = Field(description="Whether ffmpeg is on PATH.")
    path: str | None = Field(default=None, description="Where it is.")
    version: str | None = Field(default=None, description="First line of `ffmpeg -version`.")


class Mp4Exported(FileResult):
    frame_count: int = Field(description="Frames encoded.")
    fps: int = Field(description="Playback rate.")
    size_bytes: int = Field(description="Size of the mp4.")


class ScreencastTools(ToolBase):
    """Start/stop a screencast recording into a directory of frames."""

    def __init__(self, mcp: FastMCP) -> None:
        self._frame_dir: Path | None = None
        self._frame_count = 0
        self._frame_format = "jpeg"
        self._handler: FrameHandler | None = None
        # The recorded tab: stop_screencast must stop THAT one even if the
        # active tab changed in between.
        self._tab: Any = None
        super().__init__(mcp)

    def _reset_state(self) -> None:
        self._frame_dir = None
        self._frame_count = 0
        self._handler = None
        self._tab = None

    GROUP = "screencast"

    def _register_tools(self) -> None:
        self._register(self.start_screencast, Effect.WRITE, "Start screencast")
        self._register(self.stop_screencast, Effect.WRITE, "Stop screencast", page_scoped=False)
        # ffmpeg encoding of a long capture can take a while.
        self._register(
            self.export_screencast_mp4,
            Effect.WRITE,
            "Export screencast to mp4",
            page_scoped=False,
            timeout=300,
        )
        self._register(
            self.check_ffmpeg_available,
            Effect.READ,
            "Check ffmpeg installed",
            page_scoped=False,
            timeout=10,
        )

    async def start_screencast(
        self,
        output_dir: Annotated[
            str,
            Field(
                description="Directory for the frames, under $HOME, the temp dir or ZENDRIVER_MCP_ARTIFACT_ROOT; created if missing, existing frame files are overwritten. Omit for a new temp dir. Example: '/tmp/rec'"
            ),
        ] = "",
        fmt: Annotated[
            Literal["jpeg", "png"],
            Field(description="Frame format: jpeg is smaller, png lossless. Example: 'jpeg'"),
        ] = "jpeg",
        quality: Annotated[
            int, Field(ge=0, le=100, description="JPEG quality 0-100. Example: 80")
        ] = 80,
        max_width: Annotated[
            int, Field(ge=1, description="Frames wider than this are downscaled. Example: 1280")
        ] = 1280,
        every_nth_frame: Annotated[
            int, Field(ge=1, description="Keep one frame in N; 1 keeps every frame. Example: 2")
        ] = 1,
    ) -> ScreencastStarted:
        """Start recording the tab's painted frames as image files.

        Chrome sends a frame whenever the page repaints, so a static page
        produces few frames. Stop with stop_screencast, then turn the frames
        into a video with export_screencast_mp4. One recording at a time.
        """
        if self._frame_dir is not None:
            raise ZendriverMCPError(
                "A screencast is already running.",
                code=Code.CONFLICT,
                hint="Call stop_screencast first.",
            )

        target = resolve_artifact_dir(output_dir, default_prefix="zendriver-screencast")

        tab = self.session.page
        extension = "jpg" if fmt == "jpeg" else "png"

        async def on_frame(event: cdp.page.ScreencastFrame) -> None:
            # Late straggler after stop_screencast nulled the frame dir.
            # Drop silently rather than raising AssertionError from the
            # Listener task.
            frame_dir = self._frame_dir
            if frame_dir is None:
                return
            index = self._frame_count
            self._frame_count += 1
            path = frame_dir / f"frame-{index:06d}.{extension}"
            path.write_bytes(base64.b64decode(event.data))
            # Acking tells Chrome we consumed the frame - without it the stream stalls.
            await tab.send(cdp.page.screencast_frame_ack(session_id=event.session_id))

        self._frame_dir = target
        self._frame_count = 0
        self._frame_format = extension
        self._handler = on_frame
        self._tab = tab

        tab.add_handler(cdp.page.ScreencastFrame, on_frame)
        try:
            await tab.send(
                cdp.page.start_screencast(
                    format_=fmt,
                    quality=quality,
                    max_width=max_width,
                    every_nth_frame=every_nth_frame,
                )
            )
        except BaseException:
            handlers_list = tab.handlers.get(cdp.page.ScreencastFrame)
            if handlers_list and on_frame in handlers_list:
                handlers_list.remove(on_frame)
            self._reset_state()
            raise
        tid = self.session.tab_id_of(tab)
        return ScreencastStarted(
            summary=f"Screencast started on {tid} -> {target}",
            directory=str(target),
            format=fmt,
            quality=quality,
            every_nth_frame=every_nth_frame,
            tab=tid,
        )

    async def stop_screencast(self) -> ScreencastStopped:
        """Stop the running screencast and report where its frames are.

        Stops the recording on the tab it was started on. Pass the returned
        directory to export_screencast_mp4 to make a video.
        """
        if self._frame_dir is None or self._handler is None:
            raise ZendriverMCPError(
                "No screencast in progress.",
                code=Code.CONFLICT,
                hint="Start one with start_screencast.",
            )

        tab = self._tab or self.session.page
        await tab.send(cdp.page.stop_screencast())

        directory = self._frame_dir
        frames = self._frame_count
        handler = self._handler

        # Clear the target dir *before* draining and detaching. Any
        # straggler frame that arrives during the sleep will see
        # _frame_dir = None and drop silently instead of asserting.
        self._frame_dir = None
        self._frame_count = 0
        self._handler = None

        await asyncio.sleep(0.2)
        handlers_list = tab.handlers.get(cdp.page.ScreencastFrame)
        if handlers_list and handler in handlers_list:
            handlers_list.remove(handler)
        self._tab = None
        return ScreencastStopped(
            summary=f"Screencast stopped: {frames} frames in {directory}",
            directory=str(directory),
            frame_count=frames,
            format=self._frame_format,
        )

    async def check_ffmpeg_available(self) -> FfmpegAvailability:
        """Report whether the ``ffmpeg`` CLI is installed, and its version.

        export_screencast_mp4 needs it; check first to tell the user what to
        install instead of failing mid-task.
        """
        binary = shutil.which("ffmpeg")
        if not binary:
            return FfmpegAvailability(
                summary=f"ffmpeg is not installed. {FFMPEG_HINT}", available=False
            )
        proc = await asyncio.create_subprocess_exec(
            binary,
            "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        first_line = out.decode().splitlines()[0] if out else ""
        return FfmpegAvailability(
            summary=first_line or f"ffmpeg at {binary}",
            available=True,
            path=binary,
            version=first_line,
        )

    async def export_screencast_mp4(
        self,
        ctx: Context,
        frames_dir: Annotated[
            str,
            Field(
                description="Directory of frames, as stop_screencast returned it. Example: '/tmp/zendriver-screencast-abc123'"
            ),
        ],
        output_path: Annotated[
            str,
            Field(
                description="Where to write the mp4, under $HOME, the temp dir or ZENDRIVER_MCP_ARTIFACT_ROOT; an existing file is overwritten. Omit for a temp file. Example: '/tmp/rec.mp4'"
            ),
        ] = "",
        fps: Annotated[
            int,
            Field(
                ge=1, le=120, description="Playback frame rate (not the capture rate). Example: 30"
            ),
        ] = 30,
    ) -> Mp4Exported:
        """Encode a screencast's frames into an H.264 mp4 video.

        Reads the frame-NNNNNN.jpg/png files start_screencast wrote, reports
        progress per encoded frame, and publishes the video as a
        browser://artifacts/ resource. Requires ffmpeg.
        """
        binary = shutil.which("ffmpeg")
        if not binary:
            raise ZendriverMCPError("ffmpeg not found.", code=Code.NOT_INSTALLED, hint=FFMPEG_HINT)

        src = Path(frames_dir).expanduser().resolve()
        if not src.is_dir():
            raise InvalidArgumentError(
                f"frames_dir does not exist: {src}",
                hint="Pass the directory stop_screencast returned.",
            )

        jpegs = list(src.glob("frame-*.jpg"))
        pngs = list(src.glob("frame-*.png"))
        if jpegs and not pngs:
            pattern = str(src / "frame-%06d.jpg")
            count = len(jpegs)
        elif pngs:
            pattern = str(src / "frame-%06d.png")
            count = len(pngs)
        else:
            raise InvalidArgumentError(
                f"No frame-*.jpg or frame-*.png files found in {src}",
                hint="Record some frames with start_screencast / stop_screencast first.",
            )

        target = resolve_artifact_path(
            output_path, default_prefix="zendriver-screencast", default_ext="mp4"
        )

        args = [
            binary,
            "-y",
            # Machine-readable progress ("frame=N" lines) on stdout.
            "-progress",
            "pipe:1",
            "-nostats",
            "-framerate",
            str(fps),
            "-i",
            pattern,
            "-vf",
            # Ensure even dimensions for yuv420p.
            "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stderr_task = asyncio.create_task(proc.stderr.read()) if proc.stderr else None
        try:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if line.startswith("frame="):
                    done = line.partition("=")[2]
                    if done.isdigit():
                        await ctx.report_progress(int(done), count, "encoding")
            await proc.wait()
        except BaseException:
            # Cancelled or failed: never leave ffmpeg running.
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            if stderr_task is not None:
                stderr_task.cancel()
            raise
        stderr = await stderr_task if stderr_task is not None else b""
        if proc.returncode != 0:
            raise ZendriverMCPError(f"ffmpeg failed ({proc.returncode}): {stderr.decode()[-500:]}")

        size = target.stat().st_size
        return Mp4Exported(
            summary=f"Encoded {count} frames -> {target} ({size // 1024} KiB)",
            files=[publish(target, "video/mp4")],
            frame_count=count,
            fps=fps,
            size_bytes=size,
        )
