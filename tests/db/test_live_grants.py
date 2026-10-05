"""The application role's grants, checked against a live database.

A grant is the control; a test that reads the migration only proves the text is
there. These connect as ``ap_agent_app`` - the role the application serves
traffic as - and try the operations directly. Every write happens inside a
transaction that is rolled back, so the database is left as found.

Skipped when no database is reachable (``just db-up`` then ``just db-migrate``).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.pool import NullPool

from ap_agent.config import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator


def _engine() -> sa.Engine | None:
    """The application role's engine, or None if the database is not up."""
    engine = sa.create_engine(Settings.model_fields["database_url"].default, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            # Not alembic_version: 0002 revokes it from this role, rightly.
            present = connection.execute(
                sa.text("SELECT to_regclass('public.payloads') IS NOT NULL")
            ).scalar()
    except (OperationalError, DBAPIError):
        return None
    return engine if present else None


ENGINE = _engine()

pytestmark = pytest.mark.skipif(
    ENGINE is None, reason="no database at 0004 reachable as ap_agent_app"
)


@pytest.fixture
def app() -> Iterator[sa.Connection]:
    """A connection as the application role, inside a transaction always rolled back."""
    assert ENGINE is not None
    with ENGINE.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _denied(connection: sa.Connection, statement: str, **params: object) -> bool:
    """Whether the statement is refused for lack of privilege."""
    savepoint = connection.begin_nested()
    try:
        connection.execute(sa.text(statement), params)
    except DBAPIError as exc:
        savepoint.rollback()
        return "permission denied" in str(exc.orig)
    savepoint.rollback()
    return False


def _payload(connection: sa.Connection, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    connection.execute(
        sa.text("INSERT INTO payloads (sha256, media_type, bytes) VALUES (:h, :m, :b)"),
        {"h": digest, "m": "application/json", "b": content},
    )
    return digest


def _invoice(connection: sa.Connection) -> str:
    connection.execute(
        sa.text(
            "INSERT INTO documents (sha256, storage_uri, media_type, byte_size, page_count, "
            "has_text_layer, pages_with_text) VALUES (:h, 'file:///x', 'application/pdf', 1, 1, "
            "true, 1)"
        ),
        {"h": "a" * 64},
    )
    connection.execute(
        sa.text(
            "INSERT INTO invoices (invoice_id, document_sha256, state) "
            "VALUES ('01TESTINVOICE0000000000000', :h, 'RECEIVED')"
        ),
        {"h": "a" * 64},
    )
    return "01TESTINVOICE0000000000000"


def test_a_payload_can_be_written_and_read(app: sa.Connection) -> None:
    digest = _payload(app, b'{"a": 1}')
    stored = app.execute(
        sa.text("SELECT bytes FROM payloads WHERE sha256 = :h"), {"h": digest}
    ).scalar()
    assert stored == b'{"a": 1}'


def test_a_payload_can_never_be_changed_or_removed(app: sa.Connection) -> None:
    """Content-addressed: the key is the content, so there is nothing to update."""
    digest = _payload(app, b"x")
    assert _denied(app, "UPDATE payloads SET media_type = 'x' WHERE sha256 = :h", h=digest)
    assert _denied(app, "DELETE FROM payloads WHERE sha256 = :h", h=digest)


def test_a_payload_key_must_be_a_sha256(app: sa.Connection) -> None:
    savepoint = app.begin_nested()
    with pytest.raises(IntegrityError):
        app.execute(
            sa.text("INSERT INTO payloads (sha256, media_type, bytes) VALUES ('nope', 'x', 'x')")
        )
    savepoint.rollback()


@pytest.mark.parametrize(
    "table",
    [
        "invoices",
        "documents",
        "erp_writes",
        "approvals",
        "approval_decisions",
        "extractions",
        "runs",
        "dedup_index",
        "payloads",
        "audit_events",
    ],
)
def test_the_application_deletes_nothing(app: sa.Connection, table: str) -> None:
    """Rule 3: no tool deletes anything. Held by the grant, not by the code."""
    assert _denied(app, f"DELETE FROM {table} WHERE false")  # noqa: S608 - fixed table names


@pytest.mark.parametrize("table", ["dedup_index", "approval_decisions", "audit_events"])
def test_insert_only_tables_cannot_be_updated(app: sa.Connection, table: str) -> None:
    column = {
        "dedup_index": "vendor_id",
        "approval_decisions": "reason",
        "audit_events": "decision",
    }[table]
    assert _denied(app, f"UPDATE {table} SET {column} = NULL WHERE false")  # noqa: S608


def test_one_person_approves_once(app: sa.Connection) -> None:
    """A tier needing two approvers needs two people."""
    invoice_id = _invoice(app)
    app.execute(
        sa.text(
            "INSERT INTO approvals (approval_id, invoice_id, required_approvers, approver_role, "
            "amount, currency, config_version, rule_basis, status) VALUES "
            "('01TESTAPPROVAL000000000000', :i, 2, 'owner', 1.00, 'USD', 'guardrails_v1', 'x', "
            "'pending')"
        ),
        {"i": invoice_id},
    )
    insert = sa.text(
        "INSERT INTO approval_decisions (decision_id, approval_id, decided_by, decision, reason) "
        "VALUES (:d, '01TESTAPPROVAL000000000000', 'u-1', 'approve', 'checked')"
    )
    app.execute(insert, {"d": "01TESTDECISION000000000001"})
    savepoint = app.begin_nested()
    with pytest.raises(IntegrityError):
        app.execute(insert, {"d": "01TESTDECISION000000000002"})
    savepoint.rollback()


def test_a_decision_needs_a_reason(app: sa.Connection) -> None:
    invoice_id = _invoice(app)
    app.execute(
        sa.text(
            "INSERT INTO approvals (approval_id, invoice_id, required_approvers, approver_role, "
            "amount, currency, config_version, rule_basis, status) VALUES "
            "('01TESTAPPROVAL000000000000', :i, 1, 'ap_clerk', 1.00, 'USD', 'guardrails_v1', "
            "'x', 'pending')"
        ),
        {"i": invoice_id},
    )
    savepoint = app.begin_nested()
    with pytest.raises(IntegrityError):
        app.execute(
            sa.text(
                "INSERT INTO approval_decisions (decision_id, approval_id, decided_by, decision, "
                "reason) VALUES ('01TESTDECISION000000000001', '01TESTAPPROVAL000000000000', "
                "'u-1', 'approve', '   ')"
            )
        )
    savepoint.rollback()


def test_one_invoice_per_document(app: sa.Connection) -> None:
    """Running the same file again resumes it; it never creates a second invoice."""
    _invoice(app)
    savepoint = app.begin_nested()
    with pytest.raises(IntegrityError):
        app.execute(
            sa.text(
                "INSERT INTO invoices (invoice_id, document_sha256, state) "
                "VALUES ('01TESTINVOICE0000000000001', :h, 'RECEIVED')"
            ),
            {"h": "a" * 64},
        )
    savepoint.rollback()
