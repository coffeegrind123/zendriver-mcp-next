"""Tool descriptions reach clients without their source indentation.

Python 3.13 strips docstring indentation at compile time; 3.12 does not, so on
3.12 every description line after the first carried eight spaces, about 1 KB
of the catalogue a client loads (measured on CI, 2026-11-15). A docstring set
at runtime reproduces the 3.12 shape on any version.
"""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer as FastMCP

from zendriver_mcp.registry import Effect
from zendriver_mcp.response import Result
from zendriver_mcp.tools.base import ToolBase


class _Tools(ToolBase):
    GROUP = "test"

    def _register_tools(self) -> None:
        async def probe() -> Result:
            return Result(summary="ok")

        probe.__doc__ = (
            "First line.\n\n        Second paragraph,\n        indented in source.\n        "
        )
        self._register(probe, Effect.READ, "Probe", page_scoped=False)


def test_description_is_dedented() -> None:
    server = FastMCP("t")
    _Tools(server)
    (tool,) = asyncio.run(server.list_tools())
    assert tool.description == "First line.\n\nSecond paragraph,\nindented in source."
