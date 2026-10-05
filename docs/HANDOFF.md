# HANDOFF — ap-agent

_Last updated: 2026-10-05. Last code change 2026-09-16 (Day 3, step 2), verified live 2026-10-05. Update this file at the end of every working day._

## Start here — catching up a new session

Read these four files, in this order, before changing anything:

1. **`CLAUDE.md`** — the seven non-negotiable rules, the conventions, and what is
   stubbed. Loaded into every session automatically; read it anyway, because the
   "what is stubbed" section is the fastest true picture of the repo.
2. **This file** — state of every component, the 7-day plan, and where the plan
   has actually got to. The section *Where we are in the plan* is the one-screen
   answer.
3. **`docs/DECISIONS.md`** — every decision with the alternative it beat, newest
   first. Read at least the two most recent sections (`2026-09-16 — the
   three-way match` and `2026-09-15 — guardrails as config`). A decision
   recorded here is settled; do not re-derive it.
4. **`ap-invoice-agent-architecture-and-stack.md`** — the original design, where
   it still exists. Treat as established. Where this repo diverges from it, the
   divergence is recorded in DECISIONS.md with a reason.

Then run `just lint && just typecheck && just test` once. It should be green at
**1385 tests**. If it is not, that is the first thing to fix and nothing below is
trustworthy until it is.

**Gap in the record:** there was a ~3-week pause between 2026-09-16 and
2026-10-05 with no commits. Nothing changed in the repo during it. `git log
--oneline -10` is the fastest confirmation.

**The matcher has been verified live**, on 2026-10-05, on three invoices that
landed in three different places. Details in `docs/EXTRACTION_LOG.md` under
*2026-10-05 - the matcher, live, three runs*. You do not need to spend credit
re-confirming it works; the next live run should be spent on whatever is built
next.

## Purpose (three sentences)

`ap-agent` is an accounts-payable invoice-processing workflow: intake → extraction → validation → vendor resolution → duplicate check → PO match → approval → post → schedule, with a deterministic state machine, an LLM in exactly two seats (document extraction; exception explanation), durable human-in-the-loop approvals, and a tamper-evident audit trail. It is a 7-day self-directed build intended as a portfolio project for Forward Deployed Engineer applications at large companies. The owner is a full-stack engineer new to Python and AI tooling; explanations should map Python/AI concepts to JS/TS equivalents when useful.

## Ground rules for any assistant continuing this work

