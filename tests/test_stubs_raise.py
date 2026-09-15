"""Every stub raises NotImplementedError.

A stub that quietly returns ``None`` or an empty result is how a half-built
pipeline gets shipped. These tests are deleted as each module is implemented -
their failure is the reminder to update the docs and this file together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ap_agent.audit.chain import ChainVerification, compute_event_hash, verify_chain
from ap_agent.audit.writer import PostgresAuditWriter
from ap_agent.evals.golden import GoldenCase, load_golden_set, score_extraction
from ap_agent.loop.agent import run_invoice


def test_agent_loop_is_stubbed() -> None:
    with pytest.raises(NotImplementedError):
        run_invoice("inv-1", run_id="run-1")


def test_audit_chain_is_stubbed() -> None:
    with pytest.raises(NotImplementedError):
        compute_event_hash(None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        verify_chain([])
    with pytest.raises(NotImplementedError):
        ChainVerification()


def test_audit_writer_is_stubbed() -> None:
    with pytest.raises(NotImplementedError):
        PostgresAuditWriter()


def test_evals_are_stubbed() -> None:
    with pytest.raises(NotImplementedError):
        GoldenCase()
    with pytest.raises(NotImplementedError):
        load_golden_set(Path("data"))
    with pytest.raises(NotImplementedError):
        score_extraction(None, None)  # type: ignore[arg-type]
