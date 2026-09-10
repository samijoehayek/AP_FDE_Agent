# ap-agent task runner.  `just --list` to see everything.

set dotenv-load := true
set shell := ["bash", "-uc"]

default:
    @just --list

# Install dependencies and git hooks.
setup:
    uv sync --all-groups
    uv run pre-commit install
    @echo
    @echo "Next: cp .env.example .env && just db-up && just db-migrate"

# Lint and format check.
lint:
    uv run ruff check .
    uv run ruff format --check .

# Apply formatting and safe lint fixes.
fmt:
    uv run ruff check --fix .
    uv run ruff format .

# Strict type check.
typecheck:
    uv run pyright

# Lint, typecheck, and run the test suite with coverage.
test: lint typecheck
    uv run pytest --cov --cov-report=term-missing

# Tests only, no lint or typecheck. For a tight edit loop.
test-fast:
    uv run pytest -q

# --- database --------------------------------------------------------------

db-up:
    docker compose up -d postgres
    @echo "waiting for postgres..."
    @until docker compose exec -T postgres pg_isready -U ap_agent -d ap_agent >/dev/null 2>&1; do sleep 1; done
    @echo "postgres ready on localhost:5432"

db-down:
    docker compose down

# Destroy the volume too. Everything in the database is lost.
db-reset:
    docker compose down -v
    just db-up
    just db-migrate

db-migrate:
    uv run alembic upgrade head

db-revision message:
    uv run alembic revision --autogenerate -m "{{message}}"

db-shell:
    docker compose exec postgres psql -U ap_agent -d ap_agent

# Prove audit_events refuses UPDATE, DELETE and TRUNCATE for the app role.
verify-audit:
    uv run python scripts/verify_append_only.py

# --- data ------------------------------------------------------------------

# Print dataset licences, then stream sample corpora into data/synthetic/.
#
#   just pull-data                      licences only, downloads nothing, exits 1
#   just pull-data --accept-licenses    pull both at their defaults (50 + 20)
#   just pull-data --accept-licenses --dataset mychen76 --limit 10
#
# Pass flags directly. Do NOT use the usual `just recipe -- --flag` separator:
# `--` is forwarded verbatim into ARGS, and click reads it as end-of-options, so
# the flag after it arrives as a stray positional and the script rejects it.
pull-data *ARGS:
    uv run python scripts/pull_hf_datasets.py {{ARGS}}

# Run one invoice through the agent loop. Spends tokens (extraction calls the API).
run PATH *ARGS:
    uv run ap-agent run {{PATH}} {{ARGS}}

# Read one invoice with the extraction model. Spends tokens.
extract PATH *ARGS:
    uv run ap-agent extract {{PATH}} {{ARGS}}

# Read one invoice twice and score the agreement. Spends tokens (two model calls).
confidence PATH *ARGS:
    uv run ap-agent confidence {{PATH}} {{ARGS}}

# Walk data/ and write data/index.csv.
ingest *ARGS:
    uv run python scripts/index_invoices.py {{ARGS}}

# Render labelled synthetic invoices from seeded POs. STUB.
generate *ARGS:
    uv run python scripts/generate_invoices.py {{ARGS}}

# --- docs ------------------------------------------------------------------

# Regenerate the state diagram in docs/ARCHITECTURE.md from the transition table.
docs-diagram:
    uv run python scripts/render_state_diagram.py

# --- housekeeping ----------------------------------------------------------

hooks:
    uv run pre-commit run --all-files

clean:
    rm -rf .pytest_cache .ruff_cache .coverage htmlcov coverage.xml
    find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +
