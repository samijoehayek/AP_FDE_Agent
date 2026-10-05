# Extraction log

## 2026-09-09 — extract_v1, Claude Sonnet 5, 6 documents

Load-bearing fields = vendor_name, invoice_number, invoice_date, currency, total.

| File                    | Kind | Load-bearing wrong                                  | Line-item errors | Notes                                                                                                                                            |
| ----------------------- | ---- | --------------------------------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| kaggle/invoice_51109301 | pdf  | 0/5                                                 | 0/3              | clean; date 03/07/2023 read DD/MM (correct)                                                                                                      |
| kaggle/invoice_51109302 | pdf  | 0/5                                                 | 0/6              | clean                                                                                                                                            |
| kaggle/invoice_51109303 | pdf  | 0/5                                                 | 0/4              | clean                                                                                                                                            |
| kaggle/invoice_51109304 | pdf  | 0/5                                                 | 0/6              | clean; date 07/10/2023 read DD/MM (correct)                                                                                                      |
| kaggle/invoice_51109305 | pdf  | **1/5 — invoice_date 2024-09-03, truth 2024-03-09** | 0/6              | date 09/03/2024 read MM/DD; same vendor/layout as 301/304 where DD/MM was chosen                                                                 |
| hf_mychen76/00000       | png  | n/a — no truth                                      | n/a              | judged by consistency: arithmetic ok, decimal-comma normalized, unambiguous date; remit_to_display captured an IBAN (must never flow downstream) |

Totals (Kaggle, with truth): 24/25 load-bearing fields, 25/25 line items, 5/5 arithmetic.
Formatting-only diffs ignored: thousands separators, trailing zeros, newline vs comma in addresses.

## 2026-09-10 — confidence check, four live runs

Two readings per run: `claude-sonnet-5` over the page image, `claude-haiku-4-5` over the
PDF text layer. The check between them is pure code.

| Run | File | `--country` | invoice_date read | verdict | auto_ok |
| --- | --- | --- | --- | --- | --- |
| 1 | 51109305 | IN | 2024-03-09 | `date_resolved_from_locale` | yes |
| 2 | 51109305 | — | **2024-09-03** | `ambiguous_date_unknown_locale` | no (before the loop wiring) |
| 3 | 51109301 | — | 2023-07-03 | `ambiguous_date_unknown_locale` | no (before the loop wiring) |
| 4 | 51109301 | IN | 2023-07-03 | `date_resolved_from_locale` | yes |

Every other load-bearing field scored 1.00 (agreed and grounded) on all four runs, and
Indian digit grouping (`1,67,560.00`) grounded without the check knowing that convention.

**The finding.** Runs 1 and 2 are the same file, the same model and the same prompt about
twenty minutes apart, and the vision model returned **2024-03-09** in one and
**2024-09-03** in the other. This is the 51109305 bug from 2026-09-09 reproducing live,
and it settles what kind of bug it is: not a model that reads the date wrongly, but a
document that does not say which reading is meant. The check refused both times; the
locale rule gave the same answer both times.

Runs 2 and 3 predate the loop wiring, where an ambiguous date became an open date carried
forward rather than a failure. Under the current rules both would be `auto_ok=True` with
`date_verdict=ambiguous`, and the date would be settled by the receipt window at
`VALIDATED` or by the vendor's country at `VENDOR_RESOLVED`.

Saved verdicts: `data/confidence/*.json` (git-ignored).

## 2026-09-12 — step 4 live check, two runs

The three reads (`lookup_vendor`, `get_purchase_order`, `get_receipts`) wired into
the loop, checked against a real invoice from each corpus. Two model calls per
run. Trails are under `data/audit/` (git-ignored) at the ULIDs below.

| Run | Document | Final state | Steps | Rows | Result |
| --- | --- | --- | --- | --- | --- |
| 1 | `kaggle/invoice_51109301.pdf` | `NON_PO` | 6 | 12 | **as designed** |
| 2 | `generated/AP-SEED-001/clean/invoice.pdf` | `INGESTED` | 1 | 2 | **failed extraction — two real defects** |

