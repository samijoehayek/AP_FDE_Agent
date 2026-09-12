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

Python 3.12, `uv`, `src/` layout, ruff + pyright strict + pre-commit, pytest, Pydantic v2 (`extra="forbid"`, `Decimal` for money), pydantic-settings, structlog, typer, pymupdf, SQLAlchemy 2 + Alembic on Postgres (docker-compose), prompts versioned per seat (`extract_v2`, `extract_text_v2`, selected from settings; older versions stay on disk), `anthropic` SDK with structured outputs (`output_config.format` = JSON schema from Pydantic; SDK `messages.parse()`), model **Claude Sonnet 5** (no sampling params — Sonnet 5 rejects temperature/top_p/top_k with a 400; determinism of _values_ comes from the Day 2 second reading + agreement check, not temperature). Day 4 candidates: Pydantic AI + DBOS, or LangGraph + Postgres checkpointer. Day 5: OpenTelemetry → self-hosted Langfuse; audit trail is a separate append-only Postgres table with a hash chain. Day 7: DeepEval + pytest on a golden set.

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
- `tools/lookup_vendor.py` — real: two tiers (tax id, normalised name), ambiguity is `none` with candidates. `normalise_vendor_name` is its own pure function with its own tests. `bank_details_match_on_file` is `None` because the master holds no remittance details — a comparison that did not happen, which is a different answer from one that failed. 29 tests.
- `tools/get_purchase_order.py` — real: `client.query` on `PurchaseOrder` by `DocNumber`, explicit field mapping, subtotal/discount rows discarded, amounts as `Decimal`. 15 tests against a captured-shape response at `tests/tools/qbo_purchase_order_response.json`.
- `tools/get_receipts.py` — real: reads `receipts_path` from settings, flattens every receipt document for one order into per-line quantities. 13 tests.
- `scripts/pull_hf_datasets.py`, `scripts/index_invoices.py` → `data/index.csv` (regenerate with `just ingest`).
- `scripts/generate_invoices.py` — real: 60 labelled invoice PDFs from the seeded POs, six variants each, into `data/generated/invoices/`. `Canvas(invariant=1)` and no clock anywhere, so two runs are byte-identical - a hash that moves means the generator changed, not that a document arrived. Truth files are `GeneratedInvoiceTruth` (`contracts/generated.py`), and a test asserts `InvoiceExtraction(**truth.expected.model_dump())` passes the arithmetic validator with no flags. 43 tests, all against real rendered files in `tmp_path`.
- `src/ap_agent/vendor_master.py` + `config/sandbox_vendor_master.yaml` — the vendors, in one file, read by both `seed_sandbox.py` and `generate_invoices.py`. Carries the country and the postal address, which the seed manifest does not because QuickBooks is never told them. Fails loudly on a missing vendor or a missing field. 21 tests.
- `integrations/qbo/auth.py` (refresh-token manager with rotation persistence), `integrations/qbo/client.py` (httpx wrapper: get/query/create, retries on 429/5xx, never logs tokens).
- Loop fixes applied: step budget sized to the real path; `MATCHED→CODED` marked TEMP STUB and listed in the stub-edges test; extraction audit `output_ref` is `sha256:` of the serialized extraction; per-invoice cost recorded in DECISIONS.md.
- CLI: `just extract <path>`, `just run <path> [--received-at YYYY-MM-DD]`, `just confidence <path> [...]`, `just ingest`, `just seed [--dry-run]`, `just generate [--only <po>] [--variants a,b,c] [--dry-run]`, `just vendor "<name>" [--tax-id X]`, `just receipts <po>`, `just po <po>`.
  - `just vendor` and `just receipts` read committed/local files and call nothing. **`just po` is the only one that makes a live QuickBooks call**, and it spends no tokens.
  - `--from-extraction` replays saved readings and makes **no** model calls — use it whenever the question is about the check rather than the models. `--out` writes the readings alongside the verdict so any run is replayable.
  - `run` reports `steps` (state moves) and `audit rows` separately: the extraction step is one step and four rows.
- Docs: README, ARCHITECTURE.md (state + trust-boundary diagrams), DECISIONS.md, CLAUDE.md.

**Stubs:** all tools except `ingest_document`, `extract_invoice_vision`, `extract_invoice_text`, `compute_extraction_confidence`, `lookup_vendor`, `get_purchase_order` and `get_receipts`; guardrail config loader; evals fixtures. `scripts/generate_invoices.py` is real.

