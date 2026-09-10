# AP invoice agent: the architecture one level down, and the stack that real teams use to build it

_Researched 8 September 2026 · Assumes a solo builder on a 7-day curriculum, real small-business invoices, a QuickBooks Online / Xero-class accounting system, and Python as the likely but not fixed language · Confidence: high on the architecture and the fraud/control facts (primary sources), medium-high on the tooling landscape (fast-moving; verified as of this week), lower on any vendor-published accuracy number (flagged where used)._

---

## Bottom line

Your mapping is sound. The one correction that reorganizes everything else is this: **AP invoice processing is a workflow with an LLM in two specific seats, not an autonomous agent that decides what to do next.** The states an invoice moves through are fixed and known in advance; the tolerances are policy; the approval matrix is policy; three-way matching is arithmetic. The model earns its place in exactly the places where the input is unstructured and judgment is needed — reading the document, resolving a messy vendor name, classifying and explaining an exception, drafting the note to a human — and it earns nothing in the places where the answer is deterministic. Anthropic's own guidance draws precisely this line (workflows are LLM-and-tool calls orchestrated by predefined code; agents are systems where the model directs its own path), and recommends starting with the simplest pattern that passes evaluation. Money-moving processes are the canonical case for the workflow side of that line.

That doesn't break your curriculum. Day 1's "agent loop" is still a loop — _given this invoice's current state, what is the next action_ — but the loop iterates over a state machine you define, and the model is consulted inside certain states rather than choosing which tool to call from an open menu. This is also what makes the guardrails, audit trail, and memory designs tractable: a state machine has a finite set of transitions to log, gate, and persist.

Three findings from the research should shape the build before you write a line:

1. **The invoice is untrusted input.** Documented attacks embed hidden instructions in invoice PDFs to redirect payments, and researchers have shown an OCR/extraction agent with database write access being turned against its own records by a forged document. The defense is architectural: the extraction call has no tools; extracted text is data, never instructions; bank details are _never_ read from the invoice, only from your vendor master; and the decision to pay is computed by code from ERP data, not narrated by the model.

2. **Extraction is good but not "99%", and the model cannot tell you when it's wrong.** A June 2026 paper on a 55-field real-invoice benchmark found a frontier vision model failed on roughly a quarter of fields, and that every cheap confidence signal — token log-probabilities, the model's self-reported confidence, and even five-sample self-consistency — collapsed toward "everything looks fine" at usable thresholds. What _did_ work: reading the document twice in structurally different ways and treating disagreement as the signal, plus checking whether the extracted value literally exists in the document's raw text. You can implement a cheap version of this on day 2.

3. **The fraud you're defending against is mostly social, not technical.** The AFP's April 2026 survey (400+ organizations) found 76% experienced attempted or actual payments fraud in 2025, 74% were hit by business email compromise, and the FBI logged $3.05B in reported BEC losses for 2025. The dominant AP variant is a "vendor" asking to update bank details. So the single most important guardrail in your design is not a dollar threshold — it's that the agent structurally cannot change or use bank details, and any new/changed banking triggers an out-of-band human callback.

On the stack: yes, it's Python, and yes, there are libraries you may not know. The honest layer cake for a solo builder in September 2026 is: the `anthropic` SDK with GA structured outputs (JSON schema / Pydantic-validated) for the model calls; Claude's native PDF input as the primary reader with a text-layer parse as the second reading; a hand-written loop for days 1–3, graduating to **Pydantic AI** (type-safe, OTel built in, durable execution via Temporal/DBOS/Prefect/Restate) or **LangGraph** (explicit state graph, checkpointing, `interrupt()` for human approval) on day 4; **DBOS** as the lightest durable-execution layer (in-process, checkpoints to Postgres, no separate server); the official **Intuit QuickBooks Online MCP server** or **Xero MCP server** behind a narrow allowlist for ERP access; **OpenTelemetry `gen_ai.*` spans** shipped to self-hosted **Langfuse** or **Arize Phoenix** for observability; and — separately — an **append-only audit table in Postgres** that is the actual audit trail. Evals via **pytest + DeepEval** (or promptfoo) against a golden set of 20–50 real invoices, run in CI.

The rest of this document goes one level deeper on each of your six questions, then explains the stack layer by layer with what the best teams do and why.

---

## How this actually works: the AP control model, in the vocabulary practitioners use

Before the states, the mental model. Accounts payable exists to answer three separate questions before money leaves: **did we authorize this** (purchase order), **did we receive it** (goods receipt / receiving report / service confirmation), and **is the supplier billing what we agreed** (invoice). No single document proves all three; that's the whole logic of the three-way match. When all three agree within pre-defined tolerances, the invoice is "clean" and can flow "touchless" — no human touches it between receipt and payment approval. When they don't, it becomes an **exception** and gets routed to whoever can resolve it: the buyer for price variances, receiving for quantity/missing-receipt issues, the vendor for billing errors.

Vocabulary you'll need for reading further sources: **two-way match** (PO ↔ invoice, no receipt; used for services and subscriptions), **three-way match** (adds the receipt), **non-PO invoice** (spend that was never on a PO — rent, utilities, many services — which needs GL coding and an approver instead of a match), **GRN** (goods receipt note), **vendor master** (the authoritative record of each supplier including bank details and tax ID), **tolerances** (the variance you'll accept without a human), **exception rate** and **touchless rate** (the two KPIs everyone tracks), **no-PO-no-pay** (the policy that forces every invoice to have a PO to match against), **approval matrix** (who must sign off at what dollar level), **payment run** (the batch in which approved bills are actually paid), **recovery audit** (the firm you hire afterward to find the duplicate payments you missed — they work on contingency, which tells you how common misses are).

Why the industry looks like this: AP automation vendors (Stampli, Tipalti, Bill.com, Vic.ai, Ramp, plus the ERP-native modules) sell the same pipeline you're building — capture, match, route, pay — and the pilots consistently reach about 60% touchless and then stall. The remaining 30–40% is where the real work is: duplicate vendor records, contract price escalations, partial deliveries, period-end cutoffs, cross-border tax. One useful 2026 pilot guide argues the best possible audit trail for a variance approval looks like _"approved per section 4.2 of MSA-2024-127, which permits a 4% annual escalation"_ — i.e., a retrievable citation to a rule, not a model's paragraph of reasoning. Hold that standard for your log design.

The practitioner consensus on tolerances is that zero tolerance grinds everything to a halt, and that exception rates above roughly 20–25% usually mean either the thresholds are too tight or the upstream data (PO accuracy, receipt timeliness) is bad. Automated matching typically yields 40–70% touchless on PO-backed invoices, and the gap between a mediocre and a good match rate is almost always data quality, not engine sophistication. This matters for your Day 6: if your real invoice stream has no POs (very common for a small business), most of your invoices will be non-PO, and the interesting control is the approval matrix plus anomaly detection against vendor history, not the match.

---

## Part 1 — The architecture, one level deeper

### 1. The agent loop: states, and who decides at each

The loop is `while invoice.state not in TERMINAL: next_action = decide(invoice.state, context); apply(next_action); log(...)`. What makes it honest as a curriculum exercise is that `decide` is a real decision every cycle — but the decision-maker differs by state: **code** (deterministic rules), **model** (LLM, proposing), or **human** (the workflow pauses). Design each transition knowing which of the three owns it.

**RECEIVED → INGESTED.** _Decider: code._ A file arrives (email attachment, upload, portal). Compute a SHA-256 of the bytes, check MIME type and page count, detect whether the PDF has a text layer or is a scanned image, reject encrypted/corrupt files. Decision: is this a processable document, and have we seen these exact bytes before? Exact-hash duplicates die here, before spending any tokens.

