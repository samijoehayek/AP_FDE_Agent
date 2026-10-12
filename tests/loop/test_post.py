"""POST@v1 and create_bill: an approved invoice becomes one bill, and only one.

QuickBooks is the suite's fake (``tests/qbo_fake.py``, bound for every test by
``conftest.py``); everything else - the loop, the transition table, the stores,
the approval - is real. The crash tests here kill in process; the cross-process
version, with DBOS and Postgres, is in ``tests/db/test_durable_live.py``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from tests.loop.test_runner import happy_context, make_extraction
from tests.qbo_fake import FakeQbo

from ap_agent.audit.writer import JsonlAuditWriter
from ap_agent.contracts.audit import utc_now
from ap_agent.contracts.enums import AuditEventType
from ap_agent.contracts.run import ActionKind, InvoiceRecord
from ap_agent.errors import IllegalTransition
from ap_agent.integrations.qbo.client import QboError
from ap_agent.loop.runner import (
    InProcess,
    Progress,
    StepTrail,
    apply_human_decision,
    bill_for,
    commit_iteration,
    decide,
    decide_iteration,
    drive,
    run,
)
from ap_agent.loop.runner import apply as apply_step
from ap_agent.persistence import (
    CURRENT_APPROVAL_STORE,
    CURRENT_INVOICE_STORE,
    InMemoryApprovalStore,
    InMemoryDedupIndex,
    InMemoryInvoiceStore,
)
from ap_agent.states.machine import TRANSITIONS, InvoiceEvent, InvoiceState
from ap_agent.tools.create_bill import (
    PostingRefusedError,
    create_bill,
    idempotency_key_for,
    json_number,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from ap_agent.contracts.audit import AuditEvent
    from ap_agent.contracts.invoice import InvoiceExtraction
    from ap_agent.loop.runner import RunContext

VENDOR = "62"
"""The resolved vendor in ``happy_context``: QuickBooks' id, as the bill's VendorRef."""
DOC_NUMBER = "INV1"
"""``INV-1``, normalised."""
KEY = idempotency_key_for(VENDOR, DOC_NUMBER)
NOT_APPROVED = [state for state in InvoiceState if state is not InvoiceState.APPROVED]
VENDOR_WORDS = "WIDGET"
"""The vendor's spelling of the order's "Widget". The matcher pairs them; the bill
uses the order's."""


def untaxed(**overrides: object) -> InvoiceExtraction:
    """The happy path's invoice with no tax: what POST@v1 will post today."""
    values: dict[str, object] = {
        "subtotal": Decimal("100.00"),
        "tax_total": Decimal("0.00"),
        "total": Decimal("100.00"),
        "line_items": [
            {
                "description": VENDOR_WORDS,
                "quantity": Decimal(1),
                "unit": "EA",
                "unit_price": Decimal("100.00"),
                "extended_price": Decimal("100.00"),
            }
        ],
    }
    values.update(overrides)
    return make_extraction(**values)


class Desk:
    """One invoice with durable stores, and the people who approve it."""

    def __init__(self, tmp_path: Path, extraction: InvoiceExtraction | None = None) -> None:
        self.store = InMemoryInvoiceStore()
        self.approvals = InMemoryApprovalStore()
        record = InvoiceRecord(source_path=tmp_path / "invoice.pdf", created_at=utc_now())
        self.store.create(record, document_sha256="c" * 64, byte_size=1, storage_uri="x")
        self.invoice_id = str(record.invoice_id)
        self.ctx = replace(
            happy_context(tmp_path, extraction or untaxed()),
            store=self.store,
            dedup=InMemoryDedupIndex(),
            approvals=self.approvals,
        )

    def record(self) -> InvoiceRecord:
        return self.store.load(self.invoice_id)

    def to_approved(self) -> InvoiceRecord:
        """Run to PENDING_APPROVAL, then two people approve. Nothing posted yet."""
        waiting = run(self.record(), self.ctx)
        assert waiting.state is InvoiceState.PENDING_APPROVAL
        once = apply_human_decision(
            waiting, self.ctx, event=InvoiceEvent.APPROVE, user_id="alice", reason="matches"
        )
        approved = apply_human_decision(
            once, self.ctx, event=InvoiceEvent.APPROVE, user_id="bob", reason="agreed"
        )
        assert approved.state is InvoiceState.APPROVED
        return approved

    def rows(self) -> list[AuditEvent]:
        writer = self.ctx.writer
        assert isinstance(writer, JsonlAuditWriter)
        return writer.read(self.invoice_id)

    def posts(self) -> list[tuple[str | None, str | None]]:
        return [
            (row.from_state, row.to_state)
            for row in self.rows()
            if row.event_type is AuditEventType.STATE_TRANSITION
            and row.from_state is InvoiceState.APPROVED
        ]

    def erp_rows(self) -> list[AuditEvent]:
        return [row for row in self.rows() if row.event_type is AuditEventType.ERP_WRITE]