**Stub edges remaining (10, down from 11):** `VENDOR_RESOLVED→DUPLICATE_CHECKED` (find_duplicates), `DUPLICATE_CHECKED→MATCHED` (compute_match), `MATCHED→CODED`, `CODED→PENDING_APPROVAL`, **`PENDING_APPROVAL→APPROVED` (the dangerous one — delete it the moment `request_approval` exists)**, `APPROVED→POSTED`, `POSTED→SCHEDULED`, `SCHEDULED→PAID`, `PAID→RECONCILED`, `RECONCILED→CLOSED`. `STUB_HOOKS` is now empty: no stub step does anything.

**Stub hooks: none.** `STUB_HOOKS` is an empty frozenset and a test asserts it. It held the vendor-locale date resolution for exactly as long as `lookup_vendor` was a stub; the tool owns the country now, so the hook is gone and the date is settled in the resolve-vendor step.

**The vendor-country gap is closed.** `lookup_vendor` puts the country on the record at `VALIDATED`, so an ambiguous date the receipt window cannot settle is closed one state later from the master's country. Asserted end to end in `tests/loop/test_vendor_routing.py`.

### Live check run 2026-09-12 — six defects, all now fixed

Full write-up of the run in `docs/EXTRACTION_LOG.md` (that section is left as
it was written; the re-test results go under it). Two defects were in the
extraction contract and four in the audit trail. **All six are fixed and the
re-test below is what confirms it.**

1. **Money with thousands separators fails validation.** `claude-haiku-4-5`
   returned `'272,100.00'` for `subtotal` and the `Decimal` parser rejected it -
   eight errors, extraction lost. This is not a fixture problem: the Kaggle PDFs
   print `74,120.00` too, and they have been passing only because both models
   happened to strip the commas on those documents. **Whether an invoice
   extracts currently depends on a formatting decision neither model is required
   to make.** Fix belongs on the `Money`/`UnitPrice`/`Quantity` annotations in
   `contracts/common.py` as a `mode="before"` strip, reusing the `_THOUSANDS`
   normalisation that already exists in `compute_extraction_confidence`.
2. **`LineItem.unit` rejected an empty string.** Generated invoices print no
   unit-of-measure column (the manifest has no UoM), so the model returned `""`
   and the whole extraction was lost over a field nothing compares. Real invoice
   lines often carry no unit either.

3. **The trail recorded two rows for a step that made two model calls.** The
   vision read had completed and been billed and left no trace, and the error
   was attributed to `compute_extraction_confidence`, a tool that had not run.
4. **`output_ref` carried labels**, not content addresses: `invoice:51109301`,
   `date:2024-03-09`, `fields:7`.
5. **No row carried `cost_usd`**, so a run's spend was unanswerable from its own
   trail.
6. **`VALIDATE@v1` printed `date=2023-07-03, date_open`** - naming one of two
   readings while the question was open.

**What changed**

- Numbers: one normaliser in `contracts/common.py`, used by the money types and
  by the grounding check. Ambiguous renderings **refuse** (`1,234`, `12.345.678`)
  rather than guess. Prompts bumped to `extract_v2` / `extract_text_v2`, both
  selected from settings; the `v1` files stay on disk.
- `LineItem.unit` accepts `""`. The model-facing schema is unchanged - still a
  plain string, still 19 top-level properties.
- Every tool writes its own audit row as it returns, success or failure, through
  `StepTrail`. A failure names the tool that failed.
- `output_ref` is `sha256:<hex>` everywhere; readable notes moved to
  `tool_result_summary`.
- `config/model_pricing.yaml` + `src/ap_agent/pricing.py`. Model rows carry
  `cost_usd` and record the pricing version. **The two prices are unverified -
  see the warning in that file.**
- `halt_no_tool` / `event_type=halt` for a stop because a tool is unwritten; a
  transition the table refuses for any other reason stays an error.
- Staleness (`invoice_date_too_old`) checked against `received_at`, failing only
  if every candidate fails.
- Receipts join on `line_no`. `scripts/migrate_receipts.py` has been run against
  `data/generated/receipts.json`; the seeder writes them from now on.
- `# TEMP STUB` edge `NON_PO → CODED`, so the Kaggle corpus can move past NON_PO.

### Re-test: DONE, 2026-09-12

Both runs reached `CLOSED` with a verifying chain. Full write-up in
`docs/EXTRACTION_LOG.md`; row-by-row trails and the raw purchase order in
`data/retest/2026-09-12/` (git-ignored).

| Run | State | Steps | Rows | Cost |
| --- | --- | --- | --- | --- |
| `generated/AP-SEED-001/clean` | `CLOSED` | 14 | 20 | $0.0290 |
| `kaggle/invoice_51109301` | `CLOSED` | 14 | 19 | $0.0364 |

