# Decision log

One line per decision, newest first, each with the alternative it beat. A
decision that does not name what it rejected is a preference, not a decision.

## 2026-09-09 — scaffold

### Language and tooling

| Decision | Rejected | Because |
| --- | --- | --- |
| Python 3.12 floor, 3.13 pinned in `.python-version`, both in CI | 3.14 (current stable) | 3.14 wheels exist for the heavy deps, but 3.13 is a year of production use ahead of it and the floor is what keeps the matrix honest |
| `uv` for dependency and environment management | Poetry, pip-tools, PDM | Lockfile plus interpreter management in one tool, resolution in under a second, and `uv run` makes "which environment am I in" unaskable |
| `uv_build` as the build backend | Hatchling, setuptools | The project is a straightforward `src/` package; using the backend that ships with the resolver removes a dependency and a config block |
| `src/` layout | flat layout | Tests import the installed package, so a missing `__init__.py` or a broken export fails in CI rather than passing because the CWD happened to be on `sys.path` |
| `ruff` for lint *and* format | flake8 + black + isort + pyupgrade | One tool, one config, and a rule set (`ANN`, `S`, `DTZ`, `TRY`, `PL`) that covers what four tools used to |
| `pyright` strict | mypy | Faster on this size of codebase, better Pydantic v2 inference, and the same engine as the editor most reviewers will open this in |
| `just` as the task runner | Make, npm-style scripts, tox | Make's tab-and-phony semantics are noise for a project with no compilation step; `just --list` is self-documenting |
| `gitleaks` as the secrets scanner | `detect-secrets`, `trufflehog` | Actively maintained, single Go binary so it cannot conflict with the project's own dependencies, scans history in CI as well as the staged diff |
| A `no-data-committed` pre-commit hook on top of `.gitignore` | `.gitignore` alone | `git add -f` exists, and a real invoice in git history is permanent |

### Schema and data modelling

| Decision | Rejected | Because |
| --- | --- | --- |
| Pydantic v2 with `extra="forbid"` on every contract | `extra="ignore"`, dataclasses, TypedDict | Forbidding extras is the control that stops unmodelled data — bank details, free-form instructions — riding into trusted code on a payload |
| Contracts are frozen (`InvoiceExtraction` excepted) | Mutable models | An object that has been hashed into the audit chain must not be alterable afterwards |
| `InvoiceExtraction` alone is mutable | `object.__setattr__` on a frozen model; a `computed_field` | The arithmetic self-check records flags in an after-validator; a computed field would have been elegant but diverges from "set a list rather than raising", and the setattr trick reads as a workaround |
| `Decimal` for money, `float` only for scores | `float` everywhere, integer minor units | A float subtotal off by 1e-15 fails a tolerance check and the failure looks like a vendor problem; integer minor units would work but leak into every call site |
| Arithmetic inconsistencies are flagged, never raised | Raising `ValidationError` | A document whose numbers disagree is exactly the document a human needs to see; raising loses the extraction that proves the problem |
| A small currency allowlist | Accepting any ISO-4217 code | An unexpected currency is one of the cheapest signals that a document is not what it claims |
| Evidence keys restricted to a declared set | Free-form `dict[str, FieldEvidence]` | Otherwise evidence becomes an unbounded channel for document text into trusted context |
| One closed `ReasonCode` enum shared by matching and explanation | Separate vocabularies, or free-form strings | A rule and a model-written explanation must never disagree about *what* went wrong, only about how to describe it |
| ULIDs for event and invoice ids | UUID4, auto-increment integers | Lexicographically sortable by creation time, so the audit log sorts without a join, and no sequence to leak |
| Audit actor as a discriminated union | An `actor_type` string plus nullable columns | "Who did this" is the first question asked of an AP trail, and the answer must carry which rule at which config version, which model at which prompt version |
| `ts_utc` rejected unless it is UTC | Accepting any aware datetime | A trail with mixed offsets cannot be ordered later, and converting at the edge is always cheaper than in a query |

### Architecture

