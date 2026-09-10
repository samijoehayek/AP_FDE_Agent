"""Clients for systems outside this one.

Each integration is a thin, typed boundary: authentication, transport, retries
and error mapping, and nothing else. Business logic lives in ``tools/`` and
``loop/``, so that "what QuickBooks calls a purchase order" and "what this
system does about one" stay separable.
"""
