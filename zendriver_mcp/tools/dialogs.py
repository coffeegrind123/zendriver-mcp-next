# JavaScript dialog tools - answer, list, and set the answering policy
from typing import Annotated, Any, Literal

from pydantic import Field

from zendriver_mcp.dialogs import Policy
from zendriver_mcp.registry import Effect
from zendriver_mcp.response import Result
from zendriver_mcp.tools.base import ToolBase


class DialogResult(Result):
    type: str = Field(description="alert, confirm, prompt or beforeunload.")
    message: str = Field(description="The dialog's text.")
    outcome: str | None = Field(default=None, description="How it was answered.")


class DialogList(Result):
    dialogs: list[dict[str, Any]] = Field(
        description="Recent dialogs, oldest first: tab, type, message, url, opened, outcome (null while open)."
    )
    policy: str = Field(description="How new dialogs are answered: manual, accept or dismiss.")


class PolicyResult(Result):
    policy: str = Field(description="The policy now in force.")


class DialogTools(ToolBase):
    """tools for alert, confirm, prompt and beforeunload dialogs"""

    GROUP = "dialogs"

    def _register_tools(self) -> None:
        self._register(self.handle_dialog, Effect.WRITE, "Answer dialog")
        self._register(self.list_dialogs, Effect.READ, "List dialogs", page_scoped=False)
        self._register(
            self.set_dialog_policy,
            Effect.WRITE,
            "Set dialog policy",
            idempotent=True,
            page_scoped=False,
        )

    async def handle_dialog(
        self,
        accept: Annotated[
            bool,
            Field(
                description="true presses OK (or Leave for beforeunload), false presses Cancel. Example: true"
            ),
        ] = True,
        prompt_text: Annotated[
            str | None,
            Field(
                description="For a prompt dialog: the text to answer with. Omit to accept its default. Example: 'Ada'"
            ),
        ] = None,
    ) -> DialogResult:
        """Answer the alert, confirm, prompt or beforeunload dialog open on the tab.

        While one is open, other tools on the tab fail with dialog_open.
        Returns its type, message and how it was answered.
        """
        dialog = await self.session.dialogs.answer(self.session.page, accept, prompt_text)
        return DialogResult(
            summary=f"{dialog.outcome.capitalize() if dialog.outcome else 'Answered'}: {dialog.describe()}",
            type=dialog.type,
            message=dialog.message,
            outcome=dialog.outcome,
        )

    async def list_dialogs(self) -> DialogList:
        """List recent JavaScript dialogs on every tab, and how each was answered.

        Includes auto-answered ones. Returns up to 50, oldest first, and the policy.
        """
        d = self.session.dialogs
        items = [x.as_dict() for x in d.log]
        open_now = sum(1 for x in d.log if x.outcome is None)
        return DialogList(
            summary=f"{len(items)} dialog(s), {open_now} open; policy {d.policy.value}",
            dialogs=items,
            policy=d.policy.value,
        )

    async def set_dialog_policy(
        self,
        policy: Annotated[
            Literal["manual", "accept", "dismiss"],
            Field(
                description="manual (answer with handle_dialog), accept or dismiss. Example: 'dismiss'"
            ),
        ],
    ) -> PolicyResult:
        """Choose how new JavaScript dialogs are answered, on every tab.

        accept or dismiss answers each as it opens; list_dialogs still records
        them. Returns the policy now in force.
        """
        self.session.dialogs.policy = Policy(policy)
        return PolicyResult(summary=f"Dialog policy: {policy}", policy=policy)
