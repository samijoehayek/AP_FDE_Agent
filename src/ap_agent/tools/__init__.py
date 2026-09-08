"""One module per tool. What is missing from this list is part of the design.

There is no ``pay_bill``, no ``update_vendor``, no ``update_bank_details``, and
no ``delete_*``. Their absence is a control, and it is asserted by
``tests/tools/test_tool_contracts.py`` so that adding one is a test failure and
a conversation, not a quiet commit.

The reasoning: an agent's blast radius is exactly the set of tools it can reach.
Prompts, guardrails, and approvals all reduce the *probability* of a bad call;
only the absence of a tool reduces its *possibility* to zero. So the three
actions that turn an AP mistake into an unrecoverable loss - moving money,
changing where money goes, and destroying the record of either - are not
implemented here at all. Payment is a separate treasury process; vendor master
changes are a separate human workflow with their own approvals.

Each module declares ``CALLER``, ``SIDE_EFFECTS``, and
``REQUIRES_IDEMPOTENCY_KEY`` at module level. See ``ap_agent.tools.base``.
"""

from __future__ import annotations

from ap_agent.tools.base import (
    IdempotentToolInput,
    SideEffect,
    ToolCaller,
    ToolInput,
    ToolOutput,
)
from ap_agent.tools.classify_exception import (
    ClassifyExceptionInput,
    ClassifyExceptionOutput,
    classify_exception,
)
from ap_agent.tools.compute_extraction_confidence import (
    ComputeExtractionConfidenceInput,
    ComputeExtractionConfidenceOutput,
    compute_extraction_confidence,
)
from ap_agent.tools.compute_match import ComputeMatchInput, ComputeMatchOutput, compute_match
from ap_agent.tools.create_bill import CreateBillInput, CreateBillOutput, create_bill
from ap_agent.tools.extract_invoice_text import (
    ExtractInvoiceTextInput,
    ExtractInvoiceTextOutput,
    extract_invoice_text,
)
from ap_agent.tools.extract_invoice_vision import (
    ExtractInvoiceVisionInput,
    ExtractInvoiceVisionOutput,
    extract_invoice_vision,
)
from ap_agent.tools.find_duplicates import (
    DuplicateCandidate,
    FindDuplicatesInput,
    FindDuplicatesOutput,
    find_duplicates,
)
from ap_agent.tools.get_purchase_order import (
    GetPurchaseOrderInput,
    GetPurchaseOrderOutput,
    get_purchase_order,
)
from ap_agent.tools.get_receipts import GetReceiptsInput, GetReceiptsOutput, get_receipts
from ap_agent.tools.ingest_document import (
    IngestDocumentInput,
    IngestDocumentOutput,
    ingest_document,
)
from ap_agent.tools.lookup_vendor import LookupVendorInput, LookupVendorOutput, lookup_vendor
from ap_agent.tools.mark_ready_for_payment import (
    MarkReadyForPaymentInput,
    MarkReadyForPaymentOutput,
    mark_ready_for_payment,
)
from ap_agent.tools.notify import NotifyInput, NotifyOutput, notify
from ap_agent.tools.propose_gl_coding import (
    ProposedCoding,
    ProposeGlCodingInput,
    ProposeGlCodingOutput,
    propose_gl_coding,
)
from ap_agent.tools.request_approval import (
    RequestApprovalInput,
    RequestApprovalOutput,
    request_approval,
)
from ap_agent.tools.verify_vendor_external import (
    VerifyVendorExternalInput,
    VerifyVendorExternalOutput,
    verify_vendor_external,
)

TOOL_MODULE_NAMES: tuple[str, ...] = (
    "ingest_document",
    "extract_invoice_vision",
    "extract_invoice_text",
    "compute_extraction_confidence",
    "lookup_vendor",
    "get_purchase_order",
    "get_receipts",
    "find_duplicates",
    "compute_match",
    "classify_exception",
    "propose_gl_coding",
    "verify_vendor_external",
    "create_bill",
    "request_approval",
    "mark_ready_for_payment",
    "notify",
)
"""Every tool module, in pipeline order. The test suite walks this."""

FORBIDDEN_TOOL_NAMES: frozenset[str] = frozenset(
    {"pay_bill", "update_vendor", "update_bank_details", "delete_invoice", "delete_vendor"}
)
"""Names that must never appear as a module in this package.

Asserted by a test, and any module starting with ``delete_`` is rejected too.
"""

__all__ = [
    "FORBIDDEN_TOOL_NAMES",
    "TOOL_MODULE_NAMES",
    "ClassifyExceptionInput",
    "ClassifyExceptionOutput",
    "ComputeExtractionConfidenceInput",
    "ComputeExtractionConfidenceOutput",
    "ComputeMatchInput",
    "ComputeMatchOutput",
    "CreateBillInput",
    "CreateBillOutput",
    "DuplicateCandidate",
    "ExtractInvoiceTextInput",
    "ExtractInvoiceTextOutput",
    "ExtractInvoiceVisionInput",
    "ExtractInvoiceVisionOutput",
    "FindDuplicatesInput",
    "FindDuplicatesOutput",
    "GetPurchaseOrderInput",
    "GetPurchaseOrderOutput",
    "GetReceiptsInput",
    "GetReceiptsOutput",
    "IdempotentToolInput",
    "IngestDocumentInput",
    "IngestDocumentOutput",
    "LookupVendorInput",
    "LookupVendorOutput",
    "MarkReadyForPaymentInput",
    "MarkReadyForPaymentOutput",
    "NotifyInput",
    "NotifyOutput",
    "ProposeGlCodingInput",
    "ProposeGlCodingOutput",
    "ProposedCoding",
    "RequestApprovalInput",
    "RequestApprovalOutput",
    "SideEffect",
    "ToolCaller",
    "ToolInput",
    "ToolOutput",
    "VerifyVendorExternalInput",
    "VerifyVendorExternalOutput",
    "classify_exception",
    "compute_extraction_confidence",
    "compute_match",
    "create_bill",
    "extract_invoice_text",
    "extract_invoice_vision",
    "find_duplicates",
    "get_purchase_order",
    "get_receipts",
    "ingest_document",
    "lookup_vendor",
    "mark_ready_for_payment",
    "notify",
    "propose_gl_coding",
    "request_approval",
    "verify_vendor_external",
]