- The full design rationale is in `ap-invoice-agent-architecture-and-stack.md` (attached separately). Treat it as established; do not re-derive.
- Non-negotiables live in `CLAUDE.md` (seven rules: extraction model has no tools; bank details never from the invoice; no pay/update-vendor/delete tools; every transition logged; idempotency keys on ERP writes; never commit `data/`; don't implement loop/matching/guardrails/audit-chain logic unless explicitly asked).
- The owner reviews and can explain every line of the loop, matching, guardrails and audit chain. Claude Code is used for scaffolding, tool boilerplate, reviews, and — when the owner asks for it in that session, as CLAUDE.md rule 7 requires — the implementation itself, always through tightly scoped prompts and always reviewed piece by piece.
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
- `src/ap_agent/matching/` + `tools/compute_match.py` — **real**, and the thing the rest of this exists to protect. `pairing.py` decides which invoice line bills which ordered line (printed ref first, normalised description second, ambiguity pairs nothing); `engine.py` decides whether they agree. Pure: no I/O, no clock, no model, and it never raises - an invoice it cannot make sense of is a `MatchResult` with reasons on it. **Quantities are compared to what was *received*, never `qty_ordered`**; every two-legged tolerance requires both legs; no free-text field is read or written. 245 tests, including a sweep of all 60 generated invoices against their truth files.
- `src/ap_agent/text.py` — `normalise_vendor_name`, moved out of `tools/lookup_vendor.py`. One normaliser, shared by vendor resolution and line pairing: two would agree until one was edited, and then they would disagree about which line was being billed.
- `scripts/pull_hf_datasets.py`, `scripts/index_invoices.py` → `data/index.csv` (regenerate with `just ingest`).
- `scripts/generate_invoices.py` — real: 60 labelled invoice PDFs from the seeded POs, six variants each, into `data/generated/invoices/`. `Canvas(invariant=1)` and no clock anywhere, so two runs are byte-identical - a hash that moves means the generator changed, not that a document arrived. Truth files are `GeneratedInvoiceTruth` (`contracts/generated.py`), and a test asserts `InvoiceExtraction(**truth.expected.model_dump())` passes the arithmetic validator with no flags. 43 tests, all against real rendered files in `tmp_path`.
- `src/ap_agent/vendor_master.py` + `config/sandbox_vendor_master.yaml` — the vendors, in one file, read by both `seed_sandbox.py` and `generate_invoices.py`. Carries the country and the postal address, which the seed manifest does not because QuickBooks is never told them. Fails loudly on a missing vendor or a missing field. 21 tests.
- `integrations/qbo/auth.py` (refresh-token manager with rotation persistence), `integrations/qbo/client.py` (httpx wrapper: get/query/create, retries on 429/5xx, never logs tokens).
- Loop fixes applied: step budget sized to the real path; `MATCHED→CODED` marked TEMP STUB and listed in the stub-edges test; extraction audit `output_ref` is `sha256:` of the serialized extraction; per-invoice cost recorded in DECISIONS.md.
- CLI: `just extract <path>`, `just run <path> [--received-at YYYY-MM-DD]`, `just confidence <path> [...]`, `just ingest`, `just seed [--dry-run]`, `just generate [--only <po>] [--variants a,b,c] [--dry-run]`, `just vendor "<name>" [--tax-id X]`, `just receipts <po>`, `just po <po>`, `just probe-schema`.
  - `just vendor` and `just receipts` read committed/local files and call nothing. **`just po` is the only one that makes a live QuickBooks call**, and it spends no tokens.
  - `--from-extraction` replays saved readings and makes **no** model calls — use it whenever the question is about the check rather than the models. `--out` writes the readings alongside the verdict so any run is replayable.
  - `run` reports `steps` (state moves) and `audit rows` separately: the extraction step is one step and four rows.
- Docs: README, ARCHITECTURE.md (state + trust-boundary diagrams), DECISIONS.md, CLAUDE.md.

**Stubs:** all tools except `ingest_document`, `extract_invoice_vision`, `extract_invoice_text`, `compute_extraction_confidence`, `lookup_vendor`, `get_purchase_order`, `get_receipts` and `compute_match`; evals fixtures. `scripts/generate_invoices.py` and the guardrail config loader are real.

**Stub edges remaining (10):** `VENDOR_RESOLVED→DUPLICATE_CHECKED` (find_duplicates), `NON_PO→CODED` (propose_gl_coding), `MATCHED→CODED`, `CODED→PENDING_APPROVAL`, **`PENDING_APPROVAL→APPROVED` (the dangerous one — delete it the moment `request_approval` exists)**, `APPROVED→POSTED`, `POSTED→SCHEDULED`, `SCHEDULED→PAID`, `PAID→RECONCILED`, `RECONCILED→CLOSED`. `STUB_HOOKS` is now empty: no stub step does anything.

**`DUPLICATE_CHECKED→MATCHED` is gone.** `compute_match` is real, so an invoice leaves that state on `match` or `match_exception` or does not leave it. The edge it used to travel advanced every invoice to MATCHED regardless of what its numbers said.

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

2. **A non-PO invoice routes to `NON_PO`, which is a different pipeline.** It briefly stopped dead there, because `propose_gl_coding` is a stub and nothing left that state. A `# TEMP STUB` `NON_PO→CODED` edge was added on 2026-09-12 so the Kaggle corpus - which cites no purchase orders at all - can exercise the rest of the pipeline. Listed in the stub test like the rest of the scaffolding.

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

- **2026-10-05, three live runs with the matcher in the loop (EXTRACTION_LOG.md).** The first live runs since `compute_match` existed. `AP-SEED-001/clean` reached `MATCHED`; `price_plus_3pct` and `AP-SEED-010/qty_over_received` reached `EXCEPTION` with the codes their truth files declared. **Cost is now a real number: ~$0.03 per invoice**, two model calls, stable across all three. QuickBooks still authenticates after three weeks idle (token rotated on the first call).
- The AP-SEED-010 run is the one to cite. It bills 2 of a line that ordered 11 and received 0 - inside the authorisation, outside the delivery - and a two-way match would have paid it.

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
   - At `DUPLICATE_CHECKED`: no PO reference → `NON_PO` (real); PO missing or belonging to another vendor → `EXCEPTION` carrying `PO_NOT_FOUND` or `PO_VENDOR_MISMATCH`; otherwise both snapshots land on the record, `compute_match` runs, and the invoice moves to `MATCHED` on `match` or `EXCEPTION` on `match_exception`. **`find_duplicates` is still a stub — the owner writes that one.**
   - Change of design from the original plan: an ambiguous slash date does **not** fail extraction. Both readings stay on the record and are settled later — by the receipt window at `VALIDATED`, then by the vendor's country at `VENDOR_RESOLVED`. Rationale in DECISIONS.md, 2026-09-10.
5. `verify_vendor_external` web check for NEW_VENDOR (domain age, registry hit, lookalike distance) — the scoped "model decides when to call" tool.
6. End-of-day target: a generated PO-matched invoice and a Kaggle invoice both run through the loop with confidence in the audit trail; the PO-matched one reaches the match step with real PO + receipt data in context.
7. Off-keyboard: book the customer interview.

## Day 3 — steps 1 and 2 done, step 3 is next

1. **DONE.** Guardrails as versioned YAML with a typed loader.
   - `config/guardrails.v1.yaml`, `config_version: guardrails_v1`. Five sections: `tolerances`, `approval_matrix`, `input_validation`, `output_filter`, `hard_prohibitions`. The price band and the unmatched-charge rule are each a **pair that must both hold** - an absolute cap alone is a licence on a small order, a percentage alone is one on a large order.
   - `GuardrailConfig` in `contracts/guardrails.py`, frozen and `extra="forbid"`, Decimal for money. `load_guardrails(path=None)` in `guardrails/config.py`, cached per path, **no defaults anywhere** - a missing value is a startup error, and the version must match the filename.
   - `MAX_INVOICE_AGE_DAYS` is gone from `loop/dates.py`. `resolve_date_by_receipt_window` takes the window as a required argument and the loop reads it from config, so the number a run applied is the one its `config_version` names.
   - The config and the generated fixture are tested **against each other**: a 3% overcharge breaching a 2% band, two units over a 0% band, and $25/$120 against the $50-and-2% unmatched-charge rule. Neither can drift alone. One test records *which* half is doing the work today: $120 is caught by the absolute cap on every seeded order, and the percentage arm alone would pass it - so if a smaller order is ever seeded, that test flips and says so.
   - `just probe-schema` added (not run): sends each exported schema with a dummy document and reports accepted or refused.
2. **DONE, 2026-09-16.** `compute_match`, written in four reviewed pieces. This is the function that decides whether money is owed, and the detail below is what a new session needs so it does not have to re-read the module to know what was decided.

   **Where the code is.** The reasoning is `src/ap_agent/matching/` - `engine.py:137` is `compute_match(extraction, po, receipts, config) -> MatchResult`, and `pairing.py:128` is `pair_lines`. The tool wrapper is `src/ap_agent/tools/compute_match.py:72`, same name, different job: it unpacks the tool envelope, loads the guardrails, checks the version, and calls the real one. **Read `engine.py`; the loop calls the wrapper.**

   **The property that matters most: quantities are compared to what was *received*, never to `qty_ordered`.** An order for 11 monitors that has delivered none is an order a vendor can bill 2 against and look correct against the paperwork, because 2 is well inside what was authorised. Against the goods receipt it is a bill for two monitors nobody has. Two fixtures exist to fail the matcher if anyone changes this: `AP-SEED-010/qty_over_received` bills 2 of a line that ordered 11 and received 0, and `AP-SEED-009/qty_over_received` bills 9 of a line that ordered 9 and received 7. Both pass a two-way match. If a change makes those two tests go green by comparing to the order, the change is wrong.

   **The other four properties**, each with its own test:
   - **Every two-legged tolerance requires both legs.** Price and unmatched charges each hold a percentage and an absolute cap, and both must pass, so the tighter one binds. Either alone is a licence somewhere: on a $272,100 order 2% is $5,400 and any freight passes; $50 on a $12 line is four times the line.
   - **It never raises.** An invoice it cannot make sense of is a `MatchResult` carrying reason codes. A raise would travel up as an *error* - something broken, to be retried - when the correct handling is a routing decision. An empty invoice, an order with no lines, a line priced at zero: all return a result.
   - **No free text enters or leaves.** Line descriptions are join keys in `pairing.py` and nothing else; `suspicious_text`, `remit_to_display`, `payment_terms` and `vendor_name` are never read. `MatchResult` has no string field a vendor controls - lines are identified by PO line number and by position in the extraction. A test serialises all 60 fixture results and asserts no description appears.
   - **Pure.** No I/O, no clock, no model, no ERP, so a past decision replays identically under its own `config_version`.

   **Design calls worth knowing before editing it:**
   - Quantity is decided per *purchase-order line*, price per *invoice line*. Two invoice lines of 2 against a receipt of 3 are unremarkable apart and an over-bill together, so quantities sum; prices stay their own, because averaging would let a correct line pay for an inflated one. The summed quantity then appears on **every** row of the group - the decision was made on the sum, and a row showing only its own 2 would make the exception unexplainable.
   - `RECEIPT_MISSING` and `QUANTITY_OVER_TOLERANCE` both fire on a line that received nothing. Two different facts, and a reviewer needs both.
   - An ordered line nobody billed raises **nothing**. A partial invoice against a partial delivery is normal; the row exists only so a reviewer sees what is outstanding.
   - Identity stays in the loop. The matcher is given no vendor at all, because "does this order belong to this vendor" is a comparison of ERP ids. The loop answers "may these be compared"; the matcher answers "do they agree".
   - Rounding happens *after* every comparison. `Money` refuses to round so a transcribed amount cannot be altered; a derived delta is the matcher's own arithmetic and must be readable, but a tolerance evaluated on a rounded figure would make half a cent the difference between paying and not.
   - `PARTIALLY_RECEIVED` is a billable status alongside `OPEN`. QuickBooks emits only Open/Closed so it is theoretical today.

   **What the loop does now.** `DUPLICATE_CHECKED` fetches the order and the receipts, runs `compute_match`, writes its own audit row with the result's sha256 as `output_ref`, and moves the invoice to `MATCHED` on `match` or `EXCEPTION` on `match_exception`. The decision's `decision_basis` carries the reason codes **and** the config version together, because a code without the ruleset that raised it cannot be re-checked later. The purchase-order vendor check now raises `PO_VENDOR_MISMATCH` on a `MatchResult` instead of writing a prose sentence. `InvoiceRecord` gained `match_result`; `RunContext` gained `compute_match`.

   **`DUPLICATE_CHECKED → MATCHED` on `stub_ok` is deleted.** It advanced every invoice regardless of its numbers.

   **Tests: 252** (`tests/matching/`, plus `tests/tools/test_compute_match.py`). 39 hand-written cases against the real `config/guardrails.v1.yaml` rather than a double, 19 for pairing, and a sweep of all 60 generated invoices against their truth files.

   **Verified live on 2026-10-05**, which the test suite structurally cannot do - every test fakes both model seats and feeds the truth file in. Three invoices through the real models and the real QuickBooks sandbox: `AP-SEED-001/clean` → `MATCHED` with no codes, `AP-SEED-001/price_plus_3pct` → `EXCEPTION` with `price_over_tolerance`, and `AP-SEED-010/qty_over_received` → `EXCEPTION` with `receipt_missing` and `quantity_over_tolerance`. All three agree with their truth files, all three chains verify. **~$0.03 per invoice**, two model calls each. Full write-up in `docs/EXTRACTION_LOG.md`.

   **One thing to understand before trusting that sweep.** `expected_reason_codes` in a `truth.json` is the *planted defect's signature* composed with the order's own state - not an exhaustive verdict. `AP-SEED-010/clean` declares `receipt_missing` alone, and a correct matcher also reports `quantity_over_tolerance`, because that invoice bills 9, 11 and 10 units of three lines that received nothing. So the sweep asserts: a declared MATCHED must produce **zero** codes, a declared EXCEPTION must contain **every** code the generator planted, and the one place the two legitimately differ is asserted by name in `test_where_the_matcher_says_more_than_the_truth_file`. The comparison is also filtered to matcher-owned codes, because `hidden_text` carries `suspicious_document_content`, which belongs to the output filter.

   **Side effect worth knowing:** `normalise_vendor_name` moved from `tools/lookup_vendor.py` to `src/ap_agent/text.py`. Not optional - importing it from `tools/` made the matcher import the whole tool package, which imports the matcher. One normaliser shared by vendor resolution and line pairing; two would agree until one was edited.

   **Three loop test fixtures changed**, each quietly wrong in a way nothing had checked: the loop's "clean" invoice claimed $20 of tax on a line with no tax rate, and the ambiguous-date invoice was in INR against a USD order. Harmless until something compared a header to its lines.

3. **NEXT — apply the guardrails that are configured and unread.** The patterns and limits exist in `config/guardrails.v1.yaml` and **nothing reads them**; see *What is configured but not yet applied* below. The fixture already proves the hole: `hidden_text` plants white 4pt type telling the reader to change the vendor's bank account, its arithmetic is perfect so the matcher passes it correctly, its truth file says `expected_human_review: true`, and `tests/loop/test_generated_invoice_end_to_end.py::test_the_hidden_text_variant_reaches_the_same_place_on_the_numbers` asserts it reaches `CLOSED` with a comment naming the gap. Shape of the work: a function that runs the `output_filter` patterns over the extraction's string fields before anything downstream reads them, a route to a human when one matches, **nothing ever silently rewritten** (`never_auto_fix: true`, and stripping an IBAN destroys the evidence that somebody tried), `input_validation` enforced in `ingest_document` before any model call, and that `hidden_text` test flipping from "reaches CLOSED, which is the gap" to "reaches a person, which is the point". No live API calls needed.

### What is configured but not yet applied

Worth being explicit, because a config file reads like a working control:

- **`output_filter`** - patterns are declared and unit-tested against samples, but nothing in the loop runs them over model output yet.
- **`input_validation`** - `ingest_document` enforces none of these limits; `max_pages`, `max_file_bytes` and the MIME allowlist are declared and unread.
- **`approval_matrix`** - `request_approval` is still a stub, so no tier or rule is consulted. `PENDING_APPROVAL → APPROVED` is still the dangerous stub edge. Note that the matrix's `match_exception` rule now has something to fire on: `compute_match` produces the reason codes it keys off.
- Of the tolerances, **all but `qty_under_billing_pct` are now read** by `compute_match`: both price legs, `qty_over_billing_pct`, both unmatched-charge legs, `tax_variance_abs` and `rounding_tolerance_abs`. `invoice_max_age_days` is read by the date rule and the staleness check. `qty_under_billing_pct` is unread because under-billing is tested one-sidedly - a shortfall is the vendor's loss and never an exception - so the value is declared and the rule needs no number.

## Where we are in the plan

**Day 3 of 7, two-thirds through it.** Days 1 and 2 are done; Day 3 has three
steps and the first two are finished.

The honest summary of the build: an invoice can walk the first six of nine doors
on its own merits, and the rest are still propped open with scaffolding.

**Real, end to end:** intake (`ingest_document`), two independent readings
(Sonnet on the image, Haiku on the text layer), the agreement-and-grounding
check that scores them (`compute_extraction_confidence`, pure code, no model),
the arithmetic validator, ambiguous-date resolution (receipt window, then vendor
locale), vendor resolution against the master (`lookup_vendor`), the purchase
order from QuickBooks (`get_purchase_order`), the goods receipts
(`get_receipts`), and **the three-way match** (`compute_match`). The state
machine, the transition table, and the hash-chained JSONL audit trail are real
throughout.

**8 of 16 tools are real.** Stubs: `find_duplicates`, `propose_gl_coding`,
`request_approval`, `create_bill`, `classify_exception`, `notify`,
`mark_ready_for_payment`, `verify_vendor_external`.

**10 stub edges remain** in `STUB_TRANSITIONS`, all after `MATCHED`. They are
enumerated in `states/machine.py` and asserted in
`tests/states/test_stub_transitions.py`, so deleting one is a deliberate act
with a failing test to confirm it.

### The three holes that matter, ranked

1. **A machine still approves invoices.** `PENDING_APPROVAL → APPROVED` is a
   stub edge whose own comment calls it THE DANGEROUS ONE. The pitch for this
   whole project is deterministic money decisions plus a real human in the loop,
   and the human is currently a `stub_ok`. The approval matrix sits in the
   guardrails file with its tiers, its two-approver rules and its callback hold,
   serving an approval step that does not exist. This is the gap a reviewer
   finds first.
2. **A configured guardrail with no consumer.** `output_filter` and
   `input_validation` are declared, reviewed and unread - `grep` finds zero
   consumers outside `contracts/guardrails.py`. The `hidden_text` fixture proves
   it is exploitable today. This is Day 3 step 3 and it is the smallest of the
   three.
3. **The second LLM seat does not exist.** The architecture says a model sits in
   exactly two chairs - reading documents, and explaining exceptions in prose.
   Only the first is occupied; `classify_exception` is a stub. As of 2026-09-16
   there are finally real `MatchResult`s for it to explain, so its input exists.

### Recommended order from here

The guardrail first (Day 3 step 3): small, closes a hole the fixture proves is
open, applies config already argued about rather than deciding anything new, and
it is the half of Day 3 where the guardrails start doing something. Then
`request_approval` and the deletion of the dangerous edge, which deserves a
session of its own. Then `classify_exception`, which is the one that spends
tokens and is better done once approval exists so the explanation has somewhere
to land. `find_duplicates` is cheap and deterministic and would delete another
stub edge whenever it fits.

Neither of the first two needs a live API call.

## After Day 3 (report mapping)

- Day 4: Postgres for state/extractions/vendor profiles/dedup/config; approval pause + resume (Pydantic AI + DBOS or LangGraph checkpointer); kill-and-resume test with exactly one bill.
- Day 5: append-only audit table with hash chain in Postgres; OTel spans → self-hosted Langfuse; trace_id on every audit event.
- Day 6: real invoice stream; golden set grows from human corrections.
- Day 7: DeepEval + pytest golden-set evals in CI; report touchless rate, exception rate by reason, field accuracy, cost per invoice, time in PENDING_APPROVAL.

## Open questions

- Day 4 framework: Pydantic AI + DBOS vs LangGraph — decide after building the approval pause in both for one state.
- Whether to add a fine-tuned/small model as a second reader later (cost/privacy), vs text layer + OCR service.
- Sonnet 5 adaptive thinking is on by default; consider `output_config.effort` low for extraction once accuracy is established.