| Decision | Rejected | Because |
| --- | --- | --- |
| A transition table, not state methods | `if`/`elif` in the loop, a workflow engine (Temporal, Prefect) | The table is the spec: diffable in review, renderable as the diagram, and assertable by graph search. An engine is the right answer at ten times this volume and premature at this one |
| `transition()` is pure; the caller writes the audit event | Writing the event inside `transition()` | The state write and the audit row must commit in one transaction; a function that does its own I/O cannot participate in the caller's |
| Cancellation stops at `APPROVED` | Allowing cancel from any non-terminal state | Once a liability is in the ledger, unwinding it is an accounting action with its own trail |
| No tool is exposed to any model | Giving the explanation seat read-only tools | Prompt injection does not need to be solved if the model that reads attacker-controlled text cannot cause an effect. Widening this is an architecture change, not a refactor |
| `pay_bill`, `update_vendor`, `update_bank_details`, `delete_*` do not exist | Implementing them behind a guardrail | Prompts and guardrails reduce the probability of a bad call; absence reduces its possibility to zero. Asserted by a test so adding one is a failure and a conversation |
| Tolerances and the approval matrix in versioned YAML | Constants in Python, database rows | They change on a different clock than the code, must be reviewable by people who do not read Python, and old versions must remain resolvable so a past decision can be re-explained |
| Straight-through processing off by default (`auto_approve_max_amount: 0`) | A "sensible" default threshold | Turning it on should be a reviewed change with a golden-set number attached |
| Approvals are database rows | In-memory futures, webhook callbacks | An approval takes days; the process will restart in the meantime |
| Idempotency keys claimed in an `erp_writes` table | Relying on the ERP's own deduplication | A retry after a timeout must find the prior claim locally, before the second call goes out |
| `audit_events` append-only at the schema level | An ORM-level convention | The ORM is what a future bug will be written in; the `REVOKE` in the migration is not |
| Postgres 18 | Postgres 16 (the stated floor), SQLite | Current stable with the longest support window; SQLite cannot express the concurrency the approval flow needs |
| State stored as `String(32)`, validated in code | A database enum type | Adding a state should not require a migration and a deploy in lockstep, and the transition table is the spec either way |

### Data

| Decision | Rejected | Because |
| --- | --- | --- |
| `data/` git-ignored in its entirety, tree created from `.gitkeep` | Committing a small sample | Real invoices carry PII and supplier commercial terms; third-party corpora carry licences this repo cannot redistribute under. Both are permanent once in git history |
| Licences printed and `--accept-licenses` required before any download | Downloading and mentioning the licence in the README | A downloader that enforces it is read; a README section is not |
| Media type sniffed from magic bytes | `mimetypes.guess_type` on the filename | An attacker controls the filename |
| Laplacian variance via numpy | Pillow's `ImageFilter.Kernel` + `ImageStat` | The Pillow route clips the kernel response at 0 and 255, which distorts exactly the high-contrast edges the score is measuring. numpy is added as an explicit dependency rather than leaned on transitively through `datasets` |
| Sharpness measured at a fixed 1000px long edge | Native resolution | Laplacian variance scales with resolution, so scoring a 300-dpi scan and a 72-dpi one at native size compares nothing |
| Exact-hash duplicates flagged in `data/index.csv` | Silently de-duplicating | The same document scored fifty times looks like fifty passes; the eval split needs to see them to exclude them |
| Synthetic generation from seeded POs is planned despite public corpora existing | Relying on Hugging Face and Kaggle sets alone | A downloaded corpus has no purchase orders behind it, so it can only exercise extraction — never matching, tolerances, or approval routing |

### Observability

| Decision | Rejected | Because |
| --- | --- | --- |
| structlog, JSON in CI and production | stdlib `logging` with format strings | Structured events are queryable; the alternative is grep |
| Langfuse deferred to Day 5, its compose block committed commented-out | Wiring it now; omitting it entirely | The system must run correctly without it — `audit_events` is the compliance record and Langfuse is for debugging and cost analysis. Leaving the block visible reserves the ports and shows the intended shape |
| `deepeval` for the LLM-judged parts of the golden set only | Judging everything with a model; judging nothing | Deterministic fields are compared, not judged. A model judge is for exception summaries, where there is no single right string |

### Things that bit during the scaffold

Recorded because the next person to hit them should not have to re-derive the cause.

| Symptom | Cause | Resolution |
| --- | --- | --- |
| `import pymupdf` segfaulted the interpreter under pytest but not under `python` | `filterwarnings = ["error"]` promoted the `DeprecationWarning` that SWIG emits *during C extension module creation* into a Python exception, which SWIG cannot unwind | Three message-scoped `ignore` entries in `[tool.pytest.ini_options]`, so a genuine deprecation anywhere else still fails the build |
| `pull_hf_datasets.py` never returned, minutes after the last image was on disk | `datasets` streaming leaves a parquet reader registered for cleanup at interpreter shutdown, and that teardown does not complete on this stack | `os._exit` after flushing, with the reasoning in a comment at the call site. Everything the script writes is already flushed by `Path.write_bytes` |
| Ruff wanted every Pydantic and SQLAlchemy import moved into `if TYPE_CHECKING` | `runtime-evaluated-base-classes` matches the base class *named in the file*, and this repo's models inherit `StrictModel` / `ToolInput` / `Base`, not `BaseModel` directly | Listed this package's own bases alongside the library ones |
| Unique constraints came out named `idempotency_key`, unqualified | SQLAlchemy applies a naming convention only to *unnamed* constraints; an explicit `name=` is used verbatim | Left unique constraints unnamed so the convention generates `uq_<table>_<cols>`; check constraints pass only the short `constraint_name` their template interpolates |
| The initial migration could not be autogenerated | Docker was not running, and `--autogenerate` needs a live database to diff against | Rendered `CreateTableOp.from_table` for each table in `Base.metadata` through Alembic's own code renderer, then verified with `alembic upgrade head --sql` (offline, no connection). A test asserts the revision creates every model's table |
