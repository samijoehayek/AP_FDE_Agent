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

## Known risks

- **Ambiguous slash dates (day ≤ 12):** model is inconsistent across files. Resolve from vendor locale in Day 2 confidence check; flag when locale unknown. Seen live on 51109305.
- No ground truth for hf_mychen76 (pull script saved images only; dataset has a text column — add it).
- batch_1.csv `ocred_text` column can serve as the second reading for grounding on the Kaggle set.
