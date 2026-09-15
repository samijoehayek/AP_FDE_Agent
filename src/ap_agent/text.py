"""Reducing printed strings to what identifies the thing they name.

One module, because two callers need the same answer and two implementations
would agree right up until one of them was edited. ``lookup_vendor`` decides
whether two names are the same supplier; ``matching.pairing`` decides whether
two descriptions are the same ordered item. Both are asking "is this rendering
or is this identity", both are comparing a string a vendor printed against a
string the buyer's system holds, and a disagreement between them would show up
as an invoice matched to the wrong line - or paid twice under two vendor ids.

It lives here rather than in ``tools/`` because a normaliser is not a tool. It
calls nothing, touches nothing, and belongs to whichever code needs it; keeping
it under ``tools`` also made the matcher import the whole tool package, which
imports the matcher.

Pure. No I/O, no config, no clock.
"""

from __future__ import annotations

import re

__all__ = ["LEGAL_SUFFIXES", "normalise_vendor_name"]

LEGAL_SUFFIXES: frozenset[str] = frozenset(
    {
        "ltd",
        "limited",
        "pvt",
        "private",
        "inc",
        "incorporated",
        "llc",
        "llp",
        "plc",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "bv",
        "sa",
        "sarl",
        "ag",
        "pty",
    }
)
"""Tokens that say how a company is incorporated, not which company it is.

Stripped from the *end* only. "Acme Ltd" and "ACME Limited" are one supplier
with two renderings, and an AP function that treated them as two would pay the
same invoice twice under different vendor ids. Stripping them anywhere in the
string would be wrong: "Limited Editions Ltd" is a name.
"""

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalise_vendor_name(name: str) -> str:
    """Reduce a printed vendor name to what identifies the company.

    Case, punctuation, spacing and legal suffix are rendering, not identity. A
    supplier prints "ACME Ltd." on one template and "Acme Limited" on the next,
    and both are the same party being paid.

    Pure, and its own function rather than three lines inside the lookup,
    because the cost of getting it wrong is asymmetric: too aggressive and two
    real suppliers collapse into one, too timid and the same supplier is
    onboarded twice. That trade-off deserves to be argued with in its own tests.

    Args:
        name: The name as printed on the document or held in the master.

    Returns:
        The normalised form: casefolded, punctuation removed, whitespace
        collapsed, trailing legal suffixes dropped. May be empty if the name was
        nothing but punctuation and suffixes, and an empty result never matches.
    """
    folded = _PUNCTUATION.sub(" ", name.casefold())
    tokens = _WHITESPACE.sub(" ", folded).strip().split(" ")
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)
