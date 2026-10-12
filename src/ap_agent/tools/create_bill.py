"""Create an unpaid bill in QuickBooks - the first write with consequences.

Caller: code. The orchestrator calls this; no model can.
Side effects: ERP_READ, ERP_WRITE, DB_WRITE. Writes the key to ``invoices``.

The only tool in this package that creates an accounting record, and it
creates an *unpaid* one. It records a liability; it does not move money, and
there is no tool here that does.

**Preconditions are re-read, not trusted.** The invoice's state comes from the
store the run bound (:data:`~ap_agent.persistence.CURRENT_INVOICE_STORE`), not
from the caller, and it must be ``APPROVED``. The approval comes from the
approval store: it must be this invoice's, complete, and for exactly this
amount and currency. Any of those failing is :class:`PostingRefusedError`, and
QuickBooks is never called.

**Idempotency: the key is ``(vendor_id, normalised invoice number)``**, as
``bill:<vendor_id>:<number>``, and in QuickBooks it is the bill's ``VendorRef``
and ``DocNumber``. In order:

1. The key is written to the invoice row and committed. Unique, so a second
   invoice for the same vendor and number is refused here.
2. QuickBooks is asked for a bill with that ``DocNumber`` from that vendor. If
   one is there, its id is the answer and nothing is created. This is what
   makes a retry after a crash safe: the process that died after QuickBooks
   answered left a bill behind, and the retry finds it.
3. Only then is the bill created.

A bill found under the key with a different total is not "ours, already
made": it is :class:`BillConflictError`, and a person looks at it.

**No document text goes to QuickBooks.** Each line's description is the purchase
order's, which the ERP already holds; the invoice contributes numbers, a date
and an alphanumeric number. The memo names this system's own ids.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import Field, model_validator

from ap_agent.contracts.common import CurrencyCode, Money, Quantity, StrictModel, UnitPrice
from ap_agent.errors import APAgentError
from ap_agent.persistence import CURRENT_APPROVAL_STORE, CURRENT_INVOICE_STORE, ErpKeyTakenError
from ap_agent.states.machine import InvoiceState
from ap_agent.tools.base import IdempotentToolInput, SideEffect, ToolCaller, ToolOutput

if TYPE_CHECKING:
    from typing import Self

    from ap_agent.integrations.qbo.client import QboClient

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (
    SideEffect.ERP_READ,
    SideEffect.ERP_WRITE,
    SideEffect.DB_WRITE,
)
REQUIRES_IDEMPOTENCY_KEY = True

QBO_ENTITY: Final = "Bill"

DOC_NUMBER_PATTERN: Final = re.compile(r"^[0-9A-Z]{1,21}$")
"""A normalised invoice number, as QuickBooks will hold it.

Twenty-one characters is QuickBooks' limit for a bill's ``DocNumber``. Letters
and digits only, which is also why it is safe inside a query string: nothing
that came off a document reaches QuickBooks' query parser with a quote in it.
"""

_CENT: Final = Decimal("0.01")


class PostingRefusedError(APAgentError):
    """A precondition does not hold. Nothing was written anywhere."""


class BillConflictError(APAgentError):
    """The key names a bill that is not this invoice's. A person decides."""


class BillLine(StrictModel):
    """One line of the bill: a purchase-order item, billed at the invoice's numbers."""

    item_ref: str = Field(min_length=1, max_length=64, description="QuickBooks' item id.")
    description: str | None = Field(
        default=None,
        max_length=500,
        description="The purchase-order line's description - the ERP's words, not the vendor's.",
    )
    quantity: Quantity
    unit_price: UnitPrice
    amount: Money


def idempotency_key_for(vendor_id: str, doc_number: str) -> str:
    """The bill's key. One per vendor and number, whichever invoice asks."""
    return f"bill:{vendor_id}:{doc_number}"


