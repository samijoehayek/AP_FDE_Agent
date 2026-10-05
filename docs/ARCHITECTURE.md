# Architecture

Two diagrams and the reasoning behind them. The state diagram is generated from
`src/ap_agent/states/machine.py` by `just docs-diagram`, so it cannot drift from
the table it documents.

## The trust boundary

The single most important line in this system is the one between a document and
the code that acts on it.

```mermaid
flowchart TB
    subgraph untrusted["UNTRUSTED — anything here is attacker-controlled"]
        doc["Invoice document<br/>PDF or scan<br/><i>text, images, and whatever<br/>a vendor chose to print</i>"]
    end

    subgraph boundary["THE BOUNDARY"]
        ingest["ingest_document<br/><i>bytes, hash, page count,<br/>text layer, sharpness</i><br/><b>no interpretation</b>"]
        model["Extraction model<br/><b>NO TOOLS</b><br/><i>document text is data,<br/>never instructions</i>"]
        contract["InvoiceExtraction<br/><b>extra=forbid</b><br/><i>no bank details, no free-form<br/>instruction field, bounded evidence</i>"]
    end

    subgraph trusted["TRUSTED — deterministic, replayable, unit-tested"]
        rules["compute_match<br/><i>pure function of<br/>invoice + PO + receipts + config</i>"]
        guard["Guardrails<br/><i>versioned YAML tolerances<br/>and approval matrix</i>"]
        human["Human approval<br/><i>a durable row, not a callback</i>"]
        erp["create_bill<br/><i>unpaid liability only,<br/>with an idempotency key</i>"]
    end

    subgraph explain["EXPLANATION SEAT"]
        exmodel["classify_exception<br/><i>reads a finished MatchResult,<br/>writes prose for the queue</i>"]
    end

    subgraph absent["DELIBERATELY ABSENT"]
        none["pay_bill · update_vendor<br/>update_bank_details · delete_*<br/><i>no tool exists, so no prompt<br/>can reach one</i>"]
    end

    doc --> ingest --> model --> contract --> rules
    guard --> rules
    rules --> human --> erp
    rules -.-> exmodel -.->|display only| human
    erp -.->|never| none

    style untrusted fill:#4a1520,stroke:#c0392b,color:#f5f5f5
    style boundary fill:#4a3410,stroke:#d68910,color:#f5f5f5
    style trusted fill:#12341c,stroke:#27ae60,color:#f5f5f5
    style explain fill:#1a3550,stroke:#2980b9,color:#f5f5f5
    style absent fill:#2b2b2b,stroke:#7f8c8d,color:#f5f5f5,stroke-dasharray: 5 5
```

Reading it left to right:

**Ingestion does not interpret.** `ingest_document` produces a hash, a page
count, whether there is a text layer, and a per-page sharpness score. It never
reads a value off the page. A document that fails here fails before any model
has seen it.

**The extraction model has no tools.** This is the load-bearing control. Prompt
injection is not a solved problem, and it does not need to be solved if the
model that reads attacker-controlled text cannot cause an effect. Its entire
output surface is one `InvoiceExtraction`, validated on the way out. Text in the
document that looks like an instruction goes into `suspicious_text` as evidence
and is never re-injected as an instruction.

**The contract is the filter, not the prompt.** `extra="forbid"` means a field
that is not declared does not survive validation. There is no bank-details field
anywhere in the contract tree — a test walks the package and fails if one
appears — so the highest-value target in AP fraud has nowhere to land.
`remit_to_display` keeps what the document claimed, for a human to look at, and
no payment path reads it.

**Money decisions are code.** `compute_match` is a pure function of the
extraction, the PO, the receipts, and a versioned config. It is replayable, and
its output names the `config_version` that produced it, so a decision made last
quarter can be re-explained under last quarter's rules.

