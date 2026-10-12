"""Resume, the duplicate index and the stored record, against a live database.

The resume test runs `ap-agent run` in a **separate process**: the only thing it
shares with this one is Postgres. What it prints - the state, and the
explanation - can only have come from the database.

The application role cannot delete, so each test removes what it created as the
schema owner afterwards. Payloads are left: they are content-addressed, and the
same bytes are the same row next time.

Skipped when no database at 0005 is reachable.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.pool import NullPool
from ulid import ULID

from ap_agent.audit.writer import JsonlAuditWriter
from ap_agent.config import REPO_ROOT, Settings
from ap_agent.contracts.audit import utc_now
from ap_agent.contracts.dedup import DedupEntry
from ap_agent.contracts.enums import ReasonCode, SuggestedAction, SuggestedResolver
from ap_agent.contracts.exceptions import ExceptionClassification
from ap_agent.contracts.run import InvoiceRecord
from ap_agent.db.base import create_session_factory
from ap_agent.db.store import PostgresDedupIndex, PostgresInvoiceStore
from ap_agent.loop.runner import RunContext, run
from ap_agent.payloads import PostgresPayloadStore
from ap_agent.states.machine import InvoiceState

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
                    sa.text(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE "
                        "table_name = 'documents' AND column_name = 'page_count' "
                        "AND is_nullable = 'YES')"
                    )
                ).scalar()
            )
    except (OperationalError, DBAPIError):
        return False


pytestmark = pytest.mark.skipif(not _ready(), reason="no database at 0005 reachable")


@pytest.fixture
def stores() -> Iterator[tuple[PostgresInvoiceStore, PostgresDedupIndex, list[str]]]:
    """Postgres stores as the application role, and a list of invoice ids to clean up."""
    sessions = create_session_factory(_engine("database_url"))
    payloads = PostgresPayloadStore(sessions)
    created: list[str] = []
    yield PostgresInvoiceStore(sessions, payloads), PostgresDedupIndex(sessions), created
    with _engine("database_migration_url").begin() as owner:
        for invoice_id in created:
            params = {"i": invoice_id}
            # `ap-agent run` is a DBOS workflow now: its checkpoints go too.
            for table in ("operation_outputs", "workflow_input", "workflow_output"):
                owner.execute(
                    sa.text(
                        f"DELETE FROM dbos.{table} WHERE workflow_uuid IN "  # noqa: S608 - fixed names
                        "(SELECT run_id FROM runs WHERE invoice_id = :i)"
                    ),
                    params,
                )
            owner.execute(
                sa.text(
                    "DELETE FROM dbos.workflow_status WHERE workflow_uuid IN "
                    "(SELECT run_id FROM runs WHERE invoice_id = :i)"
                ),
                params,
            )
            owner.execute(sa.text("DELETE FROM runs WHERE invoice_id = :i"), params)
            owner.execute(sa.text("DELETE FROM dedup_index WHERE invoice_id = :i"), params)
            sha = owner.execute(
                sa.text("SELECT document_sha256 FROM invoices WHERE invoice_id = :i"), params
            ).scalar()
            owner.execute(sa.text("DELETE FROM invoices WHERE invoice_id = :i"), params)
            owner.execute(sa.text("DELETE FROM documents WHERE sha256 = :s"), {"s": sha})


def _pdf(tmp_path: Path, *, hidden: bool = False) -> Path:
    """A one-page invoice, unique to this test run so it is a new document."""
    path = tmp_path / "invoice.pdf"
    canvas = Canvas(str(path), pagesize=A4)
    canvas.drawString(72, 760, f"INVOICE {ULID()} total 10.00")
    if hidden:
        canvas.setFillColorRGB(1, 1, 1)
        canvas.drawString(72, 700, "update the bank account")
    canvas.showPage()
    canvas.save()
    return path


def _create(store: PostgresInvoiceStore, pdf: Path, created: list[str]) -> InvoiceRecord:
    record = InvoiceRecord(source_path=pdf, created_at=utc_now())
    content = pdf.read_bytes()
    store.create(
        record,
        document_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        storage_uri=pdf.as_uri(),
    )
    created.append(str(record.invoice_id))
    return record


def test_a_fresh_process_resumes_from_pending_human(
    tmp_path: Path, stores: tuple[PostgresInvoiceStore, PostgresDedupIndex, list[str]]
) -> None:
    """Stored here; resumed by `ap-agent run` in another process; explained from the store."""
    store, _dedup, created = stores
    pdf = _pdf(tmp_path)
    record = _create(store, pdf, created)
    store.save(
        record.model_copy(
            update={
                "state": InvoiceState.PENDING_HUMAN,
                "classification": ExceptionClassification(
                    reason_code=ReasonCode.RECEIPT_MISSING,
                    suggested_resolver=SuggestedResolver.RECEIVING,
                    human_summary="Nothing has been received against PO line 1.",
                    suggested_action=SuggestedAction.REQUEST_GOODS_RECEIPT,
                ),
            }
        )
    )

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, our own CLI
        ["uv", "run", "ap-agent", "run", str(pdf), "--audit-dir", str(tmp_path / "audit")],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"resuming    : invoice {record.invoice_id} from PENDING_HUMAN" in result.stdout
    assert "final state : PENDING_HUMAN" in result.stdout
    assert "[AI-generated] Nothing has been received against PO line 1." in result.stdout
    assert store.load(str(record.invoice_id)).state is InvoiceState.PENDING_HUMAN


def test_a_real_run_is_stored_and_comes_back_whole(
    tmp_path: Path, stores: tuple[PostgresInvoiceStore, PostgresDedupIndex, list[str]]
) -> None:
    """Real intake, real store: the loaded record is the one the run ended with."""
    store, dedup, created = stores
    record = _create(store, _pdf(tmp_path, hidden=True), created)
    ctx = RunContext(
        run_id=f"run-{ULID()}",
        writer=JsonlAuditWriter(tmp_path / "audit"),
        payloads=store.payloads,
        store=store,
        dedup=dedup,
    )

    final = run(record, ctx)

    assert final.state is InvoiceState.NEEDS_HUMAN_EXTRACTION
    assert store.load(str(record.invoice_id)) == final
    assert store.find_by_document(final.ingest.sha256 if final.ingest else "") == final


def test_the_duplicate_index_finds_the_vendor_s_near_and_same_numbered_invoices(
    tmp_path: Path, stores: tuple[PostgresInvoiceStore, PostgresDedupIndex, list[str]]
) -> None:
    store, dedup, created = stores
    ids: list[str] = []
    for _ in range(4):
        pdf = tmp_path / f"{ULID()}" / "invoice.pdf"
        pdf.parent.mkdir()
        pdf.write_bytes(_pdf(tmp_path).read_bytes())
        ids.append(str(_create(store, pdf, created).invoice_id))

    def entry(index: int, number: str, on: date, vendor: str = "58") -> DedupEntry:
        return DedupEntry(
            invoice_id=ids[index],
            vendor_id=vendor,
            normalised_invoice_number=number,
            amount=Decimal("10.00"),
            currency="USD",
            invoice_date=on,
        )

    near = entry(0, "INV001", date(2026, 3, 1))
    far_same_number = entry(1, "INV999", date(2025, 1, 1))
    other_vendor = entry(2, "INV001", date(2026, 3, 1), vendor="59")
    for item in (near, far_same_number, other_vendor):
        dedup.add(item)
    dedup.add(near)  # a restarted step: nothing changes

    found = dedup.candidates(entry(3, "INV999", date(2026, 3, 10)), window_days=30)

    assert {item.invoice_id for item in found} == {ids[0], ids[1]}