**`just po AP-SEED-001` has now been run.** `vendor_erp_id` is `"58"`, the PO's
line 1 and 2 join to `receipts.json` by `line_no`, and the mapping needed no
changes. `tests/tools/qbo_purchase_order_response.json` is a verbatim capture,
not a reconstruction.

Two things the re-test itself found:

* **A property leaving `required` is enough to fail the structured-output
  complexity check**, even when the schema is smaller by every local measure.
  `LineItem.unit` now keeps its place in `required` through
  `__get_pydantic_json_schema__`. Nothing measurable on this side predicts this
  limit - **probe the API after any change to the extraction schema.**
* **The receipt window did not settle the Kaggle date** as the brief predicted.
  Both readings sit inside the 365-day window, so the rule declined - as
  designed - and the vendor's country settled it. See the log for the arrival
  dates that would make the window decide.

### Open question: is GL coding a third model seat?

The architecture report defines GL coding as a model call. This repository has
claimed **two** LLM seats throughout - document extraction and exception
explanation - and that claim is in `CLAUDE.md`, `README.md` and
`ARCHITECTURE.md`. A third seat would change a load-bearing statement about the
system's shape.

The alternative is a code lookup against vendor history: the account this vendor
was coded to last time, with a confidence based on how consistent that history
is, and anything unclear going to a person. That is deterministic, replayable,
and needs no seat at all.

**Decide before `propose_gl_coding` is written**, and if it becomes a seat,
update the two-seat claim everywhere it appears rather than leaving three
documents saying something that is no longer true. It is stated in
`CLAUDE.md:10`, `README.md:11`, and the trust-boundary diagram in
`docs/ARCHITECTURE.md`.

### Two things the step-4 brief assumed that the data does not support

Both were found by reading the files rather than by a test failing later.

1. **Kaggle invoice 51109301's vendor IS in the vendor master, so it does not reach `NEW_VENDOR`.** Every invoice in the Kaggle corpus carries `TechVision Distributors Pvt Ltd` / GSTIN `27AABCT1234F1Z5`, which is the *first record* in `config/sandbox_vendor_master.yaml` - the sandbox was seeded with names taken from that corpus. So 51109301 resolves on the tax-id tier and routes to **`NON_PO`** instead, because the Kaggle documents carry no purchase-order reference at all. `tests/loop/test_vendor_routing.py` asserts what actually happens, and a separate synthetic vendor covers the `NEW_VENDOR` path.

2. **A non-PO invoice now stops at `NON_PO` rather than reaching `CLOSED`.** `propose_gl_coding` is a stub and there is no `NON_PO→CODED` stub edge, and the brief said not to add edges. This is a behaviour change for any Kaggle invoice: the run stops where the missing tool is, which is the honest place for it. Add the stub edge if you want the old end-to-end sweep back.

### One fix outside the brief's scope

`compute_extraction_confidence` could not ground a date printed with a spelled-out month (`30 Jul 2026`), because it was built against a corpus that only prints slash dates. Every clean generated invoice was therefore escalated to a human for a date plainly on the page - a named month is the *unambiguous* rendering, so the check was escalating the documents it should have trusted most. Fixed with two patterns and a `month_named` flag on `RawDate`; slash dates are still ambiguous, asserted. Without it the live `just run` on a generated invoice would have stopped at `NEEDS_HUMAN_EXTRACTION` and told you nothing about step 4.

**Vocabulary and config gaps found while labelling the fixture** (nothing was added to `ReasonCode` — the enum is unchanged):

- **No `ReasonCode` is emitted by step 4 at all.** The three reads gather facts; the two refusals at `DUPLICATE_CHECKED` (PO not found, PO belongs to another vendor) route on the `match_exception` event and record their reason in `decision_basis`, because assigning a code is the matcher's job and `PO_NOT_FOUND` already exists for the first. The second has no obvious member: it is an identity failure, not a match variance, and `VENDOR_TAX_ID_MISMATCH` is about the vendor's own id rather than a PO belonging to someone else. **Worth a new member when `compute_match` is written** - something like `po_vendor_mismatch`. Not added here, per the brief.
- **The `$50` unmatched-charge rule does not exist in `config/guardrails.v1.yaml`.** There is no threshold for an incidental charge at all. The `freight_small` / `freight_large` pair is built and labelled against it, so the fixture is already asserting a rule Day 3 has to write. This is the one place the fixture is ahead of the config.
- **No `ReasonCode` for an unmatched incidental charge.** `LINE_NOT_ON_PO` is used for the freight line and fits, but it reads as "a line that should have been on the purchase order" rather than "carriage nobody ordered". Worth a member of its own if the distinction routes to different people.
- **No `ReasonCode` for hidden or invisible text specifically.** `SUSPICIOUS_DOCUMENT_CONTENT` covers the `hidden_text` variant and is the right level of generality for now.
- **`RECEIPT_PARTIAL` is unreachable from this fixture**, because the clean invoice bills what arrived rather than what was ordered. It becomes reachable with the second-invoice-against-one-PO case, which is the next document worth generating.

