"""Tell a person something is waiting for them.

Caller: code. The orchestrator calls this; no model can.
Side effects: NOTIFICATION.

**For now the channel is the log.** A structured ``structlog`` event carrying
the template, its variables and the idempotency key; `just run` prints the
matching instruction for a person at the terminal. Email or chat is a delivery
change behind this same interface, not a change to the loop.

Templates are the only message bodies, and their variables are this system's
own values - ids, amounts, roles. No text from a document is ever sent.
"""

from __future__ import annotations

from pydantic import Field

from ap_agent.logging import get_logger
from ap_agent.tools.base import IdempotentToolInput, SideEffect, ToolCaller, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.NOTIFICATION,)
REQUIRES_IDEMPOTENCY_KEY = True

CHANNEL = "log"

log = get_logger(__name__)


class NotifyInput(IdempotentToolInput):
    """Input for :func:`notify`."""

    recipient_user_id: str = Field(
        min_length=1,
        max_length=64,
        description="A person or a role. The approval matrix names roles, so today it is one.",
    )
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
    """Emit the notification as a structured log event."""
    log.info(
        "notify",
        channel=CHANNEL,
        recipient=payload.recipient_user_id,
        template=payload.template_id,
        invoice_id=payload.invoice_id,
        idempotency_key=payload.idempotency_key,
        **payload.template_vars,
    )
    return NotifyOutput(delivered=True, channel=CHANNEL)
