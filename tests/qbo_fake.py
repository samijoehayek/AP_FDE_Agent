"""QuickBooks' Bill endpoint, as far as ``create_bill`` uses it. Never the network.

``create_bill`` builds its client through ``create_bill._client``; the suite's
autouse fixture (``tests/conftest.py``) replaces that with one of these for
every test, so no test can post a bill anywhere real.

With a ``path`` the bills live in a JSON file, so two processes - one killed,
one recovering - share one ledger, the way they would share QuickBooks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_DOC_NUMBER = re.compile(r"WHERE DocNumber = '([^']*)'")


@dataclass
class FakeQbo:
    """Bills by id, a log of what was asked, and an optional hook after each create."""

    path: Path | None = None
    queries: list[str] = field(default_factory=list[str])
    created: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    after_create: Callable[[], None] | None = None
    """Runs once QuickBooks holds the bill and before the caller hears back."""
    _bills: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    @property
    def bills(self) -> list[dict[str, Any]]:
        """Every bill QuickBooks holds."""
        if self.path is not None and self.path.exists():
            loaded: list[dict[str, Any]] = json.loads(self.path.read_text())
            return loaded
        return self._bills if self.path is None else []

    def query(self, statement: str) -> dict[str, Any]:
        """Answer ``SELECT * FROM Bill WHERE DocNumber = '...'``; nothing else is asked."""
        self.queries.append(statement)
        found = _DOC_NUMBER.search(statement)
        assert found is not None, f"create_bill asked something unexpected: {statement}"
        rows = [bill for bill in self.bills if bill["DocNumber"] == found.group(1)]
        return {"QueryResponse": {"Bill": rows} if rows else {}}

    def create(self, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Hold the bill, assign it an id, then let the hook run."""
        assert entity == "Bill"
        bills = self.bills
        bill = {
            **payload,
            "Id": str(100 + len(bills)),
            "TotalAmt": sum(line["Amount"] for line in payload["Line"]),
        }
        bills.append(bill)
        if self.path is not None:
            self.path.write_text(json.dumps(bills))
        self.created.append(payload)
        if self.after_create is not None:
            self.after_create()
        return {"Bill": bill}
