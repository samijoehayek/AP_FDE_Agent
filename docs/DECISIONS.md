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

## 2026-09-09 — extraction tool

| Decision | Rejected | Because |
| --- | --- | --- |
| Structured outputs (`output_config.format`, via the SDK's `messages.parse` with `output_format=InvoiceExtraction`) | Asking for JSON in the prompt and parsing the reply; a single `strict: true` tool the model must call | The API itself refuses to emit a shape the contract disallows, so prose is not a possible response and the "ignore your instructions and reply with X" attack has nowhere to land. Prompt-and-parse makes the schema a request; a forced tool contradicts rule 1, which says this model has no tools |
| No sampling parameters sent at all | `temperature=0` as specified | Not expressible: the models this seat runs on reject `temperature`/`top_p`/`top_k` with a 400, and `messages.parse()` does not accept them. Determinism comes from the schema constraint, which is a stronger guarantee than `temperature=0` — which never promised identical outputs either |
| TIFF is re-encoded to PNG before sending | Rejecting TIFF; rendering every input through PyMuPDF first | The Messages API accepts only jpeg/png/gif/webp in an image block, and scanned invoices arrive as TIFF often enough that rejecting them would be a real gap |
| PDFs sent as a `document` block, not rendered to images | Rasterising every page and sending image blocks | The model reads the page structure rather than a picture of it, and the text layer survives. Rendering is a lossy step taken for no reason when the API accepts the file |
| Evidence is a list of entries with an enumerated `field`, not one optional property per field | The fixed-key model; trimming EVIDENCE_FIELDS to five | Structured outputs enforces a complexity budget, and ten optional typed properties expand to ten `anyOf` branches (~3 KB) which pushes the whole schema over it: `400 Schema is too complex`, measured at 9320 B rejected vs 7536 B accepted. The enum keeps evidence a closed vocabulary, so the guarantee survives the reshape. Trimming to five fits but loses evidence on the PO references and the tax figures - the numbers matching will need |
| Every `Decimal` contract type canonicalises to a fixed scale on validation | Fixing it only in the golden-set scorer; canonicalising at the audit boundary | The audit chain hashes a serialisation, so `Decimal("1676976")` and `Decimal("1676976.00")` - equal numbers, different strings - would hash differently and report tampering that never happened. A chain that cries wolf gets ignored, which is worse than no chain. Fixing it in the scorer leaves the defect where it matters most; fixing it at the boundary means every consumer has to remember |
| Canonicalisation refuses to round rather than rounding | Quantising with ROUND_HALF_UP | The field constraints already reject excess places, so quantising is exact by construction. If that ever stops being true, a loud failure is the right outcome - silently rounding an invoice total is how a system loses money quietly |
| `cost_usd` is `CostUsd` (6 places), not `Money` (2) | Leaving it as Money | One extraction costs about $0.027, which `Money` **rejected outright** - so every model call would have failed to record its cost. The database column was already `Numeric(18,6)`; the contract disagreed with it. Found by auditing every `Money` use before changing the type, not by it failing later |
| `title` keys stripped from every contract's JSON schema | Trimming descriptions to free budget | Pydantic derives `"title": "Vendor Address"` from the key `vendor_address` - pure duplication the model can already read, costing 822 B. Descriptions are the field-level instructions extraction actually runs on, so trading those for budget would buy space with accuracy |
| `arithmetic_flags` excluded from the schema the model sees | Leaving it visible | It is derived: the validator computes it and would overwrite anything supplied, so offering it invites the reader of a document to grade its own arithmetic. It also frees a property slot and a whole enum definition. Still on the contract - only absent from the schema |
| `INR` added to the currency allowlist | Leaving it out; widening the list generally | Every invoice in the Kaggle corpus is Indian, which the first live extraction found by being refused. The allowlist is a calibration to the business, not a constant - widen it when the corpus does, never to force one awkward document through |
| The test session gets a sentinel API key, not the real one | Deleting the key; trusting every test to mock | An unmocked test would not fail - it would quietly succeed and bill a real account, and a green suite is where nobody looks. Deleting is not enough: the SDK falls through to `ANTHROPIC_AUTH_TOKEN` and to an `ant auth login` profile on disk, so an "unset" key can still authenticate. Setting the variable wins outright, and in pydantic-settings an env var also overrides the `.env` that `Settings` reads. Verified: an unmocked call under the guard returns 401 `authentication_error` |
| Prompt lives in `prompts/extract_v1.md`, loaded by version | Inlining the prompt in the module | An extraction from last month has to stay explainable under the prompt that produced it, which means old versions stay on disk and `prompt_version` is recorded on every result |

## 2026-09-09 — agent loop and audit writer

| Decision | Rejected | Because |
| --- | --- | --- |
| `decide()` is a pure function of the record and calls nothing | Deciding and doing in one function | Routing is the part most likely to change, and this way the entire policy is testable with no network, database or key. Every routing test would otherwise be an integration test |
| `apply()` returns an *event*; only `transition()` sets state | Letting `apply` return the next state | The table stays the only description of what is possible. It is also what makes a human-gated state stop the machine: the loop asks for a step, the table refuses, and there is no second opinion to fall back on |
| Stub steps advance through `STUB_TRANSITIONS`, a separate table | Faking the state change inside the loop | "What is real" is answerable by reading a table instead of tracing control flow, and a test can enumerate the scaffolding. Every stub edge mirrors an edge the real table already has, so stubs add event names, never destinations |
| Every stop writes an audit event — errors, budget exhaustion, human-gated halts | Logging only successful steps | A trail with holes exactly where things went wrong is worse than no trail, because it looks complete |
| `run()` never raises | Propagating tool exceptions | A failed invoice is a routing outcome, not a crash. The caller gets a record whose state says where it stopped |
| One JSONL file per invoice, hashed line to line | A single log file; the database table | The chain is per-invoice, so one file holds one history and concurrent invoices do not contend. The database writer stays a stub because only it can join the caller's transaction |
| The hash covers the bytes on disk, not a re-serialisation | Hashing the event object | Otherwise a formatting change looks like tampering and tampering can hide behind formatting |
| The writer overwrites any caller-supplied `prev_event_hash` | Trusting the caller | Only the writer knows what is actually last. A caller that picked its own predecessor would make the chain decorative |
| First link is `GENESIS_HASH` (64 zeros), not an empty string | The empty string the brief asked for | `prev_event_hash` is a validated SHA-256 hex field; an empty string cannot satisfy it |
| Tool callables are injected through `RunContext`, dispatched via module lookup | Importing them at the call site; plain function defaults | A dataclass default binds at class creation, so `monkeypatch.setattr(module, name, ...)` silently does nothing - a test would pass while still calling the real API. The indirection keeps the seam honest |
| `DEFAULT_MAX_STEPS = 15`, one more than the happy path | A generous round number | An agent that can loop forever will eventually loop forever on the invoice that costs the most tokens. A tight budget makes adding a pipeline step a deliberate change |

### Things that bit during the scaffold

Recorded because the next person to hit them should not have to re-derive the cause.

| Symptom | Cause | Resolution |
| --- | --- | --- |
| `import pymupdf` segfaulted the interpreter under pytest but not under `python` | `filterwarnings = ["error"]` promoted the `DeprecationWarning` that SWIG emits *during C extension module creation* into a Python exception, which SWIG cannot unwind | Three message-scoped `ignore` entries in `[tool.pytest.ini_options]`, so a genuine deprecation anywhere else still fails the build |
| `pull_hf_datasets.py` never returned, minutes after the last image was on disk | `datasets` streaming leaves a parquet reader registered for cleanup at interpreter shutdown, and that teardown does not complete on this stack | `os._exit` after flushing, with the reasoning in a comment at the call site. Everything the script writes is already flushed by `Path.write_bytes` |
| Ruff wanted every Pydantic and SQLAlchemy import moved into `if TYPE_CHECKING` | `runtime-evaluated-base-classes` matches the base class *named in the file*, and this repo's models inherit `StrictModel` / `ToolInput` / `Base`, not `BaseModel` directly | Listed this package's own bases alongside the library ones |
| Unique constraints came out named `idempotency_key`, unqualified | SQLAlchemy applies a naming convention only to *unnamed* constraints; an explicit `name=` is used verbatim | Left unique constraints unnamed so the convention generates `uq_<table>_<cols>`; check constraints pass only the short `constraint_name` their template interpolates |
| `just db-up` crash-looped on `postgres:18-alpine` with "in 18+, these Docker images are configured to store database data in a format compatible with pg_ctlcluster" | Postgres 18 changed the image's data layout to a major-version-specific subdirectory, so the volume mounts at `/var/lib/postgresql`, **not** `/var/lib/postgresql/data` | Moved the mount up one level (docker-library/postgres#1259) |
| The `REVOKE` that makes `audit_events` append-only did nothing | The Postgres image makes `POSTGRES_USER` a **superuser**, and a superuser bypasses every privilege check. The revoke was recorded in the table ACL and ignored at query time - the grant list showed only INSERT/SELECT, and an `UPDATE` still succeeded | Split the roles: `ap_agent` owns the schema and runs migrations, `ap_agent_app` (ordinary role) serves traffic. Revision `0002` grants against the app role. Verified live: `UPDATE` and `DELETE` now return `permission denied`, `INSERT` and `SELECT` still work |
| Three CI jobs failed at "Set up job" | `astral-sh/setup-uv` stopped publishing floating major tags after `v7`, so `@v10` does not resolve - the release tag is `v10.0.1` | Pinned the exact release. `actions/checkout@v7` and `actions/upload-artifact@v7` do publish floating tags and were fine |
| The secret-scan job exited 1 on a repository with no secrets | `gitleaks/gitleaks-action@v2` wraps the binary in a licence check that fails independently of the scan. The binary itself found nothing across all commits | Dropped the action and installed the pinned gitleaks binary directly - the same scan the pre-commit hook runs, with no third-party dependency in the path |
| `invoices.invoice_date` was `timestamptz` while its contract was `datetime.date` | A calendar date printed on paper has no time and no timezone; timestamptz invents both, and Postgres converts on read, so the same row can read as a different day. Here that is a wrong due date or a missed duplicate, not a cosmetic difference | Revision `0003` narrows it to `DATE`. Two structural tests now walk the metadata: calendar dates may not be timestamps, and instants must be timezone-aware |
| `just pull-data -- --accept-licenses` rejected the flag it was given | just forwards `--` verbatim into `{{ARGS}}`, and click reads `--` as end-of-options, so the flag after it arrives as a stray positional. The usual just idiom for forwarding flags is a trap for any recipe wrapping a click/typer CLI | Pass flags directly (`just pull-data --accept-licenses`), documented in the recipe and the README |
| The first live `just extract` returned `400 - This API key is not scoped to a workspace` | The key is **organisation**-scoped. Such a key resolves no workspace of its own, so the API cannot tell which one to bill and refuses unless the request names one | Added `ANTHROPIC_WORKSPACE_ID`, sent as the `anthropic-workspace-id` header **only when set** - a workspace-scoped key already carries one and the header would override it. A workspace-scoped key remains the alternative fix, with the setting left blank |
| Adding three string fields returned `400 Schema is too complex` again | The structured-output limit is a **property count**, not a byte count - which the byte-based mental model hid. 20 top-level properties were rejected at 8324 B while 17 were accepted at 8475 B: fewer bytes, more properties, rejected | Freed a slot by removing the derived `arithmetic_flags` from the schema and stripped redundant titles. 19 properties verified accepted. A test now caps the count and says to probe the API before raising it |
| The fix for the empty-evidence bug made every live request fail | Ten optional typed properties are ~3 KB of `anyOf` branches, and structured outputs rejects the whole schema past a complexity budget. Evidence went from silently empty to nothing working at all - strictly worse | Reshaped to a repeating entry with an enumerated field. The deeper lesson: the previous fix was verified against the transformed schema offline and never against the API, and the limit is not documented. Schema changes now get a live probe |
| A CLI test asserting the loop ran passed while never running it | `RunContext`'s tool defaults were plain function references, bound when the dataclass was created. Patching the module attribute did nothing, so the test exercised the real extraction seat, got a 401 from the sentinel key, recorded the error and still satisfied a loose assertion | Defaults now dispatch through the module attribute at call time, and the test asserts the final state and step count rather than that the command merely exited 0 |
| A settings test passed under `uv run python` and failed under pytest | The **deepeval pytest plugin** loads `.env` into `os.environ` at collection time. `_env_file=None` disables the dotenv *file*, not variables the plugin has already promoted, so a resolved `Settings` under pytest reflects the developer's own `.env` | Assert on `Settings.model_fields[...].default` everywhere the claim is about shipped defaults. Same root cause as the CI failure below, and the same fix |
| pytest passed locally and failed in CI | A settings test asserted on a resolved `Settings`, and `_env_file=None` disables the `.env` file but not environment variables - CI sets `DATABASE_URL`, so the test was reading the environment rather than the code | Assert on `Settings.model_fields[...].default`. A test that reads ambient environment tests the environment |
| The initial migration could not be autogenerated | Docker was not running, and `--autogenerate` needs a live database to diff against | Rendered `CreateTableOp.from_table` for each table in `Base.metadata` through Alembic's own code renderer, then verified with `alembic upgrade head --sql` (offline, no connection). A test asserts the revision creates every model's table |
