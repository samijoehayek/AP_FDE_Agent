"""Exception hierarchy for ap-agent.

Every error raised by this package inherits from :class:`APAgentError` so that
the agent loop can distinguish a failure it owns from a failure in a
dependency. ``error_class`` on an :class:`~ap_agent.contracts.audit.AuditEvent`
is the qualified name of one of these types.
"""

from __future__ import annotations


class APAgentError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigurationError(APAgentError):
    """Settings or a versioned config file is missing, malformed, or unsafe."""


class IllegalTransition(APAgentError):  # noqa: N818 - named in the state-machine spec
    """A state transition was attempted that is not in the transition table.

    This is always a programming error or a corrupted invoice row - never a
    business outcome. Business outcomes are themselves states (``EXCEPTION``,
    ``REJECTED``) and have their own edges in the table.
    """


class IngestionError(APAgentError):
    """A document could not be read, hashed, or paginated."""


class UnsupportedDocumentError(IngestionError):
    """The document's media type is not one this pipeline accepts."""


class ExtractionError(APAgentError):
    """Reading a document into an :class:`InvoiceExtraction` failed.

    Covers the API call and the validation of what came back. Both are failures
    of the same step from the caller's point of view - there is no extraction -
    and both route the invoice to ``NEEDS_HUMAN_EXTRACTION``. The underlying
    cause is always chained, so the audit event can record what actually broke.
    """
