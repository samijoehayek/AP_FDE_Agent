"""Closed vocabularies.

Every enum here is *closed on purpose*. A model that can invent a reason code
can invent a reason to pay an invoice. When the model must categorise, it picks
from a fixed list or the response fails validation and is retried.

``StrEnum`` is used throughout so that members serialise as their value in JSON
and compare equal to plain strings at a database boundary.
"""

from __future__ import annotations

from enum import StrEnum


class ArithmeticFlag(StrEnum):
    """Internal inconsistencies found in an extraction's own numbers.

    These are recorded, never raised. A document whose totals do not add up is
    a real document with a real problem, and the pipeline must be able to route
    it to a human rather than crash on it.
    """

    TOTALS_DO_NOT_SUM = "totals_do_not_sum"
    LINES_DO_NOT_SUM_TO_SUBTOTAL = "lines_do_not_sum_to_subtotal"
    LINE_EXTENSION_MISMATCH = "line_extension_mismatch"
    NEGATIVE_TOTAL = "negative_total"
    NO_LINE_ITEMS = "no_line_items"


class MatchLineOutcome(StrEnum):
    """What the matcher decided about one line.

    Five outcomes, and the last two are about lines that have no counterpart
    rather than lines that disagree:

    * ``UNMATCHED`` - on the invoice, on no purchase-order line. Freight is the
      ordinary case and is judged against the unmatched-charge tolerance rather
      than rejected outright.
    * ``UNBILLED`` - on the purchase order, on no invoice line. **Not an
      exception.** A vendor invoicing part of an order is the normal shape of a
      partial delivery; it is recorded so a reviewer can see what is still
      outstanding, and it holds nothing up.
    """

    OK = "ok"
    QTY_OVER = "qty_over"
    PRICE_OVER = "price_over"
    UNMATCHED = "unmatched"
    UNBILLED = "unbilled"


class ReasonCode(StrEnum):
    """The closed set of reasons an invoice can fail to flow straight through.

    One vocabulary is shared by :class:`~ap_agent.contracts.matching.MatchResult`
    and :class:`~ap_agent.contracts.exceptions.ExceptionClassification` so that a
    deterministic rule and a model-written explanation can never disagree about
    *what* went wrong - only about how to describe it.

    Two members are worth knowing about before reading the matcher.

    ``PO_VENDOR_MISMATCH`` is an *identity* failure, not a variance: the invoice
    quotes a purchase-order number belonging to a different supplier. There is no
    band inside which that is acceptable, which is why it is a reason code rather
    than a tolerance - a signal compared against a band is a signal that can be
    argued into passing.

    ``UOM_MISMATCH`` **can never fire against a QuickBooks purchase order.**
    QuickBooks does not put a unit of measure on a purchase-order line, so
    ``PurchaseOrderLine`` carries none and there is nothing to compare an
    invoice's ``unit`` against. It stays in the vocabulary because the concept is
    real and an ERP that does carry a unit would raise it; it is simply
    unreachable on the ERP this system talks to today.
    """

    # Document / extraction
    EXTRACTION_LOW_CONFIDENCE = "extraction_low_confidence"
    ARITHMETIC_INCONSISTENT = "arithmetic_inconsistent"
    UNSUPPORTED_CURRENCY = "unsupported_currency"
    SUSPICIOUS_DOCUMENT_CONTENT = "suspicious_document_content"
    MISSING_PO_REFERENCE = "missing_po_reference"

    # Vendor
    VENDOR_NOT_FOUND = "vendor_not_found"
    VENDOR_INACTIVE = "vendor_inactive"
    VENDOR_TAX_ID_MISMATCH = "vendor_tax_id_mismatch"
    BANK_DETAILS_NOT_ON_FILE = "bank_details_not_on_file"

    # Duplicates
    DUPLICATE_SUSPECTED = "duplicate_suspected"

    # Purchase order / receipt
    PO_NOT_FOUND = "po_not_found"
    PO_CLOSED = "po_closed"
    PO_VENDOR_MISMATCH = "po_vendor_mismatch"
    LINE_NOT_ON_PO = "line_not_on_po"
    PRICE_OVER_TOLERANCE = "price_over_tolerance"
    QUANTITY_OVER_TOLERANCE = "quantity_over_tolerance"
    UOM_MISMATCH = "uom_mismatch"
    RECEIPT_MISSING = "receipt_missing"
    RECEIPT_PARTIAL = "receipt_partial"

    # Totals
    TOTALS_OVER_TOLERANCE = "totals_over_tolerance"
    TAX_MISMATCH = "tax_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"

    # Terms
    PAYMENT_TERMS_MISMATCH = "payment_terms_mismatch"


class SuggestedResolver(StrEnum):
    """Who can actually resolve an exception. Closed: there is no "someone"."""

    BUYER = "buyer"
    RECEIVING = "receiving"
    VENDOR = "vendor"
    AP_CLERK = "ap_clerk"
    CONTROLLER = "controller"


class SuggestedAction(StrEnum):
    """The closed set of next actions a model may suggest.

    Note what is absent: nothing here pays, releases, or edits a vendor. A
    suggestion is a routing hint for a human, never an instruction to a tool.
    """

    REQUEST_CREDIT_MEMO = "request_credit_memo"
    REQUEST_CORRECTED_INVOICE = "request_corrected_invoice"
    REQUEST_PO_AMENDMENT = "request_po_amendment"
    REQUEST_GOODS_RECEIPT = "request_goods_receipt"
    REQUEST_PO_REFERENCE = "request_po_reference"
    VERIFY_VENDOR_IDENTITY = "verify_vendor_identity"
    HOLD_FOR_MANUAL_REVIEW = "hold_for_manual_review"
    ESCALATE_TO_CONTROLLER = "escalate_to_controller"
    APPROVE_WITHIN_TOLERANCE = "approve_within_tolerance"
    REJECT_INVOICE = "reject_invoice"


class ActorKind(StrEnum):
    """Discriminator for the audit actor union."""

    SYSTEM = "system"
    RULE = "rule"
    MODEL = "model"
    HUMAN = "human"
    TOOL = "tool"


class AuditEventType(StrEnum):
    """The closed set of things worth recording in the audit trail."""

    STATE_TRANSITION = "state_transition"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MODEL_CALL = "model_call"
    RULE_EVALUATION = "rule_evaluation"
    HUMAN_DECISION = "human_decision"
    APPROVAL_REQUESTED = "approval_requested"
    ERP_WRITE = "erp_write"
    ERROR = "error"
    RETRY = "retry"
    NOTE = "note"

    HALT = "halt"
    """The run stopped because the next step's tool does not exist yet.

    Distinct from ERROR on purpose. An invoice parked at NON_PO because GL coding
    is unwritten is the pipeline behaving exactly as designed, and recording it
    as an error trains a reader to skim past errors. A transition the table
    refuses for any *other* reason stays an ERROR, because that one is a bug.
    """
