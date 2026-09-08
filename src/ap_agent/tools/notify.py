"""Send a message to a person.

Caller: code. The orchestrator calls this; no model can.
Side effects: NOTIFICATION.

The only tool that produces something a human outside the system sees, which
makes it the one worth being careful with. Two constraints follow from that:

* The body is assembled from templates and typed fields, not passed through
  from a document or a model. A vendor cannot make this system send a
  message of their choosing.
* It is idempotent, because a retry loop that emails an approver forty times
  is an outage.

Not implemented: the transport, the templates, and the per-recipient rate limit.

"""

from __future__ import annotations

from pydantic import Field

from ap_agent.tools.base import IdempotentToolInput, SideEffect, ToolCaller, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.NOTIFICATION,)
REQUIRES_IDEMPOTENCY_KEY = True


class NotifyInput(IdempotentToolInput):
    """Input for :func:`notify`."""

    recipient_user_id: str = Field(min_length=1, max_length=64)
    template_id: str = Field(
        min_length=1,
        max_length=64,
        description="Templates are the only message bodies. No free text from documents.",
    )
    template_vars: dict[str, str] = Field(default_factory=dict[str, str])
    invoice_id: str | None = Field(default=None, max_length=64)


class NotifyOutput(ToolOutput):
    """Output of :func:`notify`."""

    delivered: bool
    channel: str = Field(min_length=1, max_length=32)
    already_existed: bool = False


def notify(payload: NotifyInput) -> NotifyOutput:
    """Send a message to a person.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
