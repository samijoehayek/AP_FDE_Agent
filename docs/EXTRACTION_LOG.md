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

## Known risks

- **Ambiguous slash dates (day ≤ 12):** ~~model is inconsistent across files~~ **confirmed** — the same file read both ways twenty minutes apart (2026-09-10, runs 1 and 2). Handled: the date stays open with both candidates and is settled by the receipt window, then the vendor's country. Still open when neither is available, which is what the missing `lookup_vendor` costs.
- No ground truth for hf_mychen76 (pull script saved images only; dataset has a text column — add it).
- batch_1.csv `ocred_text` column can serve as the second reading for grounding on the Kaggle set.
