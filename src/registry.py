"""What each tool is, beyond its schema: effect, title, group, budget.

Filled by ``ToolBase._register`` as modules register, read by the catalogue
(search, describe, batch), the annotations sent in ``tools/list``, and the
tests that hold every tool to the same standard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from mcp_types import ToolAnnotations


class Effect(Enum):
    """What a call does to the browser, the page, or the machine.

    READ        observes only; safe to repeat and to run speculatively.
    WRITE       changes page or browser state in an ordinary, expected way
                (navigate, click, type, set a header). Not destructive.
    DESTRUCTIVE discards state the caller cannot get back (stop the browser,
                close a tab, clear cookies or storage), or writes files.
    """

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class Reach(Enum):
    """Whether a call can touch the network or other open-ended entities.

    WEB    may load pages, submit forms, or otherwise talk to sites.
    LOCAL  stays inside this browser and this machine.
    """

    WEB = "web"
    LOCAL = "local"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    group: str
    effect: Effect
    reach: Reach
    idempotent: bool
    page_scoped: bool
    timeout: float
    summary: str = field(default="")

    def annotations(self) -> ToolAnnotations:
        read_only = self.effect is Effect.READ
        return ToolAnnotations(
            title=self.title,
            read_only_hint=read_only,
            # The spec defaults destructiveHint to TRUE when absent, so a WRITE
            # tool has to say False explicitly or clients treat it as dangerous.
            destructive_hint=None if read_only else self.effect is Effect.DESTRUCTIVE,
            idempotent_hint=None if read_only else self.idempotent,
            open_world_hint=self.reach is Reach.WEB,
        )


SPECS: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> None:
    SPECS[spec.name] = spec