**The explanation seat is downstream of the decision.** By the time a model
writes an exception summary, the reason codes are already fixed. It chooses
which one to lead with and writes prose for the approver. Nothing it produces is
read by a rule.

**The pipeline stops at an unpaid bill.** `create_bill` records a liability.
`mark_ready_for_payment` sets a flag. Payment itself happens in a separate
treasury process with its own controls. That is the deliberate end of this
system's authority.

## The state machine

Every legal move is a key in `TRANSITIONS`. There is no fallback branch and no
way to reach a state by setting a column — `transition()` raises
`IllegalTransition` for anything not in the table.

The invariant worth stating explicitly: **no path reaches `POSTED` without
passing through `APPROVED`**. It is asserted as a graph search
(`test_no_path_reaches_posted_without_passing_through_approved`) rather than
edge by edge, so it survives edges added later by someone who has not read this
document. The same search covers everything downstream — `SCHEDULED`, `PAID`,
`RECONCILED`, `CLOSED`.

Cancellation stops at the gate. Once an invoice is `APPROVED` there is a
liability in the ledger, and unwinding it is an accounting action with its own
trail — not a state this pipeline may quietly rewrite.

<!-- BEGIN GENERATED STATE DIAGRAM -->

```mermaid
stateDiagram-v2
    [*] --> RECEIVED
    APPROVED --> POSTED: post
    APPROVED --> EXCEPTION: post_failed
    APPROVED --> POSTED: stub_ok
    CODED --> CANCELLED: cancel
    CODED --> PENDING_APPROVAL: request_approval
    CODED --> PENDING_APPROVAL: stub_ok
    DUPLICATE_CHECKED --> CANCELLED: cancel
    DUPLICATE_CHECKED --> MATCHED: match
    DUPLICATE_CHECKED --> EXCEPTION: match_exception
    DUPLICATE_CHECKED --> NON_PO: no_po_reference
    EXCEPTION --> PENDING_HUMAN: classify
    EXTRACTED --> CANCELLED: cancel
    EXTRACTED --> VALIDATED: validate
    EXTRACTED --> NEEDS_HUMAN_EXTRACTION: validation_failed
    INGESTED --> CANCELLED: cancel
    INGESTED --> EXTRACTED: extract
    INGESTED --> NEEDS_HUMAN_EXTRACTION: extraction_failed
    INGESTED --> NEEDS_HUMAN_EXTRACTION: output_flagged
    MATCHED --> CANCELLED: cancel
    MATCHED --> CODED: code
    MATCHED --> CODED: stub_ok
    NEEDS_HUMAN_EXTRACTION --> CANCELLED: cancel
    NEEDS_HUMAN_EXTRACTION --> EXTRACTED: human_extraction_provided
    NEW_VENDOR --> CANCELLED: cancel
    NEW_VENDOR --> REJECTED: reject
    NEW_VENDOR --> VENDOR_RESOLVED: vendor_onboarded
    NON_PO --> CANCELLED: cancel
    NON_PO --> CODED: code
    NON_PO --> REJECTED: reject
    NON_PO --> CODED: stub_ok
    ON_HOLD_DUPLICATE --> CANCELLED: cancel
    ON_HOLD_DUPLICATE --> REJECTED: confirm_duplicate
    ON_HOLD_DUPLICATE --> DUPLICATE_CHECKED: duplicate_cleared
    PAID --> RECONCILED: reconcile
    PAID --> RECONCILED: stub_ok
    PENDING_APPROVAL --> APPROVED: approve
    PENDING_APPROVAL --> CANCELLED: cancel
    PENDING_APPROVAL --> REJECTED: reject
    PENDING_APPROVAL --> EXCEPTION: request_changes
    PENDING_APPROVAL --> APPROVED: stub_ok
    PENDING_HUMAN --> MATCHED: accept_with_reason
    PENDING_HUMAN --> CANCELLED: cancel
    PENDING_HUMAN --> REJECTED: reject
    PENDING_HUMAN --> DUPLICATE_CHECKED: rematch
    POSTED --> SCHEDULED: schedule_payment
    POSTED --> SCHEDULED: stub_ok
    RECEIVED --> CANCELLED: cancel
    RECEIVED --> INGESTED: ingest
    RECEIVED --> NEEDS_HUMAN_EXTRACTION: input_flagged
    RECONCILED --> CLOSED: close
    RECONCILED --> CLOSED: stub_ok
    SCHEDULED --> PAID: payment_confirmed
    SCHEDULED --> POSTED: payment_failed
    SCHEDULED --> PAID: stub_ok
    VALIDATED --> CANCELLED: cancel
    VALIDATED --> NEW_VENDOR: remit_to_mismatch
    VALIDATED --> VENDOR_RESOLVED: resolve_vendor
    VALIDATED --> NEW_VENDOR: vendor_not_found
    VENDOR_RESOLVED --> CANCELLED: cancel
    VENDOR_RESOLVED --> DUPLICATE_CHECKED: check_duplicates
    VENDOR_RESOLVED --> ON_HOLD_DUPLICATE: duplicate_suspected
    VENDOR_RESOLVED --> DUPLICATE_CHECKED: stub_ok
    CANCELLED --> [*]
    CLOSED --> [*]
    REJECTED --> [*]
```

