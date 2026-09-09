"""Typed contracts: the single source of truth for every schema in the system.

If a shape is not declared here it does not cross a boundary. Tool inputs and
outputs, model responses, database rows and the audit trail all derive from
these models, so there is exactly one place to look for what a field means and
exactly one place a reviewer has to read to know what a model is allowed to say.

Every model inherits :class:`~ap_agent.contracts.common.StrictModel`, which sets
``extra="forbid"``. A field that is not declared is rejected, loudly, at the
boundary - which is the mechanism that keeps unmodelled data (remittance
details, free-form instructions) from riding into trusted code on a payload.
"""

from __future__ import annotations

from ap_agent.contracts.audit import (
    GENESIS_HASH,
    Actor,
    AuditEvent,
    HumanActor,
    ModelActor,
    RuleActor,
    SystemActor,
    ToolActor,
    utc_now,
)
from ap_agent.contracts.common import (
    CURRENCY_ALLOWLIST,
    Confidence,
    CurrencyCode,
    Money,
    Quantity,
    Sha256Hex,
    StrictModel,
    TaxRate,
    UnitPrice,
    is_allowed_currency,
)
from ap_agent.contracts.enums import (
    ActorKind,
    ArithmeticFlag,
    AuditEventType,
    MatchLineStatus,
    MatchTotalsStatus,
    ReasonCode,
    SuggestedAction,
    SuggestedResolver,
)
from ap_agent.contracts.exceptions import ExceptionClassification
from ap_agent.contracts.invoice import (
    EVIDENCE_FIELDS,
    ExtractionEvidence,
    FieldEvidence,
    InvoiceExtraction,
    LineItem,
)
from ap_agent.contracts.matching import MatchLineResult, MatchResult
from ap_agent.contracts.purchase_order import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from ap_agent.contracts.vendor import VendorRef

__all__ = [
    "CURRENCY_ALLOWLIST",
    "EVIDENCE_FIELDS",
    "GENESIS_HASH",
    "Actor",
    "ActorKind",
    "ArithmeticFlag",
    "AuditEvent",
    "AuditEventType",
    "Confidence",
    "CurrencyCode",
    "ExceptionClassification",
    "ExtractionEvidence",
    "FieldEvidence",
    "GoodsReceipt",
    "GoodsReceiptLine",
    "HumanActor",
    "InvoiceExtraction",
    "LineItem",
    "MatchLineResult",
    "MatchLineStatus",
    "MatchResult",
    "MatchTotalsStatus",
    "ModelActor",
    "Money",
    "PurchaseOrder",
    "PurchaseOrderLine",
    "PurchaseOrderStatus",
    "Quantity",
    "ReasonCode",
    "RuleActor",
    "Sha256Hex",
    "StrictModel",
    "SuggestedAction",
    "SuggestedResolver",
    "SystemActor",
    "TaxRate",
    "ToolActor",
    "UnitPrice",
    "VendorRef",
    "is_allowed_currency",
    "utc_now",
]
