"""Tool results: one typed model per tool, sent as structured content plus text.

Every tool returns a subclass of :class:`Result`. Its pydantic schema becomes
the tool's ``outputSchema``, and the wrapper in ``zendriver_mcp/tools/base.py`` turns an
instance into a ``CallToolResult`` carrying:

* ``structuredContent``: the model dumped to JSON (None fields omitted), for
  clients and programs (``batch`` reads ``$N.field`` from it);
* ``content``: :meth:`Result.render` as text, for clients that only show text
  (the spec asks servers to send both), plus any images or resource links from
  :meth:`Result.media`.

Failures are not results. A tool raises a ``ZendriverMCPError`` and
:func:`error_result` turns it into ``isError`` with ``{error, code, hint}``.
"""

from __future__ import annotations

import json
from typing import Any

from mcp_types import (
    CallToolResult,
    ContentBlock,
    ImageContent,
    ResourceLink,
    TextContent,
)
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from zendriver_mcp.errors import Code, ZendriverMCPError

# Names a result field must not take: they are Result's methods, and a field
# would shadow them silently (a `media` field broke media()).
RESERVED_FIELD_NAMES = frozenset({"render", "media", "attach_image", "to_call_result"})


class Artifact(BaseModel):
    """A file a tool wrote, reachable both on disk and as an MCP resource."""

    path: str = Field(description="Absolute path on the server's filesystem.")
    uri: str = Field(description="Resource URI; read it with resources/read.")
    mime_type: str = Field(description="MIME type of the file.")
    size: int = Field(description="Size in bytes.")


class Result(BaseModel):
    """Base of every tool result."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="One line saying what happened.")

    _images: list[ImageContent] = PrivateAttr(default_factory=list)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        clash = RESERVED_FIELD_NAMES & set(cls.model_fields)
        if clash:
            raise TypeError(
                f"{cls.__name__} has fields named {sorted(clash)}, which Result reserves."
            )

    def render(self) -> str:
        """Text for clients that read ``content`` only.

        The summary, then every other populated field as compact JSON. A
        subclass whose payload is prose (page text, HTML) overrides this so the
        model is not handed a JSON-escaped wall of text.
        """
        rest = self.model_dump(mode="json", exclude_none=True, exclude={"summary", "files"})
        if not rest:
            return self.summary
        return self.summary + "\n" + json.dumps(rest, separators=(",", ":"), ensure_ascii=False)

    def media(self) -> list[ContentBlock]:
        """Non-text content blocks: attached images, then a link per file."""
        blocks: list[ContentBlock] = list(self._images)
        for f in getattr(self, "files", None) or []:
            blocks.append(
                ResourceLink(
                    type="resource_link",
                    uri=f.uri,
                    name=f.uri.rsplit("/", 1)[-1],
                    mime_type=f.mime_type,
                    size=f.size,
                )
            )
        return blocks

    def attach_image(self, data: bytes, mime_type: str) -> None:
        import base64

        self._images.append(
            ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type=mime_type)
        )

    def to_call_result(self) -> CallToolResult:
        content: list[ContentBlock] = [TextContent(type="text", text=self.render())]
        content.extend(self.media())
        return CallToolResult(
            content=content,
            structured_content=self.model_dump(mode="json", exclude_none=True),
        )


class FileResult(Result):
    """A result for a tool that writes files: each is also an MCP resource."""

    files: list[Artifact] = Field(description="Files this call wrote.")


class ErrorPayload(BaseModel):
    error: str
    code: str
    hint: str | None = None


def error_result(exc: BaseException) -> CallToolResult:
    """Turn any exception into an ``isError`` result the model can act on.

    Deliberate failures keep their message, code and hint. Anything else keeps
    its type and message too: this is a local automation server whose caller is
    the user's own agent, and "AttributeError: 'NoneType' object has no attribute
    'url'" lets it work around a problem that a bare "internal error" would hide.
    """
    # A bare ValueError is NOT mapped to invalid_argument: library internals
    # raise it too (zendriver's "list.remove(x): x not in list"), and calling
    # those the caller's mistake sends the model off fixing its arguments.
    # Argument problems are raised as InvalidArgumentError on purpose.
    if isinstance(exc, ZendriverMCPError):
        payload = ErrorPayload(error=str(exc), code=exc.code.value, hint=exc.hint)
    else:
        text = str(exc).strip()
        payload = ErrorPayload(
            error=f"{type(exc).__name__}: {text}" if text else type(exc).__name__,
            code=Code.INTERNAL.value,
            hint="Unexpected failure inside the tool. Check get_browser_status, then retry once.",
        )
    text = payload.error if payload.hint is None else f"{payload.error}\nHint: {payload.hint}"
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=payload.model_dump(exclude_none=True),
        is_error=True,
    )


def structured_of(result: Any) -> dict[str, Any] | None:
    """The structured payload of a finished call, whatever shape it came back in."""
    if isinstance(result, Result):
        return result.model_dump(mode="json", exclude_none=True)
    if isinstance(result, CallToolResult):
        return result.structured_content
    return None