Run 1 trail: `data/audit/01M29B6MZM49NH7DN20Y2KK41P.jsonl`
Run 2 trail: `data/audit/01M29BD4HDSX7C7Y7NBW2080FT.jsonl`

### Run 1 — the ambiguous date is finally settled end to end

`03/07/2023` is 3 July or 7 March and the page does not say. The trail now shows
the whole resolution as three rows inside one step:

```
[6] tax_id_exact                     printed='TechVision Distributors Pvt Ltd',
                                     basis=tax_id_exact, vendor_id=58, country=IN
[7] date_resolved_from_vendor_locale candidates=2023-03-07|2023-07-03,
                                     vendor_country=IN, chose=2023-07-03
[8] resolve_vendor                   -> VENDOR_RESOLVED
```

Three things this confirms live, that were previously only asserted in tests:

* The vendor master resolves a real extracted vendor on the **tax-id** tier.
* The country it returns settles a date the receipt window could not - and row 7
  is attributed to `DATE-RESOLVE@v1`, a **rule**, not to the lookup it sits inside.
* Row 10 (`no_po_reference` -> `NON_PO`) fetched nothing at all. The Kaggle
  documents cite no purchase order, so QuickBooks was never called.

It stops at `NON_PO` because `propose_gl_coding` is a stub and no stub edge
leaves that state. The run stops where the missing tool is.

**Correction to the prediction made before the run:** 12 rows, not 11. The
date-resolution row is the twelfth, and it only exists when a date is actually
ambiguous - which the estimate forgot.

### Run 2 — two defects, and the first one is worse than it looks

Extraction failed on the *second* reading (`claude-haiku-4-5` over the text
layer). The vision read was fine. Eight validation errors, of two kinds:

**A. Money arrives with thousands separators and the contract cannot parse it.**

```
subtotal: Input should be a valid decimal [input_value='272,100.00']
```

This is **not** a generated-fixture problem. The Kaggle PDFs print `74,120.00`
and `667,080.00` too. Run 1 survived only because `claude-sonnet-5` happened to
strip the commas and `claude-haiku-4-5` happened to strip them on that document
as well. **Whether an invoice extracts at all currently depends on which way a
model rounded a formatting decision**, which means the Kaggle corpus has been
passing by luck, not by design.

The model is not wrong here. `272,100.00` is what the page says, and reporting it
verbatim is defensible. The contract is what cannot read it. The fix belongs on
the `Money` / `UnitPrice` / `Quantity` annotations in `contracts/common.py`: a
`mode="before"` validator that strips digit-group separators, which is the same
normalisation `_THOUSANDS` already performs in
`compute_extraction_confidence` for grounding. One implementation, not two.

**B. `LineItem.unit` comes back empty and is rejected.**

```
line_items.0.unit: String should have at least 1 character [input_value='']
```

The generated invoice prints no unit-of-measure column, deliberately: the seed
manifest has no UoM and inventing one would be a fact the fixture does not
support. The model has nothing to read and returns `""`; `unit` requires at
least one character, so the whole extraction is lost over a field nothing
compares. Plenty of real invoice lines carry no unit either.

Two honest fixes, and they are not equivalent:
* Make `unit` optional (`str | None`, coercing `""` to `None`). Matches what
  documents actually do, and loses nothing - no rule reads `unit` today.
* Print a unit column on generated invoices. Narrower, and leaves every real
  invoice without one still failing.

**Neither defect is in step 4's code.** Both are in the extraction contract, and
both were invisible until a document with grouped thousands and no unit column
went through the whole loop - which is exactly what the generated fixture was
built to do.

### Not run

`just po AP-SEED-001` was blocked by this environment's permission classifier as
a production read, so **the QuickBooks purchase-order shape is still
unverified**. `tests/tools/qbo_purchase_order_response.json` remains built from
Intuit's documentation rather than captured from a live response, and
`get_purchase_order` has not made a real call. Run 2 failed before reaching it.

