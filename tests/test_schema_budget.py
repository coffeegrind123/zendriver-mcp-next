"""Guardrail: what the tool list costs a client's context must not creep up.

Tool descriptions and input schemas load into every session's context (output
schemas usually do not, so they have their own, looser budget). The default
catalogue surface is the one that matters most: it is what every client pays.

If a test fails, trim descriptions or move prose into the instructions or
search_tools; do NOT just raise the budget.

Measured 2026-11-08 (SDK 2.3 rewrite): catalogue surface 19 tools, 23.8 KB
input side; full surface 103 tools, 99 KB input side, 76 KB of output schemas.

Measured 2026-11-15, after descriptions were trimmed: catalogue 19 tools,
25.2 KB (budget unchanged); full surface 117 tools, 110.3 KB input side,
91.8 KB of output schemas. The full budgets were raised for 14 new tools
(dialogs, downloads, response bodies, text and URL waits, open_tabs, save_pdf,
session export/import, block_resources, allow_next_submit), each about 0.8 KB
input side; the full surface is opt-in (ZENDRIVER_MCP_SURFACE=full).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CATALOG_BUDGET_BYTES = 26_000
CATALOG_MAX_TOOLS = 20
FULL_BUDGET_BYTES = 113_000
FULL_OUTPUT_BUDGET_BYTES = 94_000

_MEASURE = (
    "import json,asyncio;from zendriver_mcp.tools import mcp;t=asyncio.run(mcp.list_tools());"
    "print(json.dumps([len(t),"
    "len(json.dumps([{'n':x.name,'d':x.description or '','i':x.input_schema} for x in t])),"
    "len(json.dumps([x.output_schema for x in t]))]))"
)


def _measure(surface: str) -> tuple[int, int, int]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZENDRIVER_MCP_")}
    env["ZENDRIVER_MCP_SURFACE"] = surface
    out = subprocess.check_output([sys.executable, "-c", _MEASURE], env=env, cwd=ROOT)
    count, size, outputs = json.loads(out.decode().strip().splitlines()[-1])
    return count, size, outputs


def test_catalog_surface_stays_small() -> None:
    count, size, _ = _measure("catalog")
    assert count <= CATALOG_MAX_TOOLS, f"catalogue lists {count} tools; a core tool crept in"
    assert size <= CATALOG_BUDGET_BYTES, f"catalogue surface {size} bytes > {CATALOG_BUDGET_BYTES}"


def test_full_surface_under_budget() -> None:
    count, size, outputs = _measure("full")
    assert size <= FULL_BUDGET_BYTES, f"full surface: {count} tools, {size} bytes"
    assert outputs <= FULL_OUTPUT_BUDGET_BYTES, f"output schemas: {outputs} bytes"