@pytest.fixture
def desk(tmp_path: Path) -> Desk:
    return Desk(tmp_path)


# --- (a) approve -> one bill -----------------------------------------------


def test_an_approved_invoice_becomes_one_bill(desk: Desk, fake_qbo: FakeQbo) -> None:
    run(desk.to_approved(), desk.ctx)

    assert len(fake_qbo.bills) == 1
    bill = fake_qbo.bills[0]
    assert (bill["VendorRef"], bill["DocNumber"]) == ({"value": VENDOR}, DOC_NUMBER)
    looked = "SELECT * FROM Bill WHERE DocNumber = 'INV1'"
    assert fake_qbo.queries == [looked], "looked before it wrote"
    final = desk.record()
    assert final.erp_bill_id == bill["Id"]
    assert final.erp_idempotency_key == KEY
    assert desk.store.erp_keys == {KEY: desk.invoice_id}
    assert desk.posts() == [(InvoiceState.APPROVED, InvoiceState.POSTED)]


def test_the_trail_names_the_key_the_bill_and_how_it_was_got(desk: Desk) -> None:
    run(desk.to_approved(), desk.ctx)

    (tool,) = desk.erp_rows()
    assert getattr(tool.actor, "name", None) == "create_bill"
    assert tool.decision == "created"
    assert tool.decision_basis == f"key={KEY}, created, erp_bill_id=100"
    assert tool.output_ref is not None
    post = next(row for row in desk.rows() if row.to_state is InvoiceState.POSTED)
    assert post.actor.kind == "rule"
    assert getattr(post.actor, "rule_id", None) == "POST@v1"
    assert (post.decision_basis or "").startswith(f"rule=POST@v1, key={KEY}, created, ")
    assert "erp_bill_id=100, total=100.00 USD, lines=1" in (post.decision_basis or "")


def test_the_bill_carries_numbers_from_the_invoice_and_words_from_the_order(
    desk: Desk, fake_qbo: FakeQbo
) -> None:
    """Nothing a vendor typed reaches QuickBooks: not its name, not its line text."""
    run(desk.to_approved(), desk.ctx)

    (line,) = fake_qbo.bills[0]["Line"]
    assert line["Amount"] == 100.0
    assert line["Description"] == "Widget", "the purchase order's description"
    assert line["ItemBasedExpenseLineDetail"]["ItemRef"] == {"value": "19"}
    sent = json.dumps(fake_qbo.created)
    assert VENDOR_WORDS not in sent
    assert "Acme Ltd" not in sent


# --- (b) killed after the create returned, recovered ------------------------


class _Killed(BaseException):
    """Stands in for the process dying: no ``except Exception`` catches it."""


def test_a_run_killed_after_the_create_returned_finds_the_bill_and_makes_no_other(
    desk: Desk, fake_qbo: FakeQbo
) -> None:
    approved = desk.to_approved()

    def die() -> None:
        raise _Killed

    fake_qbo.after_create = die
    with pytest.raises(_Killed):
        run(approved, desk.ctx)
    assert len(fake_qbo.bills) == 1, "QuickBooks holds the bill"
    assert desk.record().state is InvoiceState.APPROVED, "nothing here knows it"
    assert desk.record().erp_bill_id is None
    assert not desk.erp_rows()

    fake_qbo.after_create = None
    run(desk.record(), desk.ctx)  # the recovery: the post step runs again

    assert len(fake_qbo.bills) == 1
    assert len(fake_qbo.created) == 1
    (found,) = desk.erp_rows()
    assert found.decision == "found"
    assert desk.record().erp_bill_id == fake_qbo.bills[0]["Id"]
    assert desk.posts() == [(InvoiceState.APPROVED, InvoiceState.POSTED)]


def test_a_commit_killed_before_its_save_posts_once_from_the_checkpoint(desk: Desk) -> None:
    """Decide was checkpointed with the bill id; the save died; commit runs again."""
    desk.to_approved()
    outcome = decide_iteration(desk.record(), desk.ctx)
    assert outcome.to_state is InvoiceState.POSTED
    real_save = desk.store.save

    def dies(_record: InvoiceRecord) -> None:
        raise _Killed

    desk.store.save = dies  # type: ignore[method-assign]
    with pytest.raises(_Killed):
        commit_iteration(desk.ctx, outcome)
    desk.store.save = real_save  # type: ignore[method-assign]

    commit_iteration(desk.ctx, outcome)

    assert desk.posts() == [(InvoiceState.APPROVED, InvoiceState.POSTED)]
    assert desk.record().erp_bill_id == "100"