<!-- END GENERATED STATE DIAGRAM -->

### The paths through it

| Path | Route |
| --- | --- |
| Straight through | `RECEIVED → INGESTED → EXTRACTED → VALIDATED → VENDOR_RESOLVED → DUPLICATE_CHECKED → MATCHED → CODED → PENDING_APPROVAL → APPROVED → POSTED → SCHEDULED → PAID → RECONCILED → CLOSED` |
| Unreadable document | `INGESTED → NEEDS_HUMAN_EXTRACTION → EXTRACTED` |
| Unknown vendor | `VALIDATED → NEW_VENDOR → VENDOR_RESOLVED`, or `→ REJECTED` |
| Possible duplicate | `VENDOR_RESOLVED → ON_HOLD_DUPLICATE → DUPLICATE_CHECKED`, or `→ REJECTED` |
| Match exception | `DUPLICATE_CHECKED → EXCEPTION → MATCHED → CODED → …` |
| No PO | `DUPLICATE_CHECKED → NON_PO → CODED → …` |
| Approver sends it back | `PENDING_APPROVAL → EXCEPTION → MATCHED → CODED → PENDING_APPROVAL` |
| Payment fails | `SCHEDULED → POSTED → SCHEDULED` |

## Persistence

| Table | Holds | Note |
| --- | --- | --- |
| `documents` | One row per distinct file, keyed by SHA-256 | Identity is the bytes |
| `invoices` | Current state and the extracted header | Unique `(vendor_id, invoice_number)` — the duplicate control the database enforces |
| `audit_events` | The hash-chained trail | Append-only at the schema level. The application role holds `SELECT` and `INSERT` and nothing else — and it is an ordinary role, not the owner, because a superuser bypasses the check |
| `approval_requests` | Durable human-in-the-loop approvals | A four-day approval must survive a deploy |
| `erp_writes` | The idempotency ledger | A key is claimed before the call goes out |

## Audit trail

Each event stores the hash of the one before it. That does not stop someone with
database access from rewriting history — it makes rewritten history *detectable*,
which is the honest claim for a single-database design. Making it tamper-*proof*
needs an external anchor: publishing the chain head daily to a store the database
administrator does not control.

`step_seq`, not the timestamp, defines order. Clocks are not trustworthy and two
events can share a millisecond.

## What is not built yet

The agent loop, the extraction prompts, the matching logic, the guardrail
evaluation, and the audit hash chain. Each is a `NotImplementedError` under a
docstring stating what it will do and the constraints it must satisfy — see
`docs/DECISIONS.md` and the module docstrings.
