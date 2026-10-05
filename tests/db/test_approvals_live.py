"""The Postgres approval store against a live database.

Completion is derived from the decision rows: the first reject rejects, enough
distinct approvers approve, and one person's second vote is refused. Written as
the application role; removed afterwards as the schema owner.

Skipped when no database at 0005 is reachable.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.pool import NullPool
from ulid import ULID

from ap_agent.config import Settings
from ap_agent.contracts.approval import ApprovalRequirement
from ap_agent.contracts.audit import utc_now
from ap_agent.contracts.run import InvoiceRecord
from ap_agent.db.base import create_session_factory
from ap_agent.db.store import PostgresApprovalStore, PostgresInvoiceStore
from ap_agent.payloads import PostgresPayloadStore
from ap_agent.persistence import ApprovalDecisionRefusedError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _engine(url_field: str) -> sa.Engine:
    return sa.create_engine(Settings.model_fields[url_field].default, poolclass=NullPool)


def _ready() -> bool:
    try:
        with _engine("database_url").connect() as connection:
            return bool(
                connection.execute(
                    sa.text("SELECT to_regclass('public.approvals') IS NOT NULL")
                ).scalar()
            )
    except (OperationalError, DBAPIError):
        return False


pytestmark = pytest.mark.skipif(not _ready(), reason="no database at 0005 reachable")


@pytest.fixture
def approvals(tmp_path: Path) -> Iterator[tuple[PostgresApprovalStore, str]]:
    """An approval store and one invoice to request against; cleaned up as the owner."""
    sessions = create_session_factory(_engine("database_url"))
    invoices = PostgresInvoiceStore(sessions, PostgresPayloadStore(sessions))
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(f"%PDF-1.4 {ULID()}".encode())
    record = InvoiceRecord(source_path=pdf, created_at=utc_now())
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    invoices.create(record, document_sha256=sha, byte_size=pdf.stat().st_size, storage_uri="x")
    invoice_id = str(record.invoice_id)
    yield PostgresApprovalStore(sessions), invoice_id
    with _engine("database_migration_url").begin() as owner:
        params = {"i": invoice_id}
        owner.execute(
            sa.text(
                "DELETE FROM approval_decisions WHERE approval_id IN "
                "(SELECT approval_id FROM approvals WHERE invoice_id = :i)"
            ),
            params,
        )
        owner.execute(sa.text("DELETE FROM approvals WHERE invoice_id = :i"), params)
        owner.execute(sa.text("DELETE FROM invoices WHERE invoice_id = :i"), params)
        owner.execute(sa.text("DELETE FROM documents WHERE sha256 = :s"), {"s": sha})


def _requirement(required: int) -> ApprovalRequirement:
    return ApprovalRequirement(
        required_approvers=required,
        approver_role="owner",
        tier_max_amount=None,
        rules_fired=[],
        hold_for_callback=False,
        amount=Decimal("272100.00"),
        currency="USD",
        auto_approve=False,
        basis="rule=APPROVAL-MATRIX@v1, test",
    )


def _open(store: PostgresApprovalStore, invoice_id: str, required: int = 2) -> str:
    approval_id = str(ULID())
    store.request(
        approval_id,
        invoice_id,
        _requirement(required),
        amount=Decimal("272100.00"),
        currency="USD",
        config_version="guardrails_v1",
    )
    return approval_id


def test_a_request_is_opened_once(approvals: tuple[PostgresApprovalStore, str]) -> None:
    store, invoice_id = approvals
    approval_id = str(ULID())
    args = (approval_id, invoice_id, _requirement(2))
    kwargs = {"amount": Decimal("1.00"), "currency": "USD", "config_version": "guardrails_v1"}

    first, created = store.request(*args, **kwargs)  # type: ignore[arg-type]
    again, created_again = store.request(*args, **kwargs)  # type: ignore[arg-type]

    assert (created, created_again) == (True, False)
    assert first == again
    assert (first.status, first.required_approvers, first.approvals) == ("pending", 2, ())


def test_two_people_complete_it_and_one_person_votes_once(
    approvals: tuple[PostgresApprovalStore, str],
) -> None:
    store, invoice_id = approvals
    approval_id = _open(store, invoice_id)

    one = store.decide(approval_id, user_id="alice", decision="approve", reason="ok")
    assert (one.status, one.approvals) == ("pending", ("alice",))
    with pytest.raises(ApprovalDecisionRefusedError, match="alice has already decided"):
        store.decide(approval_id, user_id="alice", decision="approve", reason="again")
    two = store.decide(approval_id, user_id="bob", decision="approve", reason="agreed")

    assert (two.status, two.approvals) == ("approved", ("alice", "bob"))
    with _engine("database_migration_url").connect() as owner:
        row = owner.execute(
            sa.text("SELECT status, decided_by, decision FROM approvals WHERE approval_id = :a"),
            {"a": approval_id},
        ).one()
    assert tuple(row) == ("approved", "bob", "approve"), "the completing decision is recorded"


def test_the_first_reject_completes_it(approvals: tuple[PostgresApprovalStore, str]) -> None:
    store, invoice_id = approvals
    approval_id = _open(store, invoice_id)
    store.decide(approval_id, user_id="alice", decision="approve", reason="ok")

    final = store.decide(approval_id, user_id="bob", decision="reject", reason="wrong price")

    assert (final.status, final.rejected_by) == ("rejected", "bob")
    with pytest.raises(ApprovalDecisionRefusedError, match="is rejected, not pending"):
        store.decide(approval_id, user_id="carol", decision="approve", reason="late")


def test_a_cancelled_request_takes_no_more_decisions(
    approvals: tuple[PostgresApprovalStore, str],
) -> None:
    store, invoice_id = approvals
    approval_id = _open(store, invoice_id)

    store.cancel(approval_id, by="rule:APPROVAL-REFRESH@v1", reason="match changed")

    view = store.get(approval_id)
    assert view is not None
    assert view.status == "cancelled"
    with pytest.raises(ApprovalDecisionRefusedError, match="is cancelled"):
        store.decide(approval_id, user_id="alice", decision="approve", reason="ok")
