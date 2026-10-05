"""The output filter: what a model's reading may not carry into code that acts on it.

Responsible for one question, asked of every structured output a reading seat
returns: does any transcribed field contain something a document has no honest
reason to put there - an account number, a link, an instruction addressed to a
model? And has the reader itself reported instruction-like text in
``suspicious_text``?

What it deliberately does not do:

* **Rewrite.** ``never_auto_fix`` is true in the guardrails file and is enforced
  here by having no code path that edits a reading. Stripping an IBAN out of a
  field would destroy the evidence that somebody put one there.
* **Route.** It returns flags; the loop decides that a flagged reading goes to a
  person.
* **Read ``remit_to_display``.** The remit-to block prints payment details by
  design, so a pattern match there says nothing. ``lookup_vendor`` compares it
  to the vendor master server-side instead.
* **Return what matched.** A flag names the pattern and the field, never the
  text, so a planted instruction cannot reach the audit trail through the check
  that caught it.

Pure. No I/O, no clock, no model: the same reading under the same config always
produces the same flags, which is what lets a past decision replay.
"""

from __future__ import annotations

import re
from functools import cache
from typing import TYPE_CHECKING

from ap_agent.contracts.screening import OutputFlag

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ap_agent.contracts.guardrails import OutputFilter
    from ap_agent.contracts.invoice import InvoiceExtraction

__all__ = ["SUSPICIOUS_TEXT", "screen_extraction"]

SUSPICIOUS_TEXT = "suspicious_text"
"""The check name when the reader itself reported instruction-like text.

Not a pattern: the reading seat was told to copy anything that reads like an
instruction into this field, so anything there at all is the reader telling the
system what it saw. Its contents are never screened or echoed - only its
presence counts.
"""


@cache
def _compiled(pattern: str) -> re.Pattern[str]:
    """Compile once per pattern string. Patterns come from a reviewed file."""
    return re.compile(pattern)


def _string_fields(extraction: InvoiceExtraction) -> Iterator[tuple[str, str]]:
    """Every transcribed string on a reading, as ``(field_path, value)``.

    Paths drop list indices (``line_items.description``) because a skip rule is
    about what kind of field a value is, not which line it sat on. An evidence
    snippet is filed under the field it cites, so it is skipped exactly when the
    field itself would be: the snippet behind an invoice number is still an
    invoice number.

    ``suspicious_text`` is not yielded. Its presence is the signal, and running
    patterns over it would only report the same finding again.
    """
    header = (
        "vendor_name",
        "vendor_tax_id",
        "vendor_email_domain",
        "vendor_address",
        "bill_to_name",
        "bill_to_tax_id",
        "invoice_number",
        "payment_terms",
        "remit_to_display",
    )
    for name in header:
        value = getattr(extraction, name)
        if isinstance(value, str) and value:
            yield name, value
    for reference in extraction.po_references:
        yield "po_references", reference
    for line in extraction.line_items:
        yield "line_items.description", line.description
        if line.unit:
            yield "line_items.unit", line.unit
        if line.po_line_ref:
            yield "line_items.po_line_ref", line.po_line_ref
    for entry in extraction.evidence:
        yield entry.field.value, entry.snippet


def screen_extraction(extraction: InvoiceExtraction, config: OutputFilter) -> list[OutputFlag]:
    """Return every pattern that fired on this reading, one flag per pattern and field.

    Args:
        extraction: One reading's structured output, as the seat returned it.
        config: The ``output_filter`` section of the versioned guardrails.

    Returns:
        Flags in a stable order - ``suspicious_text`` first, then patterns in the
        order the config lists them, then fields in the order they were read.
        Empty means the reading may proceed.
    """
    flags: list[OutputFlag] = []
    if any(text.strip() for text in extraction.suspicious_text):
        flags.append(OutputFlag(check=SUSPICIOUS_TEXT, field=SUSPICIOUS_TEXT))

    unscreened = set(config.unscreened_fields)
    fields = [(path, value) for path, value in _string_fields(extraction) if path not in unscreened]
    for pattern in config.patterns:
        compiled = _compiled(pattern.pattern)
        skipped = set(pattern.skip_fields)
        seen: set[str] = set()
        for path, value in fields:
            if path in skipped or path in seen:
                continue
            if compiled.search(value):
                seen.add(path)
                flags.append(OutputFlag(check=pattern.name, field=path))
    return flags
