# ap-agent

**Problem.** Accounts payable is the place where a document from outside the
company turns into a payment. It is high-volume, rule-bound, and the failure
modes are expensive and asymmetric: a duplicate payment, a price accepted 8%
above the purchase order, a remittance address changed by an invoice that asked
nicely. It looks like an obvious target for an LLM agent, and a naive agent —
one with a `pay_bill` tool and a prompt that says "be careful" — is exactly the
wrong shape for it.

**Approach.** The LLM occupies two seats, both of them narrow. It reads
documents and returns a typed `InvoiceExtraction`, with no tools of any kind.
And it writes the human-readable explanation of an exception that deterministic
code has already found. Everything that decides money — three-way matching,
tolerances, duplicate detection, approval routing — is ordinary Python evaluated
against a versioned config file. Every state change goes through an explicit
transition table and writes a hash-chained audit event. There is no tool that
pays, no tool that edits a vendor record, and no tool that deletes anything;
their absence is a control, and a test fails if one appears.

**Status.** Scaffold. The typed contracts, the state machine, the persistence
layer, the document-ingestion tool, and the data pipeline are implemented and
tested. The agent loop, extraction prompts, matching logic, guardrail
evaluation, and audit hash chain are deliberately stubbed — each is a
`NotImplementedError` under a docstring stating what it will do and the
constraints it must satisfy. See [docs/DECISIONS.md](docs/DECISIONS.md) for the
choices made so far and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the
state and trust-boundary diagrams.

---

## Architecture in one pass

```
document (untrusted)
   │
   ├─ ingest_document ─────────► sha256, page count, text layer, sharpness
   │                             (no interpretation, no model)
   ▼
extraction model  ── NO TOOLS ─► InvoiceExtraction   ← the trust boundary
   │                             (extra="forbid"; no bank details anywhere)
   ▼
deterministic rules ───────────► MatchResult, reason codes, config_version
   │                             (pure functions; replayable; unit-tested)
   ▼
human approval ────────────────► a durable row, not a callback
   │
   ▼
ERP write ─────────────────────► create_bill, with an idempotency key
                                 (creates an unpaid liability; never pays)
```

The explanation model sits beside `MatchResult`, turning reason codes into prose
for the approver's queue. Nothing it writes is read by a rule.

Full diagrams: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Setup

```bash
just setup          # uv sync + pre-commit install
just db-up          # Postgres 18 in Docker
just db-migrate     # alembic upgrade head
just test           # ruff + pyright + pytest
just verify-audit   # prove the audit trail cannot be rewritten
```

The database runs two roles. The application connects as `ap_agent_app`, an
ordinary role, so the revoke that makes `audit_events` append-only actually
binds; migrations connect as the owner `ap_agent`. Pointing `DATABASE_URL` at
the owner silently disables the control, which is why `just verify-audit`
exists and runs in CI.

Requires [uv](https://docs.astral.sh/uv/), [just](https://just.systems), and
Docker. Copy `.env.example` to `.env` before running anything that talks to the
API or the database.

## Commands

| Command | What it does |
| --- | --- |
| `just setup` | Install dependencies and git hooks |
| `just lint` | `ruff check` + `ruff format --check` |
| `just typecheck` | `pyright` in strict mode |
| `just test` | Lint, typecheck, and pytest with coverage |
| `just db-up` / `just db-down` | Postgres via docker compose |
| `just db-migrate` | `alembic upgrade head` |
| `just verify-audit` | Prove `audit_events` refuses UPDATE, DELETE and TRUNCATE |
| `just db-revision "msg"` | Autogenerate a migration from the models |
| `just ingest` | Index every document under `data/` into `data/index.csv` |
| `just pull-data` | Stream sample corpora from Hugging Face |
| `just docs-diagram` | Regenerate the state diagram from the transition table |

## Data: what is real and what is synthetic

`data/` is git-ignored in its entirety. Real invoices contain personal data and
supplier commercial terms, and none of it belongs in a public repository. The
tree is created empty by `just setup`; nothing here ships with the repo.

| Folder | Source | Real or synthetic | Licence | Used for |
| --- | --- | --- | --- | --- |
| `data/real/` | _template — describe your source_ | real | _n/a, not redistributed_ | _extraction spot-checks only_ |
| `data/synthetic/kaggle/` | Kaggle invoice batch (100 born-digital PDFs + `batch_1.csv`) | synthetic | _confirm on the dataset page before reuse_ | **Extraction golden set** — the CSV carries `json_data` ground truth per file |
| `data/synthetic/hf_mychen76/` | `mychen76/invoices-and-receipts_ocr_v1` | synthetic | **not declared on the card** — local evaluation only, not redistributed | Vision-path extraction; sharpness triage |
| `data/synthetic/hf_rvlcdip/` | `chainyo/rvl-cdip-invoice` | real documents, public corpus | `other` — derived from IIT-CDIP; check terms | Low-quality scan handling |
| `data/generated/` | `scripts/generate_invoices.py` | synthetic, labelled | own work | Matching, tolerances, approvals, adversarial documents |

`data/synthetic/kaggle/` is the most useful corpus today: 100 born-digital PDFs
with per-file JSON ground truth, which is exactly what an extraction golden set
needs. It still has no purchase orders behind it, so it can score extraction and
nothing else.

Only `data/generated/` will have POs, which is why it is the only corpus that can
exercise matching, tolerances, or approval routing at all — see the docstring in
`scripts/generate_invoices.py`.

Run `just pull-data` to see each dataset's licence printed before anything
downloads; nothing is written without `--accept-licenses`.

## Layout

```
src/ap_agent/
  contracts/    Pydantic v2 models. The single source of truth for every schema.
  states/       The state enum, the event enum, and the transition table.
  tools/        One module per tool. What is missing is part of the design.
  loop/         Agent loop.                      STUB
  guardrails/   Versioned tolerance + approval config loader.   STUB
  audit/        Append-only event model (done) and writer/chain. STUB
  db/           SQLAlchemy models and Alembic environment.
  evals/        Golden-set loader and scorers.   STUB
  cli.py        Typer entry points.
scripts/        Corpus download, indexing, and synthetic generation.
config/         guardrails.v1.yaml — versioned, never edited in place.
docs/           ARCHITECTURE.md, DECISIONS.md
```

`CLAUDE.md` holds the rules that are not up for renegotiation.

## Licence

MIT.