The two things that check hangs on: `vendor_erp_id == "58"` for AP-SEED-001, and
the PO's line order matching `receipts.json` - the receipts join is positional.

## 2026-09-12 — re-test after the six fixes

Same two documents, after the two contract defects and four trail defects were
fixed. Four model calls. Full row-by-row trails and the raw purchase order are
under `data/retest/2026-09-12/` (git-ignored).

| Run | Final state | Steps | Rows | Chain | Tokens in/out | Cost |
| --- | --- | --- | --- | --- | --- | --- |
| `generated/AP-SEED-001/clean` | `CLOSED` | 14 | 20 | verifies | 11,675 / 1,286 | **$0.0290** |
| `kaggle/invoice_51109301` (`--received-at 2023-09-10`) | `CLOSED` | 14 | 19 | verifies | 11,808 / 1,998 | **$0.0364** |

Priced at the verified rates: Sonnet 5 $2/$10 per million, Haiku 4.5 $1/$5,
`model_pricing.yaml` version `2026-09-12-verified`. The first draft of that file
said $3/$15 for Sonnet - the September increase Anthropic then cancelled - which
would have overstated both runs by half.

Both defects that stopped the generated invoice are gone: the grouped thousands
parse, and a line with no unit column validates. Every `output_ref` in both
trails is a `sha256:` content address, every model row carries a `cost_usd` and
the pricing version, and no row carries a retry (the SDK does not expose its
count, so the field reads 0 rather than being measured).

### `just po AP-SEED-001` — run at last, and the shape is right

`vendor_erp_id` is `"58"`, line 1 is Laser Printer Mono and line 2 is 27in IPS
Monitor, and `receipts.json` carries the same two under `line_no` 1 and 2. The
join holds. **The mapping needed no changes**, and
`tests/tools/qbo_purchase_order_response.json` is now a verbatim capture rather
than a reconstruction from Intuit's documentation. The real response differs from
the hand-built one only by extra keys the mapping already drops.

### Two `400 Schema is too complex` failures, and what they cost

Before either run succeeded, the vision seat was refused twice. Neither cost
tokens - the request is rejected before inference - but both cost a round trip
and the second was avoidable.

The cause was a single property leaving the schema's `required` array. Giving
`LineItem.unit` a default so an empty string could validate is what took it out.
The resulting schema was *smaller by every count available locally* - 104 nodes
against 104, 19 properties against 19, and at one intermediate point 76 nodes and
7,204 bytes against 7,223 - and was refused anyway. **Structured outputs appears
to expand an optional property into something substantially larger than a
required one, and nothing measurable on this side predicts it.**

The fix keeps `unit` in `required` through
`LineItem.__get_pydantic_json_schema__`, so the model must still supply the
field and may supply `""`, while Python callers may omit it.

The first refusal had a second, independent cause found on the way: a
`BeforeValidator` on the money annotations made pydantic emit `decimal_places`
and `max_digits` as raw keywords, which are not JSON Schema. That coercion moved
to a model-level validator on `InvoiceExtraction` and `LineItem`, where it
changes no annotation and therefore no schema.

A test now pins both: the schema declares no non-JSON-Schema keyword, and the
property count stays at 19. Neither would have caught the `required` change -
that one is recorded here because only the API can tell you.

### The one expectation that did not hold

`--received-at 2023-09-10` was expected to let the **receipt window** settle
`03/07/2023` at VALIDATED, with no vendor-locale row. The vendor-locale row
appeared instead.

Both readings - 2023-03-07 and 2023-07-03 - are inside the 365-day window, so
`resolve_date_by_receipt_window` had two survivors and declined, which is what it
is built to do. `MAX_INVOICE_AGE_DAYS` is deliberately generous so the rule
declines rather than eliminating a candidate that might be correct; the vendor's
country settled it one state later. To make the window decide, arrival has to
make exactly one reading impossible: before 2023-07-03, or between 2024-03-07
and 2024-07-02.

## 2026-10-05 — the matcher, live, three runs