**INGESTED → EXTRACTED.** _Decider: model, then code._ Two readings of the document produce a structured `InvoiceExtraction` (header fields, line items, per-field evidence pointers). Code compares the two readings and computes field-level confidence. Decision: are the load-bearing fields (vendor identity, invoice number, total, currency, date) agreed and grounded in the document text? If not → `NEEDS_HUMAN_EXTRACTION` (a human confirms or corrects fields; corrections are logged and become eval data).

**EXTRACTED → VALIDATED.** _Decider: code._ Schema and arithmetic: required fields present, line items sum to subtotal within a cent-level tolerance, subtotal + tax = total, dates are sane, currency is allowed, amount is positive (or it's a credit note and goes down a different path). Decision: is this internally consistent? Failure here is almost always an extraction error or a genuinely malformed invoice; either way a human should see it.

**VALIDATED → VENDOR_RESOLVED / NEW_VENDOR.** _Decider: code first, model for the ambiguous middle, human for new._ Match the extracted vendor to the vendor master by tax ID, then by normalized name, then by email domain/IBAN as tiebreakers. An exact tax-ID hit is code. A fuzzy name hit with several candidates is where the model helps ("these three master records; which, if any, is this invoice from, and why?") — but its answer is a proposal that code checks against hard signals (does the IBAN on file match? does the remit-to address match?). No confident match → `NEW_VENDOR`, which is always a human decision and where the optional web check (Day 2's second tool) lives: registry lookup, domain age, does the invoice's email domain match the company's real domain or a lookalike one letter off.

**VENDOR_RESOLVED → DUPLICATE_CHECKED.** _Decider: code._ Fuzzy duplicate search against history (see edge cases for the rule). Decision: likely duplicate → `ON_HOLD_DUPLICATE` for human confirmation; otherwise proceed. This must happen before any approval, never after payment.

**DUPLICATE_CHECKED → MATCHED / EXCEPTION / NON_PO.** _Decider: code._ If the invoice references a PO: fetch PO and receipts, run the match line by line (quantity, unit price, extended price), then totals (freight, tax, discounts). Everything within tolerance → `MATCHED`. Outside tolerance → `EXCEPTION` with a specific reason code. No PO reference → look for an open PO for this vendor with matching amounts (a common "vendor forgot the PO number" case); if none, `NON_PO`.

**EXCEPTION → (classified) → PENDING_HUMAN.** _Decider: model proposes, human disposes._ This is the second seat where the model belongs: given the invoice, PO, receipts, and the specific mismatch, classify the exception (price variance, quantity over-billed, freight not on PO, partial delivery, receipt missing, tax mismatch, wrong entity), name the likely resolver, and draft a plain-language note with the numbers. The model never resolves; it makes the human's job take 90 seconds instead of 15 minutes. Exceptions age; an exception older than N business days escalates automatically (see memory).

**NON_PO → CODED → PENDING_APPROVAL.** _Decider: model proposes GL code and department from vendor history and line descriptions; code checks it against the vendor's historical coding; human approves._ For a small business this branch may carry most volume.

**MATCHED → APPROVED (auto) or → PENDING_APPROVAL.** _Decider: code applies the approval matrix._ Below the auto-approve threshold and clean → `APPROVED` with the rule ID recorded. Otherwise route to the approver(s) the matrix specifies. The workflow _pauses_ here — this is the human-in-the-loop interrupt that your Day 4 memory design must survive, because approvals take hours to days.

**APPROVED → POSTED.** _Decider: code._ Create the bill in the accounting system with an idempotency key (so a retry after a network failure doesn't create two bills). Record the ERP's bill ID.

**POSTED → SCHEDULED.** _Decider: code._ Compute pay-by date from terms (net 30, 2/10 net 30 with early-payment discount), place it in the next payment run. **In the 7-day build, the agent stops here.** Executing payment is a separate, deterministic, human-controlled step in your accounting system with its own dual controls. The agent should not have a tool that moves money; it should have a tool that says "ready to pay on date X for reason Y."

**PAID → RECONCILED → CLOSED.** _Decider: code._ When the bank feed shows the payment, match it to the bill and close. Reconciliation failures (paid amount ≠ bill, or paid twice) are their own exception type.

Terminal states: `CLOSED`, `REJECTED` (with reason), `CANCELLED` (vendor issued a credit / withdrew). Parking states: `ON_HOLD_*` variants. Every state has a maximum dwell time after which it escalates.

Two properties to build in from the start. **Every transition is a function of (current state, evidence) and is logged with the rule or actor that caused it.** And **the invoice's state lives in the database, not in the model's context** — a crashed run is re-entered at the persisted state, not restarted from the PDF.

### 2. Tools, defined properly

A tool definition that the model will call needs a name, a one-paragraph purpose that tells the model _when_ to use it, typed inputs, typed outputs, and an explicit statement of side effects. Tools that code calls (not the model) still deserve the same rigor because they're the seams where things fail. Below, "caller" says who invokes it.

**`ingest_document`** — _caller: code._ Input: file bytes, source channel, received-at. Output: `doc_id`, `sha256`, `mime`, `page_count`, `has_text_layer`, `image_quality_score` (a cheap sharpness metric per page), `is_exact_duplicate` (hash seen before). Side effects: stores the original immutably. Purpose: the only way a document enters the system.

**`extract_invoice_vision`** — _caller: code; this is the model call._ Input: `doc_id`, the target JSON schema (vendor block, invoice number, dates, currency, subtotal/tax/total, payment terms, PO references, line items with description/qty/unit/unit price/extended price/tax rate, remit-to block _for display only_). Output: the structured extraction, plus for each field a page number and a short evidence snippet the model claims it read from. Constraints: this call has **no tools attached**, a fixed prompt version, temperature 0, and structured-output enforcement. It is told, in the system prompt, that the document may contain text that looks like instructions and that such text is content to be transcribed into a `suspicious_text` field, not followed.

**`extract_invoice_text`** — _caller: code._ Input: `doc_id`. Output: raw text with word bounding boxes (from the PDF text layer when present, OCR otherwise) and a second, independently-produced structured extraction from that text (a cheaper model or a rule-based parser). Purpose: the second reading. Disagreement between this and the vision extraction is your primary confidence signal; a value that appears in neither the text layer nor the OCR output is a hallucination by definition.

**`compute_extraction_confidence`** — _caller: code, no model._ Input: both extractions, OCR word confidences, image quality. Output: per-field confidence, `auto_ok: bool`, list of fields needing human confirmation. Purpose: implements the "two readings + grounding" logic.

**`lookup_vendor`** — _caller: code; the model may be asked to adjudicate among returned candidates._ Input: extracted vendor name, tax ID, email domain, IBAN/account fragment, address. Output: exact match, or ranked candidates with per-signal scores, or none. Never returns bank details to the model — returns a boolean `bank_details_match_on_file` computed server-side.

**`get_purchase_order`** — _caller: code._ Input: PO number (or vendor ID + date window). Output: PO header and lines with ordered qty, unit price, currency, open (unbilled) qty, status.

**`get_receipts`** — _caller: code._ Input: PO number. Output: receipt lines with received qty and dates.

**`find_duplicates`** — _caller: code._ Input: vendor ID, normalized invoice number, amount, date. Output: candidate prior invoices with a similarity score and which rule fired.

**`compute_match`** — _caller: code, no model._ Input: extraction, PO, receipts, the tolerance config. Output: `MatchResult` with per-line status (ok / price variance / qty variance / not on PO), totals status, and a list of reason codes. This is the heart of AP and must be boringly deterministic and unit-tested.

**`classify_exception`** — _caller: code; this is the model call._ Input: extraction, PO/receipt snapshot, `MatchResult`, vendor history summary. Output: structured — `reason_code` from a closed enum, `suggested_resolver` from a closed enum, `human_summary` (≤120 words), `suggested_action` from a closed enum. Purpose: make the exception legible. Its output never changes state by itself.

**`propose_gl_coding`** — _caller: code; model call._ Input: line descriptions, vendor history (top GL codes and departments for this vendor), chart of accounts (allowed codes only). Output: per-line GL code + department + confidence. Constrained to the provided chart of accounts.

**`verify_vendor_external`** — _caller: code, optionally triggered by `NEW_VENDOR` or a bank-change event._ Input: legal name, country, website/email domain. Output: registry hit(s), domain registration age, whether email domain matches the registered site, lookalike-domain distance to known vendors. This is your Day 2 web search. The output is evidence for a human, not a decision.

**`create_bill`** — _caller: code, only from `APPROVED`._ Input: vendor ID, invoice number, date, due date, lines with GL codes, attachment `doc_id`, **idempotency key = (vendor_id, normalized_invoice_number)**. Output: ERP bill ID. Side effect: writes to the ledger. This tool is the first with real-world consequences, and it is only callable by code from one state.

**`request_approval`** — _caller: code._ Input: bill summary, required approvers per matrix, deadline. Output: approval request ID. Side effect: notifies humans; the workflow suspends until a signal arrives (approve / reject / request-info).

**`mark_ready_for_payment`** — _caller: code, only from `POSTED`._ Input: bill ID, proposed pay date, discount captured (if any). Output: confirmation. Deliberately _not_ a "pay" tool.

**`append_audit_event`** — _caller: every other tool, via a wrapper._ Not a tool the model knows about. See section 5.

**`notify`** — _caller: code._ Templated messages only. Free text from the model may be included as a clearly delimited "AI summary" block, never as the instruction to the human.

Note what's absent: `update_vendor`, `update_bank_details`, `pay_bill`, `delete_*`, and `send_email_to_vendor` with free text. Their absence is the guardrail.

### 3. Guardrails with concrete numbers for a small-to-mid business

Treat these as a config file, versioned, with every change logged — because the audit question "why was this auto-approved" must be answerable with "rule R-07 as it read on that date."

**Input validation (before any model call).** Accept PDF, PNG, JPEG, TIFF; reject anything else. ≤ 25 MB, ≤ 20 pages (a 20+ page "invoice" is a statement, a contract, or an attack — route to human). Reject password-protected or malformed files. Reject exact-hash duplicates. Flag files with a text layer whose rendered page has near-zero visible text (hidden-text signal) and files with text in white/near-white or outside the page box.

**Extraction acceptance.** Auto-accept a field only if the two readings agree (exact for numbers/IDs, ≥ 0.9 similarity for names/addresses) _and_ the value is present in the raw text/OCR output. Require agreement on: vendor identity, invoice number, total, currency, invoice date. Anything else can be human-confirmed at approval time. Maximum 2 extraction retries (a retry with a different prompt phrasing, not the same one); after that, human.

**Arithmetic and sanity.** Line items must sum to subtotal within max(0.02, 0.1%) — the small tolerance covers rounding of per-line tax. Subtotal + tax − discounts = total within 0.01. Invoice date not in the future and not older than 180 days (older is a stale resend or a fraud probe). Due date ≥ invoice date. Total > 0 (else credit-note path). Tax rate must be in the allowed set for the vendor's jurisdiction. Currency must be in an allowlist (the one or two you actually trade in).

**Match tolerances (per line, then totals).** Unit price: within ±2% _and_ ≤ $50 absolute per line, whichever binds first. Quantity: invoice qty ≤ received qty for countable goods (never pay for more than received — 0% over-billing tolerance); allow up to 5% over-delivery for bulk/weighed goods if the receipt records it. Unmatched charges (freight, handling): ≤ $50 or ≤ 2% of PO value if not on the PO; above that, exception. Total variance from PO: ≤ $10 after all line-level checks (catches rounding, catches nothing else). Services on two-way match: invoice ≤ PO remaining balance, else exception. Non-PO invoices: compared to the vendor's trailing-12-month median; > 2× median or > $2,500 absolute triggers a "review the amount" flag even when the approver is the same.

**Approval matrix (illustrative; tune to the business).** PO-matched and clean: < $1,000 auto-approve; $1,000–$10,000 one approver (the PO owner or department head); > $10,000 two approvers including the controller/owner. Non-PO: always at least one human, with the same tiers for a second signature. New vendor's first invoice: always two humans regardless of amount, and the vendor must be set up (with bank details verified) before the bill is created. Any invoice from a vendor whose bank details changed in the last 30 days: hold for callback verification regardless of amount.

**Hard prohibitions (not thresholds — the tool doesn't exist).** The agent cannot create or modify vendor records; cannot read bank details into model context; cannot execute payments; cannot delete or void anything; cannot send free-text messages to vendors; cannot approve its own exceptions. Bank details on the invoice's remit-to block are extracted _only_ to compare (server-side) against the vendor master; a mismatch is a fraud flag, never an update.

**Loop and budget limits.** Max 15 loop iterations per invoice run (a clean invoice needs about 8). Max 3 model calls per state. Max 2 re-match attempts after a data refresh (e.g., a receipt was posted). Per-invoice token/cost budget (a few cents on a mid-tier model; abort at 10×). Per-run wall clock ≤ 5 minutes of active processing; waits for humans don't count. Global circuit breaker: if > 30% of invoices in a rolling hour go to exception, pause auto-approval and page a human — that's the signature of a bad model update, a changed vendor template, or an attack.

**Output filtering (the model's outputs are checked before they are used).** Structured outputs are validated against the schema; enum fields must be in the enum; any string field containing an IBAN/account-number pattern, a URL, or imperative phrases like "transfer", "update the account", "ignore" is flagged and the run goes to human. The model's free-text summary is displayed to humans under an "AI-generated" label and is never parsed by code to make decisions.

**Injection defense, summarized.** The model that reads documents has no tools. The model that classifies exceptions gets only structured fields, not raw document text. Decisions are made by code from ERP-sourced data. Instruction-like text found in a document is itself a fraud indicator and is logged.

### 4. Context and memory: what lives where, and why

The distinction you drew (context window vs. external memory) is right; the refinement is that for AP, "external memory" is not a vector store or a "what the agent learned" notebook — it's a relational database with a few well-defined tables, plus checkpoints of in-flight runs. Resist the temptation to give the agent open-ended memory: anything it "learns" that changes behavior must land as an explicit, reviewed config change, because an unreviewed learned rule is exactly what an attacker would try to plant (and the 2026 pilot literature notes that a wrongly-approved variance becomes a precedent the system then repeats).

**In the working context for a single run (ephemeral, rebuilt from the DB on every step):** the invoice's current state; the structured extraction (not the raw PDF text, once extraction is accepted); the PO and receipt snapshot; the resolved vendor record _minus_ bank details (a boolean about bank-match is enough); the `MatchResult`; the vendor profile summary (a dozen numbers: median amount, invoice cadence, usual GL codes, days since last bank change, count of prior exceptions); the tolerance and matrix config version; the last few steps of this run. Each model call gets only the slice it needs — the exception classifier does not need the raw PDF, the GL-coding proposer does not need the PO. Small, purpose-built contexts are both cheaper and safer.

**Persisted externally (survives runs, crashes, and days of waiting):**

- _Invoice record_ — one row per invoice, with state, timestamps per transition, ERP bill ID, pointers to the document and extraction. This is the state machine's source of truth.
- _Extraction record_ — both readings, per-field confidence, and any human corrections (with who/when). Human corrections are gold: they're your eval dataset and your drift detector.
- _Audit log_ — append-only (section 5).
- _Vendor profile_ — derived statistics, refreshed nightly, never edited by the agent. Also `last_bank_change_at` and `bank_change_verified_by`.
- _Dedup index_ — (vendor_id, normalized_invoice_number, amount, date) plus document hashes, for the duplicate check.
- _Open exceptions and approvals_ — with created-at and owner, so aging and escalation work.
- _Config_ — tolerances, approval matrix, allowlists, prompt versions, each versioned with effective dates.
- _Run checkpoints_ — where the durable-execution layer stores enough to resume a suspended run (typically the last completed step's inputs/outputs).

**Why each must be external.** State must survive the process because approvals take days. The duplicate check is impossible without cross-invoice history. Aging and escalation require timestamps that outlive a run. Vendor anomaly detection ("this vendor's invoices are normally $400; this one is $4,000") needs history. The audit trail must exist even if the agent process dies mid-step. And the config must be versioned so that the log's "auto-approved under rule R-07 v3" is reconstructable a year later.

**What should _not_ be persisted into anything the model reads:** raw document text (it's an injection vector; keep it in blob storage referenced by ID), bank details, full prior conversations. The model gets summaries and structured facts.

### 5. The audit trail: one entry, and one invoice end to end

First a distinction the tooling world blurs: **observability traces** (what Langfuse/LangSmith/Phoenix store — spans, token counts, latencies, prompt/response bodies) and the **audit trail** (the business record of what happened to this invoice, who decided, under which rule) are different artifacts with different retention, access control, and immutability requirements. Use the trace tools for debugging and evaluation; write the audit trail yourself to an append-only table you control, and have each audit event carry the trace ID so an auditor can drill into the model call if they need to.

**One audit event, fields:**

- `event_id` (ULID — sortable, unique), `invoice_id`, `run_id`, `step_seq` (monotonic within run)
- `ts_utc` (ISO-8601, from the server, never from the model)
- `actor` — one of `system`, `rule:<rule_id>@<config_version>`, `model:<model_id>@<prompt_version>`, `human:<user_id>`, `tool:<tool_name>`
- `event_type` — `state_transition`, `tool_call`, `model_call`, `human_decision`, `validation_failed`, `escalation`, `error`, `config_applied`
- `from_state`, `to_state` (null for non-transition events)
- `input_ref` / `output_ref` — content-addressed pointers (hashes) to the stored inputs and outputs, not the payloads themselves; payloads live in blob storage with their own retention
- `tool_name`, `tool_args_redacted` (PII/bank fields masked), `tool_result_summary`
- `model_id`, `prompt_version`, `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`, `trace_id`
- `decision` (e.g., `auto_approve`, `route_to:buyer`, `hold:duplicate`) and `decision_basis` — the rule ID and the actual values compared (`unit_price_variance=1.4%; limit=2%`), or for a human, their free-text reason
- `confidence` where applicable
- `error_class`, `error_message`, `retry_count`
- `prev_event_hash` — hash of the previous event for this invoice, forming a chain so tampering is detectable

**A clean, PO-backed invoice, end to end (14 events, ~8 loop iterations):**

1. `system` — `RECEIVED → INGESTED`; sha256, 2 pages, text layer present, not a hash duplicate.
2. `tool:extract_invoice_vision` — model call, prompt v7, 3,100 in / 900 out tokens, 6.2 s, trace `t-…`.
3. `tool:extract_invoice_text` — text-layer parse, second extraction.
4. `rule:EXTRACT-CONF@v2` — all five load-bearing fields agree and are grounded; two line descriptions differ cosmetically (0.93 sim); `auto_ok=true`. `INGESTED → EXTRACTED`.
5. `rule:VALIDATE@v2` — lines sum to subtotal (Δ 0.00), tax 11% in allowed set, dates sane. `EXTRACTED → VALIDATED`.
6. `tool:lookup_vendor` — exact tax-ID match to vendor 00213; `bank_details_match_on_file=true`; last bank change 412 days ago. `VALIDATED → VENDOR_RESOLVED`.
7. `tool:find_duplicates` — no candidates above 0.6. `→ DUPLICATE_CHECKED`.
8. `tool:get_purchase_order` PO-4471; `tool:get_receipts` PO-4471 (two events).
9. `rule:MATCH@v5` — 4/4 lines within tolerance (max price variance 0.8%), freight $18 not on PO but under $50 rule, total Δ $0.00. `→ MATCHED`.
10. `rule:APPROVAL-MATRIX@v3` — total $742.10 < $1,000, clean match, known vendor → `auto_approve`. `MATCHED → APPROVED`.
11. `tool:create_bill` — idempotency key `00213:INV20260901`; ERP bill `B-9932`. `APPROVED → POSTED`.
12. `rule:TERMS@v1` — net 30 from 2026-09-01 → pay-by 2026-10-01; no discount. `tool:mark_ready_for_payment`. `POSTED → SCHEDULED`.
13. (later, bank feed) `system` — payment matched, amount equal. `SCHEDULED → PAID → RECONCILED`.
14. `system` — `→ CLOSED`. Run cost $0.04, human touches: 0.

An exception invoice adds a `model:classify_exception` event, a `state_transition` to `EXCEPTION`, a `notify` event, days of silence, a `human:<user>` decision event with their reason, and possibly a `rule:MATCH` re-run after a receipt was posted. Every one of those is reconstructable from the table alone.

### 6. Edge cases the design must absorb before you build

**Duplicates that aren't byte-identical.** The same invoice arrives by email and via a portal; a vendor resends an unpaid invoice with a suffix (INV-001 vs INV-001A); a paper copy is scanned after the PDF was already processed; the same vendor exists twice in your master under slightly different names (the recovery-audit firms say duplicate vendor records are the root cause more often than duplicate invoices). Rule that practitioners use: normalize the invoice number (uppercase, strip punctuation and spaces, strip common prefixes), then flag when vendor matches exactly _and_ amount within 0.5% _and_ date within 7 days _and_ invoice number ≥ 85% similar — or when amount and vendor match with any invoice number in a 30-day window. Detection runs before approval. Merge duplicate vendors at the master, not in the matcher.

**Amount mismatches, decomposed.** Price variance (vendor raised prices; PO is stale), quantity variance (partial shipment billed in full; over-shipment), freight/handling not on PO, tax mismatch (wrong rate, tax on freight), currency (PO in USD, invoice in EUR), rounding, and the sneaky one: a total-only check that passes because an over-billed line is offset by an under-billed line. Always match line by line first, then totals.

**Multiple invoices per PO and partial deliveries.** A blanket or large PO gets billed in pieces. Track open (unbilled) quantity per PO line and match against that, not against the original ordered quantity, or your second invoice looks like a duplicate and your third looks like over-billing.

**Missing PO.** Genuinely non-PO spend (rent, utilities, SaaS, professional services); a PO created _after_ the invoice arrived ("PO after the fact" is a control failure but common in small firms); the vendor omitted the PO number. Search open POs for this vendor by amount before declaring non-PO; if you enforce no-PO-no-pay, the exception path should ask the buyer to raise/confirm a PO, not reject the vendor.

**Credit notes, pro-formas, statements, quotes.** All look like invoices. Negative totals or "credit" wording → credit path (applied against the vendor's balance, never "paid"). "Pro forma" / "quote" / "estimate" → not payable. A statement listing several invoices → not an invoice; extract the referenced invoice numbers and check they're all known.

**Fraud, ranked by likelihood for a small business.** (1) Bank-detail change requests by email from a "vendor" (BEC/vendor email compromise) — the number-one vector per the AFP survey; defended by structural inability to change bank details plus a phone callback to a number on file, never one in the email. (2) Lookalike domains (one letter off), which more than half of surveyed organizations saw. (3) Ghost vendors and fabricated invoices, increasingly AI-generated and visually perfect — defended by the match, the vendor master, and treating visual polish as zero evidence. (4) Inflated or duplicate invoices from a real vendor. (5) Hidden-text prompt injection in the PDF itself — new, documented, and cheap to attempt. Your Day 3 guardrails should be tested against a small set of adversarial invoices you make yourself (white text, off-page text, instructions in metadata, a remit-to block that differs from the vendor master).

**Vendor identity ambiguity.** "ACME Ltd", "Acme Limited", "ACME (UK)", the same tax ID under two names after a rename, two legitimate vendors with similar names. Match on tax ID and bank account (server-side) before name.

**Time and aging.** Invoices stuck in `PENDING_APPROVAL` because the approver is on holiday; early-payment discounts missed because the run waited; period-end cutoffs (September invoice arriving in October). Escalate on age; compute discount deadlines at scheduling time; record both invoice date and received date.

**Documents that aren't what they seem.** A photo of a paper invoice taken on a phone (skewed, shadows); a PDF containing three invoices; a 40-page PDF where the invoice is page 2 and the rest is a delivery manifest; an invoice in a language or number format the prompt didn't anticipate (1.234,56); a vendor whose template changed last month so the extraction that worked for a year quietly starts misreading the PO field.

**Model and integration drift.** A model version update changes extraction behavior; the accounting system's API rate-limits you or returns a 500 after creating the bill (hence idempotency keys); OAuth tokens expire mid-run; the vendor master was updated between when you fetched it and when you posted. Pin model versions, retry writes idempotently, and re-validate against fresh data after any suspension longer than a few hours.

**Accountability.** When a human overrides, capture the reason. When the system auto-approves, capture the rule and the values. When it's wrong, the fix is a config change with a date, not a prompt tweak nobody can find later.

---

## Part 2 — What I found: the stack, layer by layer

### Is it "Python with AI libraries I don't know"? Mostly yes — here's the actual layer cake

Python is the default not because it's better at this than TypeScript but because every layer below is Python-first: the model SDKs, Pydantic (which is the schema/validation library the SDKs themselves are built on), the agent frameworks, the durable-execution libraries, the observability SDKs, and the eval tools. TypeScript is a legitimate alternative (Mastra and the Vercel AI SDK are the leading TS agent toolkits; LangGraph.js and the Claude Agent SDK also ship TS) but for a learner the Python path has more examples and fewer seams. The layers, bottom to top:

1. **Model call + structured outputs** — the `anthropic` SDK (or OpenAI's), with JSON-schema-enforced output validated into Pydantic models.
2. **Document reading** — native PDF input to the model, a text-layer/OCR second reading, optional specialized document-AI services.
3. **Orchestration / the loop** — hand-written first; then Pydantic AI or LangGraph.
4. **Durable execution + human-in-the-loop** — DBOS, Temporal, Prefect, Restate; or LangGraph's own checkpointer.
5. **ERP integration** — official QuickBooks Online / Xero MCP servers or REST APIs, behind a narrow tool allowlist.
6. **Observability** — OpenTelemetry `gen_ai.*` spans → Langfuse or Phoenix (self-hosted) or LangSmith/Braintrust (managed).
7. **Audit trail** — your own append-only table.
8. **Evals** — golden invoice set, pytest + DeepEval / promptfoo, run in CI.
9. **Boring infrastructure** — Postgres, a queue or cron for intake, blob storage for originals, a tiny approval UI or Slack bot.

Each in turn, with what the best teams do and why.

### Layer 1: the model call, and why structured outputs change the design

The single biggest change from a year ago is that structured output is now a first-class, generally available API feature rather than a prompting trick. Anthropic's API takes an `output_config.format` of type `json_schema` and constrains decoding so the response is guaranteed to match; the Python SDK accepts a Pydantic model directly and validates into it. The same mechanism applies to tool arguments (`strict` tool use), so a tool's inputs are guaranteed to match its input schema. Constraints to know: every object needs `additionalProperties: false`, and numeric/string-length constraints aren't enforced by the schema layer — so you still validate ranges and arithmetic in code afterward (which you'd do anyway for the guardrails above).

What this means architecturally: your `InvoiceExtraction`, `MatchResult`, `ExceptionClassification`, and `GLCodingProposal` are Pydantic models first, prompts second. The Pydantic model _is_ the contract between the model and your code, and it's the same model you'll use in your database layer, your eval assertions, and your audit log. Write the schemas on day 1 before any prompt.

Two practices from teams doing extraction in production: keep the extraction schema flat-ish and explicit (nested optional objects are where models get creative), and include an `evidence` sub-field per important value (page, nearby text) — it costs a few hundred output tokens and gives you the grounding check for free.

### Layer 2: reading the document — vision LLMs vs. document-AI services, and the confidence problem

The industry has shifted from multi-stage OCR pipelines (detect text → recognize → layout → tables → key-value) toward vision-language models that do it in one pass, and a wave of small open-source OCR-specialized VLMs appeared in 2025–26 (PaddleOCR-VL, DeepSeek-OCR, olmOCR, dots.ocr, GLM-OCR among them) that run on one GPU. The frontier hosted models read PDFs natively: Claude's API accepts a PDF as a `document` content block (base64, URL, or via the Files API), processes each page as both extracted text and an image, and allows requests up to 32 MB and 600 pages on current models. For an invoice — one to three pages — this is the simplest possible reader and you should start with it.

Three things the vendor benchmarks won't tell you plainly:

_Accuracy claims are marketing until measured on your documents._ Content-marketing sites cite "97–99% field-level accuracy" for LLM extraction and "95–99%" for Azure Document Intelligence / Google Document AI / Textract. Those numbers are unsourced, definitional (character-level? field-level? which fields?), and drawn from clean invoices. The one hard number I trust: on DocILE, a public benchmark of ~6,700 real invoices with 55 field types including IBAN/BIC/VAT and line-item sub-fields, a frontier vision model (GPT-4o, temperature 0) failed on about 26% of fields. Your invoices are easier than DocILE's worst, but the point stands — plan for a meaningful minority of fields to be wrong, and plan for the wrong ones to look confident.

_VLM readers hallucinate differently from OCR._ Classic OCR misreads or skips characters. A VLM can invent plausible text that was never on the page, and because it reads fluently it's harder to catch. The extraction-confidence paper's authors put it well: a silently wrong extraction is more dangerous than a visibly absent one. This is why a second reading matters.

_The model's confidence is not a routing signal._ The same paper tested three cheap confidence proxies as ways to decide "automate vs. send to human": mean token log-probability (0.705 ROC-AUC), verbalized self-assessed confidence (0.692), and five-sample self-consistency (0.744, at 5× cost). All three collapsed toward accepting everything at practical thresholds. What worked: fusing signals about the _document_ — OCR word confidence in the region the value came from (0.896 AUC on its own, better than anything model-internal), whether the extracted value literally appears in the OCR text, image sharpness at that spot — with the disagreement between two structurally different reads of the same document (a field-by-field "hunter" prompt vs. a holistic "list everything you see" prompt). Their full system reached 0.928 AUC and 99.1% accuracy on the 80% of fields it chose to automate. You don't need the gradient-boosted classifier; the day-2 version is: two readings, exact agreement on money/ID fields, and value-in-text grounding. That alone eliminates most confident hallucinations.

_Where the specialized services fit._ Azure Document Intelligence's prebuilt invoice model, Google Document AI's invoice parser, and AWS Textract's AnalyzeExpense give you per-field confidence and bounding boxes out of the box and are priced per page (Azure's prebuilt tier was listed at $10 per 1,000 pages in 2026 vendor comparisons; Google's invoice parser at $0.10 per 10 pages — verify current pricing). They're strongest on the fixed layouts they were trained on and weak outside them; one small independent test in August 2026 (by a competing vendor, so discount accordingly) reported Azure at ~86% of fields correct on invoices. Reasonable use: as the _second reading_ for scanned/image invoices where you need OCR anyway, since they hand you the word confidences and boxes the grounding check wants. For born-digital PDFs, the text layer (via `pypdf`/`pdfplumber`/`pymupdf`, or Docling for layout-aware parsing) is a free second reading. Open-source OCR (Tesseract, PaddleOCR/RapidOCR) works for scans if you'd rather not pay per page, at the cost of more setup.

### Layer 3: orchestration — write the loop yourself, then pick a framework for the right reason

The July 2026 framework landscape (I used Langfuse's comparison as the spine and cross-checked against several others):

- **LangGraph** (1.x) — explicit graph of nodes and edges; durable execution that resumes exactly where it stopped; `interrupt()` for human-in-the-loop; short- and long-term memory; used in production by Klarna, Replit, Elastic, and others per its README. The default for "the process itself must be auditable and deterministic." Cost: more upfront structure.
- **Pydantic AI** (2.x) — type-safe agents where inputs, tool signatures, and outputs are Python types; OpenTelemetry instrumentation built in; durable execution via official integrations with Temporal, DBOS, Prefect, and Restate; supports every major model provider. The "FastAPI feeling" option for Python developers who want tests and type contracts without heavy orchestration.
- **OpenAI Agents SDK** (0.18, still 0.x) — minimal primitives (agents, handoffs, guardrails, sessions), provider-agnostic despite the name, built-in tracing. Good if you're GPT-centric.
- **Claude Agent SDK** — the Claude Code harness exposed as a library: a hardened loop with hooks, permission allowlists, in-process MCP tools, subagents. Excellent for coding/research agents with file and shell access; heavier than you need for a state-machine workflow, but its hooks and permission model are worth studying for Day 3.
- **Google ADK** (2.x), **Microsoft Agent Framework** (1.x; the successor to AutoGen, which is now in maintenance mode, and Semantic Kernel), **AWS Strands** — ecosystem-aligned picks.
- **CrewAI** — role-based multi-agent crews plus "Flows" for deterministic wrapping. Approachable; a poor fit here because AP doesn't need a cast of agents.
- **Mastra / Vercel AI SDK 7** — the TypeScript options.
- **Temporal** (durable execution platform, not an agent framework) — many teams put the agent loop _inside_ a Temporal workflow and let Temporal handle retries, timeouts, signals for human approval, and state that lasts for days or years; Temporal's own AI cookbook has a human-in-the-loop Python example using signals and durable timers with a full decision log.

Two pieces of advice that appear in nearly every serious source, including Langfuse's and Anthropic's: a single-model tool loop is under a hundred lines with the provider SDK and is a fine place to start; frameworks earn their place when you need durable state, retries, human-in-the-loop steps, and consistent tracing — the parts that are tedious to rebuild. And if you use a framework, understand the code underneath it, because wrong assumptions about what it does are a top source of bugs.

For your curriculum this maps cleanly. **Days 1–3: write the loop yourself** with the raw SDK, Pydantic models, and an explicit state enum. You'll learn more, and you'll have something to compare frameworks against. **Day 4: adopt one** when memory and human-in-the-loop force the question. If you want the state machine to be _visible_ in code (nodes = states, edges = transitions, `interrupt()` at approvals), pick LangGraph. If you want typed contracts everywhere and OTel for free, and you're happy to model the state machine yourself in Python with a durable wrapper, pick Pydantic AI. Either is defensible; the Langfuse comparison's own verdict is Pydantic AI for type-safe single agents embedded in Python services, LangGraph for explicit orchestration of long-running, multi-actor workflows. Build the same two states in both and trace them; commit to the one whose traces you'd rather debug.

### Layer 4: durable execution and the human-in-the-loop pause

This is the layer your Day 4 is really about. An approval takes days; a process restart mid-run must not lose or repeat work; a retried ERP write must not create a second bill. The general solution is "durable execution": every step's inputs and outputs are checkpointed, and on restart the workflow replays to where it was.

Options, lightest to heaviest:

- **LangGraph checkpointer** — if you chose LangGraph, its built-in checkpointing (to SQLite/Postgres) plus `interrupt()` gives you suspend-for-a-human and resume, with no extra infrastructure.
- **DBOS** — runs _in-process_ as a Python library and checkpoints workflow and step state to Postgres; Pydantic AI wraps any agent in a `DBOSAgent` so the run loop becomes a durable workflow and model/MCP calls become steps. No separate server. This is my pick for a solo builder who wants real durability without operating a cluster.
- **Prefect / Restate** — also officially supported by Pydantic AI; Prefect if you already think in flows/tasks, Restate if you want a lightweight durable runtime with a strong HTTP story.
- **Temporal** — the industrial option: a separate server (or Temporal Cloud), workflows in plain Python, signals for human input, timers that survive anything. Pydantic AI has native Temporal support co-maintained with Temporal. Overkill for the 7 days; the right answer at scale.

What durable execution does _not_ do, which people learn the hard way: it doesn't make your side effects idempotent. If `create_bill` succeeds and then the process dies before the checkpoint, the replay will call `create_bill` again. Hence the idempotency key on every write, and a "check before write" for anything that can't take one.

### Layer 5: talking to the accounting system

For a QuickBooks Online or Xero-class system you have three routes, and the news in 2026 is that the vendors themselves ship MCP servers:

- **Intuit's official QuickBooks Online MCP server** (open source, previewed October 2025, now documented for production) runs locally as a stdio subprocess with OAuth 2.0 and exposes 144 tools across 29 entity types plus 11 reports. The REST and a newer GraphQL API remain. Intuit's developer program has a free Builder tier with uncapped "Core" (mostly write) calls and 500,000 metered "CorePlus" (read/query/report) calls per month; excess reads are blocked, not billed; production use requires a self-assessment, and Intuit's terms attach responsible-AI and data-protection obligations to LLM use. (Verified via a July 30, 2026 third-party audit of Intuit's pages; confirm on Intuit's site before relying on the numbers.)
- **Xero's official MCP server** and an `xero-agent-toolkit` repo with examples for LangChain, OpenAI Agents, and Google ADK.
- **Unified-API vendors** (Merge, Codat, Rutter, Unified.to, Truto, Apideck) that normalize QuickBooks/Xero/NetSuite/Zoho behind one schema, several now with MCP fronts and PII controls. Worth it if you'll ever support more than one accounting system; unnecessary for one.

The trap everyone flags: handing the model all 144 tools is "tool bloat" — it picks the wrong one, hallucinates parameters, and burns tokens. And it's a security hole: an extraction/matching agent has no business seeing `delete_customer`. The pattern is a _virtual_ or filtered server — one server definition per agent role exposing exactly the read tools it needs (`get_purchase_order`, `get_receipts`, `query_bills`, `query_vendors`) and, for the one code-called write, `create_bill`. Intuit's own server has environment flags to disable write/update/delete categories. In your design, the model never calls the ERP directly anyway; code does, from specific states. MCP is still useful because it gives you typed tool schemas and OAuth handling for free.

One more integration you'll want: a bank feed or the accounting system's payment records for reconciliation. In the 7-day build, "reconciliation" can be reading the bill's paid status from the ERP.

### Layer 6: observability, and why it isn't your audit trail

The consolidation in this space happened fast: ClickHouse acquired Langfuse in January 2026 and Cisco announced its intent to acquire Galileo in April. The practical lesson every engineer draws is: don't couple your instrumentation to a vendor. Instrument to the **OpenTelemetry GenAI semantic conventions** (`gen_ai.*` attributes on spans for model calls, tool calls, agent runs) and choose the backend later. Caveat: as of v1.40/1.41 of the spec (early-to-mid 2026) the `gen_ai.*` namespace is still marked experimental, SDKs disagree on a handful of attribute names, and the churn is at the edges (multimodal, agent graphs, MCP). Pydantic AI and Strands emit these spans natively; LangGraph and the OpenAI Agents SDK trace natively to their own backends and via OTel to others; Langfuse ingests OTLP directly; Phoenix converts `gen_ai.*` into its OpenInference schema automatically since May 2026.

Backend choice by deployment model: **self-hosted Langfuse or Arize Phoenix** if telemetry (which includes invoice contents) must stay inside your perimeter — and for AP it should; **LangSmith or Braintrust** if you want managed with built-in evals; **Helicone**-style proxies for zero-code cost tracking.

Why this is separate from the audit trail (section 5): trace stores are optimized for debugging, sampled or retention-limited, and hold raw prompts and responses. An auditor wants an immutable, complete, per-invoice record of decisions and actors, in a system with access controls matching your financial records. Write that yourself; link it to the trace by ID.

### Layer 7: evaluation and testing — the part that separates a demo from a system

The consistent practice among teams shipping extraction and agent pipelines: a **golden dataset** of real documents with human-verified expected values, run on every prompt or model change, with the build failing if field-level accuracy on load-bearing fields drops below a threshold. Start with 20–50 invoices (keeps CI under five minutes), and grow it from production corrections — every human fix to an extraction becomes a new golden case. Measure per field, not per document; "invoice total" and "vendor" should be near-perfect, line items will lag.

Tooling: **DeepEval** plugs into pytest (`assert_test`) and supports component-level evals on individual tool calls or spans; **promptfoo** is YAML-driven and good for comparing prompts and models side by side; **Braintrust** and **LangSmith** bundle evals with tracing. Any of them works; the one you'll actually run in CI is the right one.

Beyond extraction accuracy, test the _decisions_: unit tests for `compute_match` with a table of known variances; property tests that an invoice with a mismatched remit-to never reaches `APPROVED`; a small adversarial set (hidden text, instruction text, lookalike vendor) that must land in human review; and a replay test that kills the process mid-run and asserts exactly one bill exists afterward.

### The stack I'd actually use for the seven days

Python 3.12 with `uv`. `anthropic` SDK with `output_config` structured outputs and Pydantic v2 models for every contract. Claude's native PDF input as reader one; `pymupdf` text layer (or Azure Document Intelligence prebuilt-invoice for scans) as reader two. A hand-written state machine and loop (days 1–3), `httpx` for the two external calls (an ERP read via the official MCP server or REST, and a web-check API). Guardrails as a versioned YAML config loaded into Pydantic. Postgres (or SQLite on day 1) for invoice state, vendor profiles, dedup index, config, and the append-only audit table with a hash chain. On day 4, wrap the loop in Pydantic AI + DBOS (or port the states to LangGraph with a Postgres checkpointer) to get suspend/resume for approvals. OpenTelemetry → self-hosted Langfuse in Docker. DeepEval + pytest with a golden set of your real invoices. A Slack bot or a one-page web form for approvals. No vector database, no multi-agent crew, no payment tool.

Cost to run: a two-page invoice through a mid-tier vision model with two structured-output calls is on the order of cents; the ERP calls are free at your volume; Langfuse and Postgres are free self-hosted. The expensive part is your time building the golden set — which is also the most valuable thing you'll produce.

---

## Where this could be wrong

**The "workflow, not agent" framing could be too conservative for your learning goals.** If the point of Day 1 is to feel what an open tool-choosing loop is like, build a tiny one for a sub-problem (vendor resolution, where the model can choose between name search, tax-ID search, and web check) and keep the money path deterministic. You'd get the pedagogy without the risk.

**Framework verdicts are dated the week they're written.** Everything in Layer 3 was verified against July–September 2026 sources, but the OpenAI Agents SDK is still 0.x, LangGraph and Pydantic AI both shipped major versions this year, and vendor-native SDKs are moving fast. Several aggregator articles I found contradict each other on release dates; I've trusted the framework maintainers' and Langfuse's write-up over listicles. Re-check before Day 4.

**The extraction accuracy numbers cut both ways.** The DocILE result (≈26% field failure) comes from a hard benchmark with many obscure fields and a 2024-era model, and the paper's authors sell document-processing software. Your invoices, on a 2026 model, on the five fields that matter, will do far better. The design implication (two readings, grounding, human review for disagreements) holds regardless — the number is a reason to build the check, not a forecast of your error rate.

**Vendor-published tolerances and KPIs are conventions, not evidence.** "5% quantity tolerance," "40–70% touchless," "exception rate under 20–25%" all come from AP-automation vendors and consultants. They're consistent with each other and with practitioner accounts, which is why I used them, but the right numbers for a specific small business come from its own invoice history. Start tight, watch the exception rate, loosen deliberately.

**Prompt-injection-via-invoice is documented as a scenario more than as a loss.** I found security-research write-ups, a conference demonstration on a KYC document agent, a real zero-click exploit of an enterprise AI assistant via hidden PDF text (August 2026), and threat-intel on injection payloads carrying payment instructions — but not a public post-mortem of an AP agent that paid an attacker this way. That may mean it hasn't happened at scale yet, or that nobody has admitted it. The defenses are cheap enough that the distinction doesn't change the recommendation.

**Regional specifics.** Fraud statistics are U.S.-centric (AFP, FBI). Tax handling (VAT rates, invoice legal requirements, e-invoicing mandates) varies by country and I've deliberately not asserted rules for any jurisdiction; validate the tax logic against local requirements before Day 6.

**What didn't turn up.** No independent, recent, methodology-transparent benchmark of hosted vision models on invoice extraction — every one I found was vendor-run. No clear evidence on whether the specialized document-AI services still beat frontier VLMs on line items in 2026, which is the field that matters most for matching. If you want to settle it, your golden set will do it in an afternoon.

---

## What follows from this: mapping back to your seven days

**Day 1 — Agent loop.** Write the state enum and the transition table first. Write the Pydantic contracts (`InvoiceExtraction`, `MatchResult`, `ExceptionClassification`). Then the loop: fetch state → decide → apply → log → repeat, with a max-step counter. Decide by code where you can; call the model only in `EXTRACTED` and `EXCEPTION`. Run it on one PDF end to end with the ERP and web tools stubbed.

**Day 2 — Tools.** Implement `extract_invoice_vision` (native PDF input + structured output), `extract_invoice_text` (text layer), and `compute_extraction_confidence` (agreement + grounding). Wire one real ERP read (a PO lookup via the official MCP server or REST) and one real web check (a domain/registry lookup). Notice that the model "decides when to call" the web check only inside `NEW_VENDOR` — that's the scoped agency you want.

**Day 3 — Guardrails.** Externalize tolerances and the approval matrix to versioned config. Add input validation, arithmetic validation, the output filter, and the hard prohibitions (delete the tools that shouldn't exist). Build five adversarial invoices — hidden text, instruction text, off-page text, lookalike vendor, remit-to mismatch — and assert each lands in human review.

**Day 4 — Context & memory.** Move state, extractions, vendor profiles, dedup index, and config into Postgres. Add the approval pause: suspend at `PENDING_APPROVAL`, resume on a signal. This is where you adopt Pydantic AI + DBOS or LangGraph + checkpointer. Prove it by killing the process mid-run and resuming without a duplicate bill.

**Day 5 — Audit trail.** Implement the event table with the fields in section 5 and the hash chain. Wrap every tool and every transition. Ship OTel spans to Langfuse and put the trace ID on each event. Print the full trail for one invoice and read it as an auditor would.

**Day 6 — Real workflow.** Point it at a real inbox. Expect most invoices to be non-PO; expect the vendor-resolution and GL-coding paths to matter more than the three-way match; expect a template you've never seen. Every human correction goes into the golden set.

**Day 7 — Checkpoint.** Run the golden set in CI. Report: touchless rate, exception rate by reason code, field accuracy by field, cost per invoice, mean time in `PENDING_APPROVAL`. Those five numbers are what an AP manager would ask for, and having them is what makes it a system rather than a demo.

Three things to keep watching after: the OTel GenAI conventions stabilizing (then you can lean on them harder), Intuit's and Xero's MCP servers maturing toward hosted/remote endpoints, and whether the extraction-confidence research turns into a library you can drop in instead of the hand-rolled version.

---

## Sources

**Primary and most load-bearing**

- Anthropic, _Structured outputs_ (platform.claude.com/docs/en/build-with-claude/structured-outputs) — GA `output_config.format`, Pydantic/Zod helpers, `additionalProperties: false`, strict tool use. Current as of Sept 2026. High trust.
- Anthropic, _PDF support_ (platform.claude.com/docs/en/build-with-claude/pdf-support) — document blocks, 32 MB, 600/100 page limits, text+image processing. Current. High trust.
- Anthropic, _Building effective agents_ (anthropic.com/research/building-effective-agents), Dec 2024 — workflows vs. agents; simplest pattern first. Foundational; high trust.
- Nitesh Kumar (Perfios), _Beyond Logprobs: A Multi-Signal Confidence Engine for LLM-Based Document Field Extraction_, arXiv 2606.24420, June 2026 (IJCAI-ECAI workshop) — DocILE ≈26% field failure; logprob/verbalized/self-consistency AUCs; OCR-grounding and dual-read signals. Peer-reviewed workshop paper from a vendor; methodology transparent. High trust on findings, treat the product framing with care.
- AFP, _2026 Payments Fraud and Control Survey Report_ (released Apr 14, 2026; via AFP site, U.S. Bank, Nacha, and press summaries) — 76% fraud exposure, 74% BEC, 58% checks, 17% using AI. High trust; U.S.-centric.
- FBI IC3 2025 report figures (via DeepStrike and Gigapay summaries, July–Aug 2026) — 24,768 BEC complaints, $3.05B losses. High trust on the figures; secondary reporting.
- Temporal, _Human-in-the-loop AI agent_ cookbook (docs.temporal.io/ai-cookbook/human-in-the-loop-python), Aug 2026 — signals, durable timers, decision log. Primary docs; high trust.
- Pydantic, _Durable execution overview_ (pydantic.dev/docs/ai/capabilities/durable_execution/overview), current — Temporal, DBOS, Prefect, Restate integrations. Primary docs; high trust.
- DBOS, _Use DBOS with Pydantic AI_ (docs.dbos.dev/integrations/pydantic-ai), current — in-process, Postgres checkpoints, `DBOSAgent`. Primary docs; high trust.
- Intuit QuickBooks Online MCP server (github.com/intuit/quickbooks-online-mcp-server) and developer program terms, as audited by Vorp Labs, July 30 2026 — 144 tools, Builder tier limits, production review requirements. Third-party audit of primary pages; medium-high trust; re-verify on Intuit's site.
- XeroAPI, _xero-agent-toolkit_ (github.com/XeroAPI/xero-agent-toolkit) — official MCP server and framework examples. Primary; high trust.

**Landscape and practitioner sources**

- Langfuse, _Open-Source AI Agent Frameworks: Which One Is Right for You?_, July 13 2026 — the framework comparison spine used here; written by an observability vendor that integrates with all of them, so relatively neutral. High trust for capabilities; medium for "best for" verdicts.
- Speakeasy, _Choosing an agent framework_, Mar 4 2026 — OpenAI Agents SDK adoption; Pydantic AI security-through-code. Medium trust.
- Uvik and hiredeveloper.dev framework roundups, Aug–Sept 2026 — used only for triangulation; contain dating errors. Low-medium trust.
- AIMultiple, _State of OCR technology_, June 17 2026 — VLM shift, open-source OCR models, VLM hallucination. Analyst content; medium-high trust.
- invoicedataextraction.com and Parsli comparisons, May–Aug 2026 — accuracy tiers and 2026 per-page pricing for Azure/Google/AWS. Vendor content marketing; low-medium trust on accuracy claims, medium on pricing (verify).
- Kognitos, _The 7 Places Generative AI Quietly Fails in AP_, May 19 2026 — pilots stall around 60% touchless; contract-clause-citation audit standard. Vendor; medium trust, consistent with practitioner accounts.
- PayStream Advisors, _Three-Way Matching_, Mar 23 2026; Precoro, Apr 21 2026; Ramp, Aug 2026; GEP, Aug 2026; Dynamics Dad (D365 tolerance configuration), June 15 2026 — tolerance conventions, touchless rates, exception-rate rule of thumb. Vendor/consultant; medium trust; consistent across sources.
- Docsumo, _Guide to Duplicate Invoice Detection_, Apr 9 2026; Stampli, June 23 2026; Precoro, July 8 2026 — normalization and fuzzy-duplicate rules; duplicate vendor masters. Vendor; medium trust; consistent.
- Gigapay, _Vendor Invoice Fraud and BEC_, Aug 2026 — vendor-master as the attack target; AFP/IC3 synthesis. Vendor; medium trust.
- Lakera, _Indirect Prompt Injection_; Polygraf, June 11 2026; A2AS paper arXiv 2510.13825; AuthMind, June 29 2026; Decrypt on the PromptArmor/Atlassian Rovo finding, Aug 2026; Zscaler ThreatLabz, July 2 2026 — invoice-borne injection scenarios and the architectural defense consensus. Security vendors and researchers; medium-high trust on mechanism, low on incidence.
- DEV Community posts on OTel GenAI conventions (Anhaia, Apr 2026; Azena, July 16 2026), Arize Phoenix release notes May 15 2026, Digital Applied observability stack guide May 27 2026 — spec status, vendor consolidation, backend selection. Practitioner; medium-high trust.
- Inference.net, _LLM Evaluation Tools_, Feb 23 2026; DeepEval docs (CI/CD unit testing), July 2026 — golden-set practice and CI integration. Medium-high trust.
- IntuitionLabs on Temporal for agentic AI, Oct 2025 — includes the Gartner projection that ~40% of agentic AI projects will be cancelled by 2027. Secondary; medium trust.