def test_a_quickbooks_failure_leaves_it_approved_and_the_next_run_posts_once(
    desk: Desk, fake_qbo: FakeQbo, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = desk.to_approved()

    def unavailable(_entity: str, _payload: dict[str, object]) -> dict[str, object]:
        msg = "QuickBooks POST bill failed: HTTP 503"
        raise QboError(msg, status_code=503)

    real_create = fake_qbo.create
    monkeypatch.setattr(fake_qbo, "create", unavailable)
    assert run(approved, desk.ctx).state is InvoiceState.APPROVED
    assert desk.store.erp_keys == {KEY: desk.invoice_id}, "the key was written first"
    monkeypatch.setattr(fake_qbo, "create", real_create)

    run(desk.record(), desk.ctx)

    assert len(fake_qbo.bills) == 1
    assert desk.posts() == [(InvoiceState.APPROVED, InvoiceState.POSTED)]


# --- (c) a second run creates nothing ---------------------------------------


def test_a_second_run_on_a_finished_invoice_creates_nothing(desk: Desk, fake_qbo: FakeQbo) -> None:
    final = run(desk.to_approved(), desk.ctx)
    assert final.state is InvoiceState.CLOSED, "the stubs after POSTED"
    asked, rows = len(fake_qbo.queries), len(desk.rows())

    again = run(desk.record(), desk.ctx)

    assert again.state is InvoiceState.CLOSED
    assert len(fake_qbo.queries) == asked, "not even a lookup"
    assert len(fake_qbo.bills) == 1
    assert len(desk.rows()) == rows


def test_a_posted_invoice_is_never_posted_again(desk: Desk, fake_qbo: FakeQbo) -> None:
    """POSTED is not terminal; the step after it is a stub, not another bill."""
    desk.to_approved()
    InProcess(desk.record(), desk.ctx).commit(decide_iteration(desk.record(), desk.ctx))
    assert desk.record().state is InvoiceState.POSTED
    assert decide(desk.record()).kind is ActionKind.STUB

    drive(InProcess(desk.record(), desk.ctx), 1, Progress())

    assert len(fake_qbo.created) == 1


# --- (d) from APPROVED and nowhere else --------------------------------------


def test_post_leaves_approved_and_nothing_else() -> None:
    """The table says it: one source for POST, and it is the gate."""
    assert {src for (src, event) in TRANSITIONS if event == InvoiceEvent.POST.value} == {
        InvoiceState.APPROVED
    }


@pytest.mark.parametrize(
    "state", [state for state in InvoiceState if state is not InvoiceState.APPROVED]
)
def test_the_loop_chooses_create_bill_only_at_approved(desk: Desk, state: InvoiceState) -> None:
    assert (
        decide(desk.record().model_copy(update={"state": state})).kind is not ActionKind.CREATE_BILL
    )


@pytest.mark.parametrize(
    "state", [state for state in InvoiceState if state is not InvoiceState.APPROVED]
)
def test_the_post_step_is_refused_by_the_table_from_any_other_state(
    desk: Desk, fake_qbo: FakeQbo, state: InvoiceState
) -> None:
    approved = desk.to_approved()
    elsewhere = approved.model_copy(update={"state": state})
    action = decide(approved)

    with pytest.raises(IllegalTransition, match="on event 'post'"):
        apply_step(action, elsewhere, desk.ctx, StepTrail(desk.ctx, elsewhere, state))

    assert not fake_qbo.queries
    assert not desk.store.erp_keys


@pytest.fixture
def bound(desk: Desk) -> Iterator[Desk]:
    """The stores bound as the post step binds them, for calling the tool directly."""
    invoices = CURRENT_INVOICE_STORE.set(desk.store)
    approvals = CURRENT_APPROVAL_STORE.set(desk.approvals)
    try:
        yield desk
    finally:
        CURRENT_APPROVAL_STORE.reset(approvals)
        CURRENT_INVOICE_STORE.reset(invoices)


@pytest.mark.parametrize(
    "state", [state for state in InvoiceState if state is not InvoiceState.APPROVED]
)
def test_create_bill_reads_the_state_itself_and_refuses_any_but_approved(
    bound: Desk, fake_qbo: FakeQbo, state: InvoiceState
) -> None:
    """The tool does not take its caller's word: the stored state decides."""
    approved = bound.to_approved()
    payload = bill_for(approved)
    bound.store.save(approved.model_copy(update={"state": state}))

    with pytest.raises(PostingRefusedError, match="created only from APPROVED"):
        create_bill(payload)

    assert not fake_qbo.queries
    assert not bound.store.erp_keys


def test_create_bill_refuses_outside_a_run(desk: Desk, fake_qbo: FakeQbo) -> None:
    payload = bill_for(desk.to_approved())
    with pytest.raises(PostingRefusedError, match="outside a run"):
        create_bill(payload)
    assert not fake_qbo.queries


def test_create_bill_refuses_an_amount_the_approvers_did_not_see(
    bound: Desk, fake_qbo: FakeQbo
) -> None:
    payload = bill_for(bound.to_approved())
    line = payload.lines[0].model_copy(update={"amount": Decimal("90.00")})
    smaller = payload.model_copy(update={"total": Decimal("90.00"), "lines": [line]})

    with pytest.raises(PostingRefusedError, match=r"is for 100\.00 USD; the bill would be 90\.00"):
        create_bill(smaller)
    assert not fake_qbo.queries


def test_create_bill_refuses_an_approval_still_waiting(bound: Desk, fake_qbo: FakeQbo) -> None:
    waiting = run(bound.record(), bound.ctx)
    approved = waiting.model_copy(update={"state": InvoiceState.APPROVED})
    bound.store.save(approved)  # the state says approved; the approvals say otherwise

    with pytest.raises(PostingRefusedError, match="is pending, not approved"):
        create_bill(bill_for(approved))
    assert not fake_qbo.queries


# --- what goes to a person instead -------------------------------------------


def test_a_key_another_invoice_holds_goes_to_a_person(desk: Desk, fake_qbo: FakeQbo) -> None:
    """Same vendor, same number, another invoice id: the database refuses the second."""
    approved = desk.to_approved()
    desk.store.erp_keys[KEY] = "01OTHERINVOICE000000000000"

    final = run(approved, desk.ctx)

    assert final.state is InvoiceState.PENDING_HUMAN
    failed = next(row for row in desk.rows() if row.to_state is InvoiceState.EXCEPTION)
    assert "conflict: bill key" in (failed.decision_basis or "")
    assert not fake_qbo.queries


def test_a_bill_under_the_key_for_another_amount_goes_to_a_person(
    desk: Desk, fake_qbo: FakeQbo
) -> None:
    """Not ours-already-made: somebody else's bill under our number. Nothing is created."""
    fake_qbo.bills.append(
        {"Id": "7", "DocNumber": DOC_NUMBER, "VendorRef": {"value": VENDOR}, "TotalAmt": 250.0}
    )

    final = run(desk.to_approved(), desk.ctx)

    assert final.state is InvoiceState.PENDING_HUMAN
    assert not fake_qbo.created
    failed = next(row for row in desk.rows() if row.to_state is InvoiceState.EXCEPTION)
    assert "the first for 250.00; this invoice is for 100.00" in (failed.decision_basis or "")


def test_another_vendor_s_bill_with_the_same_number_is_not_ours(
    desk: Desk, fake_qbo: FakeQbo
) -> None:
    fake_qbo.bills.append(
        {"Id": "7", "DocNumber": DOC_NUMBER, "VendorRef": {"value": "99"}, "TotalAmt": 100.0}
    )
    run(desk.to_approved(), desk.ctx)
    assert len(fake_qbo.created) == 1
    assert desk.record().erp_bill_id != "7"


def test_an_invoice_with_tax_is_not_booked_short(tmp_path: Path, fake_qbo: FakeQbo) -> None:
    """Tax is not posted yet. A bill without it would understate the liability."""
    desk = Desk(tmp_path, make_extraction())  # 100.00 + 20.00 tax

    final = run(desk.to_approved(), desk.ctx)

    assert final.state is InvoiceState.PENDING_HUMAN
    failed = next(row for row in desk.rows() if row.to_state is InvoiceState.EXCEPTION)
    assert "not_posted: tax_total=20.00: tax is not posted yet" in (failed.decision_basis or "")
    assert not fake_qbo.queries
    assert not desk.store.erp_keys


def test_a_number_quickbooks_cannot_hold_is_not_posted(tmp_path: Path, fake_qbo: FakeQbo) -> None:
    desk = Desk(tmp_path, untaxed(invoice_number="INV-" + "9" * 30))
    final = run(desk.to_approved(), desk.ctx)
    assert final.state is InvoiceState.PENDING_HUMAN
    assert not fake_qbo.queries


def test_posting_needs_a_durable_store(desk: Desk, fake_qbo: FakeQbo) -> None:
    """Without one there is nowhere to write the key first, so nothing is called."""
    approved = desk.to_approved()
    ctx: RunContext = replace(desk.ctx, store=None)
    assert run(approved, ctx).state is InvoiceState.APPROVED
    assert not fake_qbo.queries


def test_an_amount_that_would_not_survive_json_is_refused() -> None:
    assert json_number(Decimal("272100.00")) == 272100.0
    with pytest.raises(PostingRefusedError, match="does not survive"):
        json_number(Decimal("1234567890123456.78"))