The first live runs since `compute_match` was written. Everything before this
faked both model seats and fed the truth file in; these read the actual PDFs and
fetched the actual purchase orders, so they test the one thing the 1385-test
suite structurally cannot - whether a *real* extraction produces numbers the
matcher agrees with.

Three invoices, chosen to land in three different places. All three did.

| invoice | final state | reason codes | steps | rows |
| --- | --- | --- | --- | --- |
| `AP-SEED-001/clean` | `MATCHED` | none | 14 | 21 |
| `AP-SEED-001/price_plus_3pct` | `EXCEPTION` | `price_over_tolerance` | 6 | 14 |
| `AP-SEED-010/qty_over_received` | `EXCEPTION` | `receipt_missing`, `quantity_over_tolerance` | 6 | 14 |

Every verdict matches the `truth.json` declared before the matcher existed.
Every decision row carries `config_version=guardrails_v1` beside its codes. All
three hash chains verify.

**The third run is the one worth keeping.** AP-SEED-010 ordered 11 network
switches and the invoice bills **2**. Two is comfortably inside what was
authorised, so a system comparing the invoice to the *purchase order* calls that
clean and pays it. Nothing has been received against that order, so the goods
receipt says 0, and billing 2 against 0 is a bill for two switches nobody has.
The matcher reported both facts rather than one: nothing arrived, and more than
arrived was billed. This is the three-way match doing something a two-way match
structurally cannot, observed on a live run rather than asserted in a test.

**Both EXCEPTION runs stop at 6 steps, and that is correct.** `EXCEPTION` is a
state no stub edge may leave, so the run halts and logs `awaiting_human`. The
`halt` row is the last one in the file.

### Cost, which is a Day 7 metric and now has a real number

| run | Sonnet (in/out) | Haiku (in/out) | cost |
| --- | --- | --- | --- |
| clean | 7278 / 798 | 4397 / 577 | $0.029818 |
| price_plus_3pct | 7277 / 855 | 4397 / 585 | $0.030426 |
| qty_over_received | 7278 / 1107 | 4393 / 553 | $0.032784 |

**~$0.03 per invoice**, $0.093 for all three, two model calls each. Stable
across the three: the input is nearly identical because the documents are the
same template, and the spread is all in output tokens.

Worth noting for anyone comparing latencies in these trails: the **first** call
of the first run took 87.7 s on Sonnet and 72.0 s on Haiku, against 7.0 s and
5.3 s on the run immediately after. Cold start, not a regression - the two later
runs are the representative numbers.

### What these runs confirmed that nothing had tested

- A real extraction is good enough that a clean invoice comes out clean. The
  matcher is strict, and an extraction off by a cent on any line would have
  raised `arithmetic_inconsistent` or `totals_over_tolerance`. None did.
- **QuickBooks still works after three weeks idle.** The refresh token rotated on
  the first call (`rotated=True`) and the two later runs reused it. Each run
  fetched its PO live from the sandbox.
- The audit trail reads correctly end to end: rows 9-11 of each match step are
  the three tool calls (PO `found`, receipts `received`, matcher `matched` or
  `exception`), row 12 is the decision that moves the invoice, and only the
  decision carries a `to_state`.

Trails are at `data/audit/01M45J5E6PMFGBCKSX6H8GYTX9.jsonl` (clean),
`01M45JAX7577ZGM2MFQQ6V9V1Q.jsonl` (price) and
`01M45JBK3BQPA4QZR9VTJWR730.jsonl` (quantity). Git-ignored, local only.

## Known risks

- **Ambiguous slash dates (day ≤ 12):** ~~model is inconsistent across files~~ **confirmed** — the same file read both ways twenty minutes apart (2026-09-10, runs 1 and 2). Handled: the date stays open with both candidates and is settled by the receipt window, then the vendor's country. Still open when neither is available, which is what the missing `lookup_vendor` costs.
- No ground truth for hf_mychen76 (pull script saved images only; dataset has a text column — add it).
- batch_1.csv `ocred_text` column can serve as the second reading for grounding on the Kaggle set.
