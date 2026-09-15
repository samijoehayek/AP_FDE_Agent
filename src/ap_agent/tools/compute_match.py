"""Match an invoice against its POs and receipts. Deterministic.

Caller: code. The orchestrator calls this; no model can.
Side effects: NONE.

Pure: same inputs, same ``MatchResult``, every time. This is the function
that decides whether money is owed, and it is code precisely so that its
output can be replayed, diffed, and unit-tested against the golden set.

Tolerances come from the versioned guardrails config and the version used is
recorded on the result, so a decision made last quarter can be re-explained
under the rules that were actually in force.

**This module is the wrapper; the reasoning is in** :mod:`ap_agent.matching`.
The split is not ceremony. A tool takes one input model and returns one output
model, which is the right shape for the loop and the wrong shape for reading a
decision about money: every argument arrives as a field on an envelope, and the
function that compares a price ends up also unpacking a payload. Keeping the
comparison in plain functions means it can be imported, called with three
objects, and tested without building a tool input at all.

Still not implemented: multi-PO invoices. An invoice citing several orders is a
real case and a different shape of match - the lines have to be assigned across
orders before any of them can be compared - and ``purchase_orders`` is a list
here so that adding it later does not change this signature. Today a payload
carrying more than one order is refused rather than silently matched against the
first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field

from ap_agent.contracts.enums import ReasonCode
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.contracts.matching import MatchResult
from ap_agent.contracts.purchase_order import PurchaseOrder, ReceiptSet
from ap_agent.guardrails.config import GuardrailsError, load_guardrails
from ap_agent.matching import compute_match as match_invoice
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

if TYPE_CHECKING:
    from ap_agent.contracts.guardrails import GuardrailConfig

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.NONE,)
REQUIRES_IDEMPOTENCY_KEY = False


class ComputeMatchInput(ToolInput):
    """Input for :func:`compute_match`."""

    invoice_id: str = Field(min_length=1, max_length=64)
    extraction: InvoiceExtraction
    purchase_orders: list[PurchaseOrder] = Field(default_factory=list[PurchaseOrder])
    receipts: list[ReceiptSet] = Field(
        default_factory=list[ReceiptSet],
        description="One set per purchase order, parallel to `purchase_orders`. A PO with "
        "nothing received is an empty set, never absent - see ReceiptSet.",
    )
    config_version: str = Field(min_length=1, max_length=32)


class ComputeMatchOutput(ToolOutput):
    """Output of :func:`compute_match`."""

    result: MatchResult


def compute_match(payload: ComputeMatchInput) -> ComputeMatchOutput:
    """Match an invoice against its PO and receipts. Deterministic.

    Unpacks the envelope, loads the named guardrails version, and hands three
    objects to :func:`ap_agent.matching.compute_match`.

    Args:
        payload: The invoice, the order, what arrived, and the config version.

    Returns:
        The match result, stamped with the config version that produced it.

    Never raises on a bad invoice. The two ways a payload can be unusable - no
    purchase order, or more than one - come back as reason codes, because they
    are facts about the document and not failures of this function. A missing
    *config* does raise: a tolerance nobody can name is not a decision anyone
    can audit later, and continuing with a guess would put a version string on a
    result that did not produce it.
    """
    if not payload.purchase_orders:
        return _no_match(payload, ReasonCode.PO_NOT_FOUND, po_number="")
    if len(payload.purchase_orders) > 1:
        # Deliberately refused rather than matched against the first. Picking
        # one order out of several and reporting a clean match would be the
        # worst available outcome: the lines belonging to the other orders would
        # all read as charges nobody ordered, or worse, as nothing at all.
        return _no_match(
            payload,
            ReasonCode.MISSING_PO_REFERENCE,
            po_number=payload.purchase_orders[0].po_number,
        )

    order = payload.purchase_orders[0]
    receipts = _receipts_for(order.po_number, payload.receipts)
    config = _config_named(payload.config_version)
    return ComputeMatchOutput(result=match_invoice(payload.extraction, order, receipts, config))


def _config_named(version: str) -> GuardrailConfig:
    """Load the guardrails, and refuse if they are not the version asked for.

    The caller names a version and this checks it rather than trusting it. The
    failure being guarded against is quiet: a run started under
    ``guardrails_v1`` while the settings point at ``v2`` would decide every
    invoice under one ruleset and stamp the other on the result, and the audit
    trail would be confidently wrong about why each invoice was held. A loud
    failure at the first invoice is much cheaper than a quarter of decisions
    nobody can re-explain.
    """
    config = load_guardrails()
    if config.config_version != version:
        msg = (
            f"asked to match under {version!r} but the loaded guardrails are "
            f"{config.config_version!r}"
        )
        raise GuardrailsError(msg)
    return config


def _receipts_for(po_number: str, sets: list[ReceiptSet]) -> ReceiptSet:
    """The receipt set for one order, or an empty one.

    An absent set means nothing arrived, which is a fact the matcher must act
    on - never a lookup to retry. See :class:`ReceiptSet`.
    """
    for received in sets:
        if received.po_number == po_number:
            return received
    return ReceiptSet(po_number=po_number)


def _no_match(payload: ComputeMatchInput, code: ReasonCode, po_number: str) -> ComputeMatchOutput:
    """A result carrying one reason and no line detail.

    ``po_number`` falls back to what the document printed, because a result has
    to say which order it is about even when that order could not be found, and
    the document's claim is the only candidate there is. It is labelling, never
    authority: nothing downstream reads it as evidence the order exists.
    """
    printed = payload.extraction.po_references
    return ComputeMatchOutput(
        result=MatchResult(
            matched=False,
            reason_codes=[code],
            config_version=payload.config_version,
            po_number=po_number or (printed[0] if printed else payload.invoice_id),
        )
    )
