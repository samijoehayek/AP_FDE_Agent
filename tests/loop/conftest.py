"""Shared stand-ins for the loop tests.

The explanation seat is faked everywhere a loop test reaches EXCEPTION, so no
loop test can reach the API - not even to be refused by the sentinel key.
"""

from __future__ import annotations

import pytest

from ap_agent.contracts.enums import SuggestedAction, SuggestedResolver
from ap_agent.contracts.exceptions import ExceptionClassification
from ap_agent.loop import runner as runner_module
from ap_agent.tools.classify_exception import ClassifyExceptionInput, ClassifyExceptionOutput

FAKE_SUMMARY = "PO line 1 does not agree with the order. Review before paying."


def fake_classify(payload: ClassifyExceptionInput) -> ClassifyExceptionOutput:
    """A seat that leads with the first reported code and routes to the AP clerk."""
    return ClassifyExceptionOutput(
        classification=ExceptionClassification(
            reason_code=payload.match_result.reason_codes[0],
            suggested_resolver=SuggestedResolver.AP_CLERK,
            human_summary=FAKE_SUMMARY,
            suggested_action=SuggestedAction.HOLD_FOR_MANUAL_REVIEW,
        ),
        model_id="claude-sonnet-5",
        prompt_version="classify_v1",
        input_tokens=1500,
        output_tokens=120,
        latency_ms=900,
    )


@pytest.fixture(autouse=True)
def no_real_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the real seat unreachable from any loop test.

    A context that forgot ``classify=fake_classify`` would otherwise send a
    request with the sentinel key. This turns that into a loud failure instead.
    """

    def _refuse(_payload: ClassifyExceptionInput) -> ClassifyExceptionOutput:
        msg = "a loop test reached the real classify_exception; inject fake_classify"
        raise AssertionError(msg)

    monkeypatch.setattr(runner_module, "classify_exception", _refuse)