## Data

- `data/synthetic/kaggle/invoices/` — 100 born-digital one-page PDFs (Indian GST-style, INR, ~3 line items, no PO numbers, no due dates), ground truth in `batch_1.csv` (`json_data` column). Golden set.
- `data/synthetic/hf_mychen76/` — ~50 real photographed invoice/receipt PNGs (scan path). `data/synthetic/hf_rvlcdip/` — ~20 low-res scanned invoices.
- `data/real/` — empty (owner may add personal invoices).
- `data/generated/` — `seed_manifest.json`, `receipts.json`, and now `invoices/`:

  ```
  data/generated/invoices/
    manifest.json                     every file with its sha256, variant, po_number, invoice_number
    AP-SEED-001/
      clean/              invoice.pdf  truth.json
      price_plus_3pct/    invoice.pdf  truth.json
      qty_over_received/  invoice.pdf  truth.json
      freight_small/      invoice.pdf  truth.json
      freight_large/      invoice.pdf  truth.json
      hidden_text/        invoice.pdf  truth.json
    ... AP-SEED-002 .. AP-SEED-010
  ```

  10 POs x 6 variants = 60 invoices: 27 expected MATCHED, 33 EXCEPTION, 42 requiring human review. Git-ignored like the rest of `data/`. Regenerate with `just generate` (same bytes every time), then `just ingest`.
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
3. **DONE.** `scripts/generate_invoices.py`: reportlab invoices from `seed_manifest.json` + `receipts.json`, six variants per PO (clean, price +3%, qty over received, freight $25, freight $120, hidden white-text "update bank account"), each with a `truth.json` validated as `GeneratedInvoiceTruth`. 60 invoices, 121 files, byte-identical on every run. `just generate [--only PO] [--variants a,b] [--dry-run]`, then `just ingest`.
   - The clean invoice bills the **received** quantity, so a partially received PO is billed for the part that arrived. The PO that received nothing is billed in full and is still an EXCEPTION - a correct invoice for goods that have not arrived is the only document in the fixture that is internally perfect and must still be held.
   - **`config/sandbox_vendor_master.yaml` is new and now defines the vendors.** The seed manifest carries no country and no postal address because QuickBooks is never told either, so both scripts read the master and join to the manifest on `display_name`. `seed_sandbox.py` builds `VENDORS` from it; the loader is `src/ap_agent/vendor_master.py`. Countries were read off the tax identifiers already in the fixture (15-char GSTIN → IN, `NN-NNNNNNN` EIN → US), not chosen. **Adding a vendor means adding it there, and the order of the file numbers the POs.**
4. **DONE except `compute_match`.** Re-tested live on 2026-09-12; both runs reach `CLOSED`. The two readings + confidence are wired on INGESTED→EXTRACTED→VALIDATED, and the three reads that feed the match are now real and wired too:
   - **`lookup_vendor`** — the vendor master YAML. Tax id exact, then normalised name exact, then none. An ambiguous name is `none` with the candidates listed, not a ranking. Runs at `VALIDATED`; `none` routes to `NEW_VENDOR` and the run halts there, because no stub edge leaves that state.
   - **`get_purchase_order`** — QuickBooks via the existing client, filtered by `DocNumber`. Explicit mapping, everything else dropped. Not found is `None`; a client error raises.
   - **`get_receipts`** — `data/generated/receipts.json`, path from settings. Unknown PO is an **empty `ReceiptSet`**, never `None`.
   - **`VALIDATED→VENDOR_RESOLVED` stub edge is gone**, and `STUB_HOOKS` is empty: the vendor-locale date rule moved from the stub step into the resolve-vendor step, which is where it belonged.
   - At `DUPLICATE_CHECKED`: no PO reference → `NON_PO` (real); PO missing or belonging to another vendor → `EXCEPTION`; otherwise both snapshots land on the record and the invoice advances to `MATCHED` through the remaining stub edge. **`compute_match` and `find_duplicates` are still stubs — the owner writes those.**
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
