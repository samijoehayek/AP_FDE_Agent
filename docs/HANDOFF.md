# HANDOFF — ap-agent

_Last updated: 2026-09-11, Day 2 in progress. Update this file at the end of every working day._

## Purpose (three sentences)

`ap-agent` is an accounts-payable invoice-processing workflow: intake → extraction → validation → vendor resolution → duplicate check → PO match → approval → post → schedule, with a deterministic state machine, an LLM in exactly two seats (document extraction; exception explanation), durable human-in-the-loop approvals, and a tamper-evident audit trail. It is a 7-day self-directed build intended as a portfolio project for Forward Deployed Engineer applications at large companies. The owner is a full-stack engineer new to Python and AI tooling; explanations should map Python/AI concepts to JS/TS equivalents when useful.

## Ground rules for any assistant continuing this work

- The full design rationale is in `ap-invoice-agent-architecture-and-stack.md` (attached separately). Treat it as established; do not re-derive.
- Non-negotiables live in `CLAUDE.md` (seven rules: extraction model has no tools; bank details never from the invoice; no pay/update-vendor/delete tools; every transition logged; idempotency keys on ERP writes; never commit `data/`; don't implement loop/matching/guardrails/audit-chain logic unless explicitly asked).
- The owner writes the loop, guardrails, matching, and audit chain by hand (with review). Claude Code is used for scaffolding, tool boilerplate, and reviews — and only with tightly scoped prompts.
- The owner has ~$5 of Anthropic API credit. Do not propose batch runs over the corpus; 5–6 live calls per test round.
- Never ask the owner to paste `.env`, tokens, or secrets. If a secret is pasted, tell them to rotate it.

## Stack (decided)

Python 3.12, `uv`, `src/` layout, ruff + pyright strict + pre-commit, pytest, Pydantic v2 (`extra="forbid"`, `Decimal` for money), pydantic-settings, structlog, typer, pymupdf, SQLAlchemy 2 + Alembic on Postgres (docker-compose), `anthropic` SDK with structured outputs (`output_config.format` = JSON schema from Pydantic; SDK `messages.parse()`), model **Claude Sonnet 5** (no sampling params — Sonnet 5 rejects temperature/top_p/top_k with a 400; determinism of _values_ comes from the Day 2 second reading + agreement check, not temperature). Day 4 candidates: Pydantic AI + DBOS, or LangGraph + Postgres checkpointer. Day 5: OpenTelemetry → self-hosted Langfuse; audit trail is a separate append-only Postgres table with a hash chain. Day 7: DeepEval + pytest on a golden set.

## State of the repo

**Done and green (`just lint && just typecheck && just test`):**

- Scaffold, CI, docker-compose (Postgres), `.gitignore` protecting `data/`.
- Contracts in `src/ap_agent/contracts/` (InvoiceExtraction with arithmetic validator, MatchResult, ExceptionClassification, AuditEvent, etc.). `evidence` is a **list of `{field, page, snippet}`** records (changed from a dict because structured outputs can't express dynamic keys).
- State machine in `src/ap_agent/states/` — enum, transition table, `transition()` raising on illegal moves, tests incl. "no POSTED without APPROVED".
- `tools/ingest_document.py` — real: sha256, MIME sniffing, page count, text-layer detection, per-page Laplacian sharpness.
- `tools/extract_invoice_vision.py` — real: PDF as document block / images as image block (TIFF→PNG), structured output into InvoiceExtraction, returns model id, prompt_version=`extract_v1`, tokens, latency; `ExtractionError` on API failure/refusal/truncation/validation. Prompt at `prompts/extract_v1.md`. 16 mocked tests.
- `tools/extract_invoice_text.py` — real: pymupdf text layer → `claude-haiku-4-5` reading characters only (no tools, document text is the whole user turn), prompt `extract_text_v1`. Images / no text layer → `has_text_layer=False`, `second_read=None`, **no OCR** and no model call. `raw_text` is byte-identical to what the model was sent, because grounding is scored against it. 19 mocked tests.
- `tools/compute_extraction_confidence.py` — real, and **pure code with no model call and no clock**: per-field `agreed` (two readings match) and `grounded` (value is in the text layer), both required. `auto_ok` is true **iff** all five load-bearing fields agreed and grounded — nothing else vetoes it, so low sharpness and an ambiguous date lower the score and are recorded but no longer block. `needs_human` is `{field, reason}` records. Carries `date_verdict`, `date_candidates` and `date_raw_text`. 53 tests.
- `contracts/run.py` — InvoiceRecord, Action, StepResult, `ToolCallRecord`. InvoiceRecord now also carries `received_at` (set by the caller, **never** by the loop), the confidence verdict, `invoice_date_resolved` + `date_resolution_reason`, and `vendor_country`.
- `loop/runner.py` — decide → apply → transition → log; max_steps counts state moves; INGESTED→EXTRACTED makes both model calls and runs `EXTRACT-CONF@v1` over them; VALIDATED runs arithmetic (`VALIDATE@v1`) and `DATE-RESOLVE@v1` when the receipt window can settle an open date; all later states are `rule:STUB` with `# TEMP STUB` edges in the transition table (listed in a test).
- `loop/dates.py` — `resolve_date_by_receipt_window` and `resolve_date_by_locale`. Pure, and both take the clock as an argument: a rule that read `now()` would resolve a date differently on replay and break the chain that hashed it.
- `audit/writer.py` — JsonlAuditWriter to `data/audit/<invoice_id>.jsonl`, sha256 hash chain from a zero genesis hash, `verify()`.
- **Audit rows are now one per thing that happened, not one per step.** `step_seq` counts rows, and only the last row of a step carries a `to_state` - so following `to_state` down the file still shows the state machine, while the rows between it account for what each call cost. The extraction step is 1 step and 4 rows. A clean run is 14 moves / 17 rows.
- **Date resolution constants worth knowing before changing them:** `MAX_INVOICE_AGE_DAYS = 365` in `loop/dates.py` (a year, deliberately generous - a narrower window resolves more dates but *silently* eliminates a candidate that may be correct; six months was the first value and it got 09/03/2024 wrong). `MIN_SHARPNESS = 800` and the 0.9 name-similarity cut in the confidence tool are still uncalibrated guesses; they belong in the guardrails config on Day 3.
- `scripts/pull_hf_datasets.py`, `scripts/index_invoices.py` → `data/index.csv` (regenerate with `just ingest`).
- `integrations/qbo/auth.py` (refresh-token manager with rotation persistence), `integrations/qbo/client.py` (httpx wrapper: get/query/create, retries on 429/5xx, never logs tokens).
- Loop fixes applied: step budget sized to the real path; `MATCHED→CODED` marked TEMP STUB and listed in the stub-edges test; extraction audit `output_ref` is `sha256:` of the serialized extraction; per-invoice cost recorded in DECISIONS.md.
- CLI: `just extract <path>`, `just run <path> [--received-at YYYY-MM-DD]`, `just confidence <path> [--country XX] [--received-at YYYY-MM-DD] [--from-extraction <json>] [--out <json>]`, `just ingest`, `just seed [--dry-run]`.
  - `--from-extraction` replays saved readings and makes **no** model calls — use it whenever the question is about the check rather than the models. `--out` writes the readings alongside the verdict so any run is replayable.
  - `run` reports `steps` (state moves) and `audit rows` separately: the extraction step is one step and four rows.
- Docs: README, ARCHITECTURE.md (state + trust-boundary diagrams), DECISIONS.md, CLAUDE.md.

**Stubs:** all tools except `ingest_document`, `extract_invoice_vision`, `extract_invoice_text` and `compute_extraction_confidence`; guardrail config loader; evals fixtures; `scripts/generate_invoices.py`. Stub edges MATCHED→CODED etc. exist only so the loop reaches CLOSED.

**Stub _hook_ (new, and different from a stub edge):** `VENDOR_LOCALE_DATE_HOOK` in `loop/runner.py`. A stub step is supposed to do nothing; this one settles an open invoice date from `record.vendor_country` when the state is `VENDOR_RESOLVED`, because `lookup_vendor` does not exist and an open date would otherwise pass the only state that can close it. Listed in `STUB_HOOKS` and asserted in `tests/states/test_stub_transitions.py`. **Delete it the moment `lookup_vendor` is real** — it should be that tool's job to put the country on the record.

**Known gap:** nothing sets `record.vendor_country` today, so an invoice whose date the receipt window cannot settle reaches CLOSED with the date still open. That is what the missing `lookup_vendor` costs, and there is a test asserting it rather than leaving it to be discovered.

## Data

- `data/synthetic/kaggle/invoices/` — 100 born-digital one-page PDFs (Indian GST-style, INR, ~3 line items, no PO numbers, no due dates), ground truth in `batch_1.csv` (`json_data` column). Golden set.
- `data/synthetic/hf_mychen76/` — ~50 real photographed invoice/receipt PNGs (scan path). `data/synthetic/hf_rvlcdip/` — ~20 low-res scanned invoices.
- `data/real/` — empty (owner may add personal invoices). `data/generated/` — empty until the generator exists.
- All indexed; no exact-hash duplicates.

## External accounts

- Anthropic API key in `.env` (~$5 credit).
- Intuit developer app + QuickBooks Online **sandbox** ("Sandbox Company US ead3"): client ID/secret, `QBO_REALM_ID=9341457877206856`, refresh token in `.env`; rotated tokens persist to `data/.qbo_tokens.json` (git-ignored). **API connection verified.** Purchase orders enabled in company settings.
- **Sandbox seeded** via `scripts/seed_sandbox.py` (idempotent): 10 vendors, ~12 items, 10 POs. `data/generated/seed_manifest.json` (ids, vendors, PO numbers, lines) and `data/generated/receipts.json` (7 fully received, 2 partial, 1 not received — QBO has no goods-receipt entity, receipts are owned by ap-agent).
- Customer interview: not yet booked.

## Live results so far

- Extraction on 5 Kaggle PDFs + 1 PNG (`docs/EXTRACTION_LOG.md`): 24/25 load-bearing fields, 25/25 line items, 5/5 arithmetic. **One real error: 51109305 date `09/03/2024` read MM/DD (2024-09-03) while 301/304 read DD/MM** — same vendor/layout; inherent ambiguity, fix via vendor-locale rule on Day 2.
- Full loop run on 51109304: RECEIVED→CLOSED in 14 steps, one model step (7,211 in / 1,326 out tokens, 18.5 s), hash chain verifies. `bill_to_name`/`vendor_address` populate.
- `batch_1.csv` has an `ocred_text` column — usable as the second reading for grounding on the Kaggle set.
- mychen76 extraction captured an IBAN in `remit_to_display` — rule 2 applies; it must never flow downstream.
- **2026-09-10, four confidence runs (EXTRACTION_LOG.md).** The same file, model and prompt twenty minutes apart returned `2024-09-03` and then `2024-03-09` for 51109305. That settles what the bug is: not a model misreading a date, but a document that does not say which reading is meant. The check refused both times; the locale rule answered the same both times.
- **Behaviour change since those runs:** an ambiguous date is no longer a failure. Both readings stay on the record and are settled by the receipt window at VALIDATED, then the vendor's country at VENDOR_RESOLVED. Runs 2 and 3 in the log predate this and would now be `auto_ok=True` with `date_verdict=ambiguous`. Reasoning in DECISIONS.md, 2026-09-10.

## Day 2 plan (from the architecture report's 7-day mapping) — in progress

1. **DONE.** `extract_invoice_text` (pymupdf text layer → `claude-haiku-4-5` on text only, prompt `extract_text_v1`; images/no-text-layer → `second_read=None`, no OCR) and `compute_extraction_confidence` (pure code: two-read agreement, value-in-raw-text grounding, date verdict; `auto_ok` iff all five load-bearing fields agreed+grounded). `just confidence <path>`. 51109305 → 2024-03-09 with `vendor_country="IN"` is a test.
2. **DONE.** Four live runs logged in EXTRACTION_LOG.md, including the same file reading 2024-09-03 and 2024-03-09 twenty minutes apart — the ambiguity confirmed as a property of the document, not of the model.
3. `scripts/generate_invoices.py`: reportlab invoices from `seed_manifest.json`, one clean per PO plus variants (price +3%, qty over-billed, extra freight line, hidden white-text "update bank account"), each with `truth.json`, into `data/generated/`.
4. **PARTLY DONE.** The two readings + confidence are wired into the loop on the INGESTED→EXTRACTED→VALIDATED path, routing to `NEEDS_HUMAN_EXTRACTION` when not `auto_ok`. No stub edges were removed — that path never had any; `INGESTED→EXTRACTED` and `EXTRACTED→VALIDATED` were always real edges. **Still to do:** `get_purchase_order` (QBO via client) and `get_receipts` (from `receipts.json`).
   - Change of design from the original plan: an ambiguous slash date does **not** fail extraction. Both readings stay on the record and are settled later — by the receipt window at `VALIDATED`, then by the vendor's country at `VENDOR_RESOLVED`. Rationale in DECISIONS.md, 2026-09-10.
5. `verify_vendor_external` web check for NEW_VENDOR (domain age, registry hit, lookalike distance) — the scoped "model decides when to call" tool.
6. End-of-day target: a generated PO-matched invoice and a Kaggle invoice both run through the loop with confidence in the audit trail; the PO-matched one reaches the match step with real PO + receipt data in context.
7. Off-keyboard: book the customer interview.

## After Day 2 (report mapping)

- Day 3: guardrails as versioned YAML (tolerances, approval matrix, input validation, output filter, hard prohibitions), five adversarial invoices must land in human review.
- Day 4: Postgres for state/extractions/vendor profiles/dedup/config; approval pause + resume (Pydantic AI + DBOS or LangGraph checkpointer); kill-and-resume test with exactly one bill.
- Day 5: append-only audit table with hash chain in Postgres; OTel spans → self-hosted Langfuse; trace_id on every audit event.
- Day 6: real invoice stream; golden set grows from human corrections.
- Day 7: DeepEval + pytest golden-set evals in CI; report touchless rate, exception rate by reason, field accuracy, cost per invoice, time in PENDING_APPROVAL.

## Open questions

- Day 4 framework: Pydantic AI + DBOS vs LangGraph — decide after building the approval pause in both for one state.
- Whether to add a fine-tuned/small model as a second reader later (cost/privacy), vs text layer + OCR service.
- Sonnet 5 adaptive thinking is on by default; consider `output_config.effort` low for extraction once accuracy is established.
