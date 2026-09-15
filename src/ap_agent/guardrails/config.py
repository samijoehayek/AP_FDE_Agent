"""Loader for the versioned tolerance and approval config.

The rules that decide money live in ``config/guardrails.v<N>.yaml``, not in
Python, for three reasons a reviewer should be able to check:

1. **They change on a different clock than the code.** A controller raising the
   price tolerance from 2% to 3% should not require a deploy.
2. **They must be versionable.** Every ``MatchResult`` and every ``AuditEvent``
   carries the ``config_version`` that produced it, so a decision made last
   quarter can be re-explained under last quarter's rules. That is only possible
   if old versions are files that still exist.
3. **They must be reviewable by someone who does not read Python.** The people
   who own an approval matrix are in finance.

The file is data. It is read with ``yaml.safe_load``, validated against
:class:`~ap_agent.contracts.guardrails.GuardrailConfig`, and rejected if it does
not conform.

**There are no defaults here.** Not one value falls back to a constant. A
tolerance the file forgot to state is a startup failure, because the alternative
is a run that names ``guardrails_v1`` in its audit trail while using a number
that appears nowhere in ``guardrails.v1.yaml`` - which makes the trail's own
claim about which rules applied untrue.

The version is checked against the filename for the same reason. ``config_version``
is how a past decision is looked up; a file whose contents disagree with its own
name turns that lookup into a guess.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

import yaml
from pydantic import ValidationError

from ap_agent.config import get_settings
from ap_agent.contracts.guardrails import GuardrailConfig
from ap_agent.errors import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["GuardrailsError", "load_guardrails", "version_from_filename"]


class GuardrailsError(ConfigurationError):
    """The guardrails file is missing, malformed, or disagrees with its own name.

    Always fatal. Every invoice is decided against this file, so a broken one is
    not a per-invoice problem to route around - it is a configuration failure,
    and the honest response is to refuse to start.
    """


def version_from_filename(path: Path) -> str:
    """Return the ``config_version`` a file of this name must declare.

    ``guardrails.v1.yaml`` must say ``guardrails_v1``. The mapping is mechanical
    so that neither can be changed without the other: renaming the file without
    editing it, or editing the version without renaming, both fail loudly rather
    than producing a trail whose ``config_version`` points at nothing.
    """
    return path.name.removesuffix(".yaml").removesuffix(".yml").replace(".", "_")


@lru_cache(maxsize=4)
def load_guardrails(path: Path | None = None) -> GuardrailConfig:
    """Load and validate a versioned guardrails file.

    Cached per path, because every invoice in a run must be decided against the
    same numbers: a file that changed mid-batch would make two invoices
    incomparable with nothing in either trail to say why.

    Args:
        path: A ``guardrails.v<N>.yaml``. Defaults to
            ``Settings.guardrails_config_path``.

    Returns:
        The validated config.

    Raises:
        GuardrailsError: The file is absent, unparseable, declares a version
            that does not match its filename, is missing a value, or carries a
            key the contract does not declare. All four are fatal - see the
            module docstring for why none of them gets a default.
    """
    source = path or get_settings().guardrails_config_path
    if not source.is_file():
        msg = f"no guardrails config at {source}"
        raise GuardrailsError(msg)

    try:
        loaded: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"{source.name} is not valid YAML: {exc}"
        raise GuardrailsError(msg) from exc

    if not isinstance(loaded, dict):
        msg = f"{source.name} must be a mapping"
        raise GuardrailsError(msg)

    try:
        config = GuardrailConfig.model_validate(cast("dict[str, Any]", loaded))
    except ValidationError as exc:
        # Reported in full. A guardrails file is short and the person fixing it
        # needs to know every field that is wrong, not the first one.
        msg = f"{source.name} is not a valid guardrails config:\n{exc}"
        raise GuardrailsError(msg) from exc

    expected = version_from_filename(source)
    if config.config_version != expected:
        msg = (
            f"{source.name} declares config_version {config.config_version!r} "
            f"but its filename says {expected!r}. An audit row naming a version "
            f"has to be able to find the file that produced it."
        )
        raise GuardrailsError(msg)

    return config
