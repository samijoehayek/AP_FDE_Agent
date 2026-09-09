"""The append-only trail and the hash chain over it."""

from __future__ import annotations

from pathlib import Path

import pytest

from ap_agent.audit.writer import JsonlAuditWriter, chain_hash
from ap_agent.contracts.audit import GENESIS_HASH, AuditEvent, SystemActor, utc_now
from ap_agent.contracts.enums import AuditEventType


def _event(invoice_id: str = "inv-1", step_seq: int = 0, **overrides: object) -> AuditEvent:
    payload: dict[str, object] = {
        "invoice_id": invoice_id,
        "run_id": "run-1",
        "step_seq": step_seq,
        "ts_utc": utc_now(),
        "actor": SystemActor(),
        "event_type": AuditEventType.NOTE,
    }
    payload.update(overrides)
    return AuditEvent.model_validate(payload)


@pytest.fixture
def writer(tmp_path: Path) -> JsonlAuditWriter:
    return JsonlAuditWriter(tmp_path / "audit")


# --- appending --------------------------------------------------------------


def test_the_first_event_starts_at_genesis(writer: JsonlAuditWriter) -> None:
    """An empty string cannot satisfy the contract's SHA-256 field, so zeros do."""
    assert writer.append(_event()).prev_event_hash == GENESIS_HASH


def test_each_event_chains_to_the_line_before_it(writer: JsonlAuditWriter) -> None:
    writer.append(_event(step_seq=0))
    second = writer.append(_event(step_seq=1))
    first_line = writer.path_for("inv-1").read_text(encoding="utf-8").splitlines()[0]
    assert second.prev_event_hash == chain_hash(first_line)


def test_a_caller_supplied_predecessor_is_ignored(writer: JsonlAuditWriter) -> None:
    """Only the writer knows what is actually last.

    Letting a caller pick its own predecessor would make the chain decorative.
    """
    writer.append(_event(step_seq=0))
    forged = writer.append(_event(step_seq=1, prev_event_hash="f" * 64))
    assert forged.prev_event_hash != "f" * 64


def test_one_file_per_invoice(writer: JsonlAuditWriter) -> None:
    writer.append(_event(invoice_id="inv-a"))
    writer.append(_event(invoice_id="inv-b"))
    assert writer.path_for("inv-a") != writer.path_for("inv-b")
    assert len(writer.read("inv-a")) == 1


def test_chains_of_different_invoices_are_independent(writer: JsonlAuditWriter) -> None:
    writer.append(_event(invoice_id="inv-a"))
    writer.append(_event(invoice_id="inv-b"))
    assert writer.append(_event(invoice_id="inv-b", step_seq=1)).prev_event_hash != GENESIS_HASH
    assert writer.verify("inv-a")
    assert writer.verify("inv-b")


def test_head_returns_the_last_event(writer: JsonlAuditWriter) -> None:
    assert writer.head("inv-1") is None
    writer.append(_event(step_seq=0))
    writer.append(_event(step_seq=7))
    head = writer.head("inv-1")
    assert head is not None
    assert head.step_seq == 7


# --- verification -----------------------------------------------------------


def test_an_untouched_chain_verifies(writer: JsonlAuditWriter) -> None:
    for seq in range(5):
        writer.append(_event(step_seq=seq))
    assert writer.verify("inv-1")
    assert writer.verify_detailed("inv-1") == (True, None)


def test_a_tampered_line_is_detected(writer: JsonlAuditWriter) -> None:
    """The point of the whole exercise: an edit anywhere breaks everything after it."""
    for seq in range(4):
        writer.append(_event(step_seq=seq, decision="approved"))
    path = writer.path_for("inv-1")

    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace('"decision":"approved"', '"decision":"tampered"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert not writer.verify("inv-1")
    intact, first_break = writer.verify_detailed("inv-1")
    assert not intact
    assert first_break == 2  # the line AFTER the edit is where the link fails


def test_a_removed_line_is_detected(writer: JsonlAuditWriter) -> None:
    for seq in range(4):
        writer.append(_event(step_seq=seq))
    path = writer.path_for("inv-1")
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:1] + lines[2:]) + "\n", encoding="utf-8")
    assert not writer.verify("inv-1")


def test_reordered_lines_are_detected(writer: JsonlAuditWriter) -> None:
    for seq in range(3):
        writer.append(_event(step_seq=seq))
    path = writer.path_for("inv-1")
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2], lines[1]]) + "\n", encoding="utf-8")
    assert not writer.verify("inv-1")


def test_an_appended_forgery_is_detected(writer: JsonlAuditWriter) -> None:
    """Someone who can write the file can still append - but not silently."""
    writer.append(_event(step_seq=0))
    path = writer.path_for("inv-1")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_event(step_seq=1, decision="approved").model_dump_json() + "\n")
    assert not writer.verify("inv-1")


def test_a_malformed_line_counts_as_broken(writer: JsonlAuditWriter) -> None:
    """An unparseable line is evidence of something, not evidence of nothing."""
    writer.append(_event(step_seq=0))
    with writer.path_for("inv-1").open("a", encoding="utf-8") as handle:
        handle.write("{not json}\n")
    assert not writer.verify("inv-1")


def test_an_unknown_invoice_verifies_vacuously(writer: JsonlAuditWriter) -> None:
    assert writer.verify("never-seen")


def test_the_hash_covers_the_bytes_on_disk(writer: JsonlAuditWriter) -> None:
    """Hashing a re-serialisation would let formatting look like tampering."""
    writer.append(_event(step_seq=0))
    line = writer.path_for("inv-1").read_text(encoding="utf-8").splitlines()[0]
    assert chain_hash(line) == chain_hash(line + "\n")