class CreateBillInput(IdempotentToolInput):
    """Input for :func:`create_bill`."""

    invoice_id: str = Field(min_length=1, max_length=64)
    vendor_id: str = Field(min_length=1, max_length=64)
    doc_number: str = Field(
        min_length=1,
        max_length=21,
        description="The normalised invoice number. The bill's DocNumber.",
    )
    approval_id: str = Field(
        min_length=1,
        max_length=64,
        description="The human approval authorising this. Re-verified here, not trusted.",
    )
    currency: CurrencyCode
    total: Money
    txn_date: date
    due_date: date | None = None
    lines: list[BillLine] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not DOC_NUMBER_PATTERN.fullmatch(self.doc_number):
            msg = f"doc_number {self.doc_number!r} is not a normalised invoice number"
            raise ValueError(msg)
        if self.idempotency_key != idempotency_key_for(self.vendor_id, self.doc_number):
            msg = "idempotency_key must be derived from vendor_id and doc_number"
            raise ValueError(msg)
        if sum((line.amount for line in self.lines), Decimal(0)) != self.total:
            msg = "the lines do not add up to the total; the bill would not be the invoice"
            raise ValueError(msg)
        return self


class CreateBillOutput(ToolOutput):
    """Output of :func:`create_bill`."""

    erp_bill_id: str = Field(min_length=1, max_length=64)
    already_existed: bool = Field(
        description="True when QuickBooks already held a bill under this key. Not an error."
    )
    idempotency_key: str = Field(min_length=8, max_length=128)
    doc_number: str = Field(min_length=1, max_length=21)


def create_bill(payload: CreateBillInput) -> CreateBillOutput:
    """Find the bill this key names, or create it.

    Raises:
        PostingRefusedError: Called outside a run, not APPROVED, or the approval
            does not cover this bill. Nothing was written.
        BillConflictError: The key is held by another invoice, or QuickBooks
            holds a bill under it for a different total.
        QboError: QuickBooks failed. The key is written; a retry looks first.
    """
    _check_preconditions(payload)
    try:
        CURRENT_INVOICE_STORE.get().claim_erp_key(payload.invoice_id, payload.idempotency_key)
    except ErpKeyTakenError as exc:
        raise BillConflictError(str(exc)) from exc

    client = _client()
    found = _find(client, payload)
    if found is not None:
        return _output(payload, found, already_existed=True)
    created = cast(
        "dict[str, Any]", client.create(QBO_ENTITY, bill_payload(payload)).get(QBO_ENTITY) or {}
    )
    return _output(payload, created, already_existed=False)


def _check_preconditions(payload: CreateBillInput) -> None:
    try:
        invoices = CURRENT_INVOICE_STORE.get()
        approvals = CURRENT_APPROVAL_STORE.get()
    except LookupError as exc:
        msg = "create_bill was called outside a run; no stores are bound"
        raise PostingRefusedError(msg) from exc

    record = invoices.load(payload.invoice_id)
    if record.state is not InvoiceState.APPROVED:
        msg = (
            f"invoice {payload.invoice_id} is {record.state.value}; "
            "a bill is created only from APPROVED"
        )
        raise PostingRefusedError(msg)
    if record.vendor_id != payload.vendor_id:
        msg = f"vendor {payload.vendor_id} is not the invoice's resolved vendor"
        raise PostingRefusedError(msg)

    approval = approvals.get(payload.approval_id)
    if approval is None or approval.invoice_id != payload.invoice_id:
        msg = f"approval {payload.approval_id} is not this invoice's"
        raise PostingRefusedError(msg)
    if approval.status != "approved":
        msg = f"approval {payload.approval_id} is {approval.status}, not approved"
        raise PostingRefusedError(msg)
    if approval.amount != payload.total or approval.currency != payload.currency:
        msg = (
            f"approval {payload.approval_id} is for {approval.amount} {approval.currency}; "
            f"the bill would be {payload.total} {payload.currency}"
        )
        raise PostingRefusedError(msg)


