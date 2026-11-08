"""The server instructions: short enough to survive, and only real tool names."""

from __future__ import annotations

import re

from src.instructions import INSTRUCTIONS
from src.tools import CATALOG
from src.tools.catalog import META

# Claude Code truncates instructions at 4,096 characters; keep headroom.
LIMIT = 3500


def test_instructions_fit() -> None:
    assert len(INSTRUCTIONS) <= LIMIT, len(INSTRUCTIONS)


def test_every_tool_named_exists() -> None:
    known = set(CATALOG) | set(META)
    named = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\b", INSTRUCTIONS))
    # Words with underscores that are not tool names: codes and arguments.
    not_tools = {"stale_id", "element_obscured", "target_blank"}
    missing = sorted(n for n in named - not_tools if n not in known)
    assert not missing, f"instructions name tools that do not exist: {missing}"


def test_the_server_sends_them() -> None:
    from src.tools import mcp

    assert mcp.instructions == INSTRUCTIONS
