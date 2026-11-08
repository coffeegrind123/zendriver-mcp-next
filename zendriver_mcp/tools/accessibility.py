"""Accessibility tree with stable uids.

The DOM walker upstream returns numeric IDs based on DOM traversal order;
they re-number on every tree mutation. For long interactive agent flows
we also expose the CDP accessibility tree keyed by stable uids that stay
valid as long as the underlying backend node still exists.

Implementation note: we call ``Accessibility.getFullAXTree`` via a raw CDP
generator that returns the response dict verbatim. Zendriver's bundled
parser ships with a stale ``AXPropertyName`` enum and raises ``ValueError``
on new Chrome values like ``uninteresting``; that exception is swallowed by
the Listener task, which leaves the original future unresolved and the
caller hanging until our timeout guard fires. Parsing the raw JSON here
side-steps the issue entirely.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import uuid4

from mcp.server.mcpserver import MCPServer as FastMCP
from pydantic import Field
from zendriver import cdp

from zendriver_mcp.errors import AccessibilityUidError
from zendriver_mcp.registry import Effect, Reach
from zendriver_mcp.response import Result
from zendriver_mcp.tools.base import ToolBase

UID_DESCRIPTION = "A uid from get_accessibility_snapshot. Example: 'ax_1b2c3d4e'"


class AxSnapshot(Result):
    uid_count: int = Field(description="Number of nodes given a uid.")
    tree: dict[str, Any] | None = Field(
        default=None,
        description="Root node: {uid, role, name, children?}; children have the same shape.",
    )


class AxNodeInfo(Result):
    uid: str = Field(description="The uid described.")
    role: str = Field(description="Accessibility role, e.g. 'button'.")
    name: str = Field(description="Accessible name.")
    backend_node_id: int = Field(description="CDP backend DOM node id (0 for non-DOM nodes).")


class AxClick(Result):
    uid: str = Field(description="The uid clicked.")
    role: str = Field(description="Its role.")
    name: str = Field(description="Its accessible name.")
    x: float = Field(description="Viewport x of the click.")
    y: float = Field(description="Viewport y of the click.")


@dataclass(slots=True)
class _UidEntry:
    backend_node_id: int
    role: str
    name: str


def _raw_get_full_ax_tree(
    depth: int | None = None,
) -> Generator[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """CDP generator that returns ``Accessibility.getFullAXTree`` raw nodes.

    Drop-in replacement for ``cdp.accessibility.get_full_ax_tree()`` that
    bypasses zendriver's ``AXNode.from_json`` (which raises on unknown
    enum values). The connection driver hands us the response dict; we
    return it unchanged so the caller can parse what it needs.
    """
    params: dict[str, Any] = {}
    if depth is not None:
        params["depth"] = depth
    response: dict[str, Any] = yield {
        "method": "Accessibility.getFullAXTree",
        "params": params,
    }
    return list(response.get("nodes", []))


class AccessibilityTools(ToolBase):
    """Accessibility-tree snapshots with stable, re-usable uids."""

    def __init__(self, mcp: FastMCP) -> None:
        # Uid -> metadata. Cleared on session reset so old backendDOMNodeIds
        # from a dead browser don't map to random nodes in the new session.
        self._uids: dict[str, _UidEntry] = {}
        super().__init__(mcp)

    def _reset_state(self) -> None:
        self._uids.clear()

    GROUP = "accessibility"

    def _register_tools(self) -> None:
        # AX tree is slow on complex pages; 30s keeps the tool callable.
        self._register(
            self.get_accessibility_snapshot, Effect.READ, "Accessibility snapshot", timeout=30
        )
        self._register(
            self.click_by_uid, Effect.WRITE, "Click by accessibility uid", reach=Reach.WEB
        )
        self._register(self.describe_uid, Effect.READ, "Describe accessibility uid")

    async def get_accessibility_snapshot(
        self,
        max_nodes: Annotated[
            int,
            Field(
                description="Cap on the number of nodes returned, to keep the reply small. Example: 400"
            ),
        ] = 400,
        interesting_only: Annotated[
            bool,
            Field(
                description="Skip ignored and structural nodes (generic, StaticText, ...) while keeping their children. Example: true"
            ),
        ] = True,
    ) -> AxSnapshot:
        """Return the page's accessibility tree with stable uids.

        Each node looks like ``{"uid": "ax_1b2c", "role": "button",
        "name": "Submit", "children": [...]}``. The uid can be passed to
        ``click_by_uid`` for deterministic interaction.

        - ``max_nodes``: cap the node count to keep the response small.
        - ``interesting_only``: skip ignored/structural nodes (``none``,
          ``generic``, ``InlineTextBox``, ``StaticText``) while keeping
          their children, so the tree stays compact without losing content.
        """
        tab = self.session.page
        await tab.send(cdp.accessibility.enable())
        raw_nodes: list[dict[str, Any]] = await tab.send(_raw_get_full_ax_tree())

        # Stable uids: reuse the existing uid for a backend_node_id that's
        # still live, so agents can keep a uid between snapshots. Build a
        # lookup of backend_node_id -> uid from the previous cache before
        # rebuilding.
        prev_by_backend: dict[int, str] = {
            entry.backend_node_id: uid for uid, entry in self._uids.items() if entry.backend_node_id
        }
        self._uids.clear()

        by_id: dict[str, dict[str, Any]] = {
            str(node.get("nodeId")): node for node in raw_nodes if node.get("nodeId") is not None
        }

        # The root is the node without a parent, not necessarily raw_nodes[0].
        roots = [n for n in raw_nodes if n.get("parentId") in (None, "")]

        interactive_roles = {
            "button",
            "link",
            "textbox",
            "searchbox",
            "checkbox",
            "radio",
            "switch",
            "combobox",
            "menuitem",
            "menuitemcheckbox",
            "menuitemradio",
            "tab",
            "option",
            "treeitem",
        }
        noise_roles = {
            "none",
            "generic",
            "presentation",
            "InlineTextBox",
            "StaticText",
        }

        def render(node: dict[str, Any]) -> list[dict[str, Any]]:
            """Return a list of rendered children for ``node``.

            A list (not a single dict) makes it trivial to flatten skipped
            wrapper nodes: we just concatenate their children into the
            parent's list.
            """
            role = _string_value(node.get("role"))
            name = _string_value(node.get("name"))
            ignored = bool(node.get("ignored"))
            backend_id = int(node.get("backendDOMNodeId") or 0)
            child_ids = node.get("childIds") or []

            rendered_children: list[dict[str, Any]] = []
            for cid in child_ids:
                child = by_id.get(str(cid))
                if child is None:
                    continue
                rendered_children.extend(render(child))

            skip = False
            if interesting_only:
                is_interactive = role in interactive_roles
                if not is_interactive:
                    if ignored or role in noise_roles:
                        skip = True
                    elif not role and not name:
                        skip = True

            if skip:
                # Don't emit ourselves, bubble children up instead.
                return rendered_children

            if len(self._uids) >= max_nodes:
                return rendered_children

            # Reuse the previous snapshot's uid for this backend_node_id
            # whenever possible so agents can cache uids across snapshots.
            uid = prev_by_backend.get(backend_id) or f"ax_{uuid4().hex[:8]}"
            self._uids[uid] = _UidEntry(backend_node_id=backend_id, role=role, name=name)
            entry: dict[str, Any] = {"uid": uid, "role": role, "name": name}
            if rendered_children:
                entry["children"] = rendered_children
            return [entry]

        if not roots:
            return AxSnapshot(summary="Empty accessibility tree", uid_count=0)

        # Some pages have multiple top-level AX roots (OOPIF iframes,
        # embedded PDFs, etc.). Render every root and flatten the rendered
        # node lists together so nothing is silently dropped.
        top: list[dict[str, Any]] = []
        for root in roots:
            top.extend(render(root))

        if len(top) == 1:
            tree: dict[str, Any] = top[0]
        else:
            tree = {"uid": "", "role": "", "name": "", "children": top}
        return AxSnapshot(
            summary=f"Accessibility snapshot: {len(self._uids)} nodes",
            uid_count=len(self._uids),
            tree=tree,
        )

    def _entry(self, uid: str) -> _UidEntry:
        entry = self._uids.get(uid)
        if entry is None:
            from zendriver_mcp.suggest import did_you_mean

            raise AccessibilityUidError(
                f"Unknown accessibility uid: {uid}",
                hint=did_you_mean(uid, list(self._uids)),
            )
        return entry

    async def describe_uid(
        self, uid: Annotated[str, Field(description=UID_DESCRIPTION)]
    ) -> AxNodeInfo:
        """Return the role, name and DOM node id cached for an accessibility uid.

        Reads the last snapshot only; it does not touch the page. Fails with a
        stale_id error if the uid is not in the latest snapshot.
        """
        entry = self._entry(uid)
        return AxNodeInfo(
            summary=f"{uid}: {entry.role} {entry.name[:60]!r}",
            uid=uid,
            role=entry.role,
            name=entry.name,
            backend_node_id=entry.backend_node_id,
        )

    async def click_by_uid(
        self, uid: Annotated[str, Field(description=UID_DESCRIPTION)]
    ) -> AxClick:
        """Click the DOM node behind an accessibility uid with a real (trusted) click.

        The click is dispatched as CDP mouse input at the node's centre, after
        scrolling it into view, so the page sees an ordinary user click. Fails
        if the uid is unknown, its node was removed since the snapshot, or
        something else covers it.
        """
        entry = self._entry(uid)
        if entry.backend_node_id == 0:
            raise AccessibilityUidError(
                f"uid {uid} ({entry.role}) has no DOM node to click.",
                hint="Pick an interactive node (button, link, textbox) from the snapshot.",
            )

        label = f"uid:{uid}"
        try:
            handle = await self.dom.handle_for_node(
                cdp.dom.BackendNodeId(entry.backend_node_id), label
            )
        except Exception as exc:  # stale backend node ids raise from CDP
            raise AccessibilityUidError(
                f"uid {uid} no longer maps to a live DOM node",
                hint="Take a fresh get_accessibility_snapshot and use a uid from it.",
            ) from exc
        x, y = await self.dom.center(handle)
        await self.dom.click(handle)
        return AxClick(
            summary=f"Clicked uid {uid} ({entry.role}: {entry.name[:40]})",
            uid=uid,
            role=entry.role,
            name=entry.name,
            x=round(x, 1),
            y=round(y, 1),
        )


def _string_value(ax_value: dict[str, Any] | None) -> str:
    """Extract the stringified value from a raw ``AXValue`` dict.

    The CDP ``AXValue`` shape is ``{"type": "...", "value": ...}``. We only
    care about string-ish values here.
    """
    if not ax_value:
        return ""
    value = ax_value.get("value")
    if value is None:
        return ""
    return str(value)