def _find(client: QboClient, payload: CreateBillInput) -> dict[str, Any] | None:
    """The bill QuickBooks holds under this key, or None. A different total is a conflict."""
    statement = f"SELECT * FROM {QBO_ENTITY} WHERE DocNumber = '{payload.doc_number}'"  # noqa: S608 - not SQL; DOC_NUMBER_PATTERN admits no quote
    envelope = cast("dict[str, Any]", client.query(statement).get("QueryResponse") or {})
    rows = cast("list[Any]", envelope.get(QBO_ENTITY) or [])
    ours = [
        bill
        for bill in (cast("dict[str, Any]", row) for row in rows if isinstance(row, dict))
        if _vendor_of(bill) == payload.vendor_id
    ]
    if not ours:
        return None
    bill = ours[0]
    total = _decimal(bill.get("TotalAmt"))
    if len(ours) > 1 or total != payload.total:
        msg = (
            f"QuickBooks holds {len(ours)} bill(s) for vendor {payload.vendor_id} numbered "
            f"{payload.doc_number}, the first for {total}; this invoice is for {payload.total}"
        )
        raise BillConflictError(msg)
    return bill


def _vendor_of(bill: dict[str, Any]) -> str:
    ref = cast("dict[str, Any]", bill.get("VendorRef") or {})
    return str(ref.get("value"))


def bill_payload(payload: CreateBillInput) -> dict[str, Any]:
    """The QuickBooks Bill, built from the input and nothing else."""
    body: dict[str, Any] = {
        "VendorRef": {"value": payload.vendor_id},
        "DocNumber": payload.doc_number,
        "TxnDate": payload.txn_date.isoformat(),
        "PrivateNote": f"ap-agent invoice {payload.invoice_id}, approval {payload.approval_id}",
        "Line": [
            {
                "DetailType": "ItemBasedExpenseLineDetail",
                "Amount": json_number(line.amount),
                **({"Description": line.description} if line.description else {}),
                "ItemBasedExpenseLineDetail": {
                    "ItemRef": {"value": line.item_ref},
                    "Qty": json_number(line.quantity),
                    "UnitPrice": json_number(line.unit_price),
                },
            }
            for line in payload.lines
        ],
    }
    if payload.due_date is not None:
        body["DueDate"] = payload.due_date.isoformat()
    return body


def json_number(value: Decimal) -> float:
    """A Decimal as the JSON number QuickBooks wants - refused unless it survives the trip.

    The one place an amount becomes a float, because JSON has no decimal type
    and the client serialises with the standard library. The value is checked
    to round-trip exactly, so a float can carry it here but never change it.
    """
    number = float(value)
    if Decimal(repr(number)) != value:
        msg = f"{value} does not survive conversion to a JSON number"
        raise PostingRefusedError(msg)
    return number


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(_CENT)


def _output(
    payload: CreateBillInput, bill: dict[str, Any], *, already_existed: bool
) -> CreateBillOutput:
    bill_id: object = bill.get("Id")
    if bill_id is None:
        msg = "QuickBooks answered without a bill id"
        raise BillConflictError(msg)
    return CreateBillOutput(
        erp_bill_id=str(bill_id),
        already_existed=already_existed,
        idempotency_key=payload.idempotency_key,
        doc_number=payload.doc_number,
    )


def _client() -> QboClient:
    """Build the client at call time; see ``get_purchase_order._client``. Tests replace this."""
    from ap_agent.integrations.qbo.client import QboClient  # noqa: PLC0415

    return QboClient()


__all__ = [
    "CALLER",
    "DOC_NUMBER_PATTERN",
    "QBO_ENTITY",
    "REQUIRES_IDEMPOTENCY_KEY",
    "SIDE_EFFECTS",
    "BillConflictError",
    "BillLine",
    "CreateBillInput",
    "CreateBillOutput",
    "PostingRefusedError",
    "bill_payload",
    "create_bill",
    "idempotency_key_for",
    "json_number",
]
