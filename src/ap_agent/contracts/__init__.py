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
    AmbiguousNumber,
    Confidence,
    CostUsd,
    CurrencyCode,
    Money,
    Quantity,
    Sha256Hex,
    StrictModel,
    TaxRate,
    UnitPrice,
    is_allowed_currency,
    normalise_decimal_text,
)
from ap_agent.contracts.enums import (
    ActorKind,
    ArithmeticFlag,
    AuditEventType,
    MatchLineOutcome,
    ReasonCode,
    SuggestedAction,
    SuggestedResolver,
)
from ap_agent.contracts.exceptions import ExceptionClassification
from ap_agent.contracts.generated import (
    GENERATOR_VERSION,
    ExpectedInvoice,
    ExpectedLine,
    ExpectedMatch,
    GeneratedInvoiceTruth,
    GeneratedVariant,
)
from ap_agent.contracts.guardrails import (
    ApprovalMatrix,
    ApprovalRule,
    ApprovalTier,
    FilterAction,
    GuardrailConfig,
    HardProhibitions,
    InputValidation,
    OutputFilter,
    OutputFilterPattern,
    Percentage,
    Tolerances,
)
from ap_agent.contracts.invoice import (
    EVIDENCE_FIELDS,
    EvidenceEntry,
    EvidenceField,
    InvoiceExtraction,
    LineItem,
)
from ap_agent.contracts.matching import MatchLine, MatchResult, SignedPercentage
from ap_agent.contracts.purchase_order import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    ReceiptLine,
    ReceiptSet,
)
from ap_agent.contracts.vendor import (
    MAX_VENDOR_CANDIDATES,
    VendorCandidate,
    VendorMatch,
    VendorMatchBasis,
    VendorRef,
)

__all__ = [
    "CURRENCY_ALLOWLIST",
    "EVIDENCE_FIELDS",
    "GENERATOR_VERSION",
    "GENESIS_HASH",
    "MAX_VENDOR_CANDIDATES",
    "Actor",
    "ActorKind",
    "AmbiguousNumber",
    "ApprovalMatrix",
    "ApprovalRule",
    "ApprovalTier",
    "ArithmeticFlag",
    "AuditEvent",
    "AuditEventType",
    "Confidence",
    "CostUsd",
    "CurrencyCode",
    "EvidenceEntry",
    "EvidenceField",
    "ExceptionClassification",
    "ExpectedInvoice",
    "ExpectedLine",
    "ExpectedMatch",
    "FilterAction",
    "GeneratedInvoiceTruth",
    "GeneratedVariant",
    "GuardrailConfig",
    "HardProhibitions",
    "HumanActor",
    "InputValidation",
    "InvoiceExtraction",
    "LineItem",
    "MatchLine",
    "MatchLineOutcome",
    "MatchResult",
    "ModelActor",
    "Money",
    "OutputFilter",
    "OutputFilterPattern",
    "Percentage",
    "PurchaseOrder",
    "PurchaseOrderLine",
    "PurchaseOrderStatus",
    "Quantity",
    "ReasonCode",
    "ReceiptLine",
    "ReceiptSet",
    "RuleActor",
    "Sha256Hex",
    "SignedPercentage",
    "StrictModel",
    "SuggestedAction",
    "SuggestedResolver",
    "SystemActor",
    "TaxRate",
    "Tolerances",
    "ToolActor",
    "UnitPrice",
    "VendorCandidate",
    "VendorMatch",
    "VendorMatchBasis",
    "VendorRef",
    "is_allowed_currency",
    "normalise_decimal_text",
    "utc_now",
]
