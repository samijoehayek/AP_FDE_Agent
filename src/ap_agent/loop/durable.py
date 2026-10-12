"""Durable runs: the loop as a DBOS workflow, each half of an iteration a step.

Responsible for one thing - that a run killed part-way through finishes from
where it stopped, writing each transition row once. The loop's control flow is
not here: :func:`ap_agent.loop.runner.drive` runs the same decide/commit pair in
process and in a workflow, so a recovered run replays the code every test runs.

How the pieces divide the work:

* **The invoices table says which state.** DBOS never holds an invoice. The
  decide step's checkpoint is a :class:`StepOutcome` - codes, a basis, and a
  payload reference to the record - and every step reloads from the store.
* **DBOS says which step.** A step that returned is checkpointed and is not run
  again on recovery; the step the process died inside *is* run again. Steps are
  at least once, never exactly once - which is why commit's row has a position
  fixed by decide, and why a second write there is refused.
* **A halt ends the workflow.** A run that stops for a person - PENDING_HUMAN,
  PENDING_APPROVAL, a duplicate hold - is finished. ``just decide`` starts a new
  one from the stored state. No workflow waits on ``DBOS.recv``: a person takes
  days, and that wait belongs in the approvals and invoices rows, which survive
  a deploy, not in a workflow that would need the same code to still be running.

What this deliberately does not do:

* **Recover anyone else's run.** Each process runs one invoice under the
  executor id ``run:<run_id>``. DBOS recovers pending workflows on launch for
  its own executor id only; with the default shared id (``local``) a second
  process's launch picked up a run the first was still executing and ran its
  current step twice, concurrently. Measured on 2026-10-12; see DECISIONS.md.
  The caller holds the invoice's lock, so the only process that can be
  recovering a run is the one that knows its owner is dead.
* **Keep connections.** DBOS has its own small pool; :func:`execute` destroys it
  before returning. The application's engines stay on ``NullPool``.
* **Create tables.** ``run_migrations`` is off: the ``dbos`` schema is created
  by the owner (``just db-migrate``), and the application role only uses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from dbos import DBOS, DBOSClient, DBOSConfig, DBOSPortableJSONSerializer, SetWorkflowID

from ap_agent.config import get_settings
from ap_agent.contracts.run import StepOutcome
from ap_agent.errors import ConfigurationError
from ap_agent.loop.runner import (
    Progress,
    commit_iteration,
    decide_iteration,
    drive,
    escalation,
)

if TYPE_CHECKING:
    from ap_agent.loop.runner import RunContext
    from ap_agent.persistence import InvoiceStore

__all__ = [
    "APP_NAME",
    "SYSTEM_SCHEMA",
    "WORKFLOW_VERSION",
    "execute",
    "workflow_status",
]

APP_NAME: Final = "ap-agent"
SYSTEM_SCHEMA: Final = "dbos"

WORKFLOW_VERSION: Final = "loop-v1"
"""DBOS recovers a workflow only under the application version that started it.

Left to itself DBOS hashes the workflow source, so any edit to this module would
strand every run in flight. Pinned instead, and bumped by hand when the *shape*
of the workflow changes - its steps, their order, their arguments - because that
is when a recorded run can no longer be replayed by the new code.
"""

UNFINISHED: Final = frozenset({"PENDING", "ENQUEUED"})
"""Statuses of a workflow that DBOS will still drive: attach to these."""

_POOL_SIZE: Final = 2
"""One run per process; DBOS's default of 20 is sized for a long-lived worker."""


@dataclass
class _Bound:
    """The context the steps run against, set by :func:`execute` for one run.

    A step's arguments are checkpointed, so they must be plain values; a
    ``RunContext`` holds callables and connections. And a recovered workflow is
    re-entered by DBOS, not by our caller. So the context is bound here, once
    per process, and the steps read it.
    """

    ctx: RunContext | None = None

    def get(self) -> RunContext:
        if self.ctx is None or self.ctx.store is None:
            msg = "a durable step ran with no run bound; call execute()"
            raise ConfigurationError(msg)
        return self.ctx

    def store(self) -> InvoiceStore:
        store = self.get().store
        assert store is not None  # noqa: S101 - get() refused a context without one
        return store


_BOUND = _Bound()


@DBOS.step()
def _start_step(invoice_id: str) -> None:
    _BOUND.store().start_run(_BOUND.get().run_id, invoice_id)


@DBOS.step()
def _decide_step(invoice_id: str) -> str:
    ctx = _BOUND.get()
    return decide_iteration(_BOUND.store().load(invoice_id), ctx).model_dump_json()


@DBOS.step()
def _commit_step(outcome: str) -> None:
    commit_iteration(_BOUND.get(), StepOutcome.model_validate_json(outcome))


@DBOS.step()
def _escalation_step(invoice_id: str, max_steps: int) -> str:
    ctx = _BOUND.get()
    return escalation(_BOUND.store().load(invoice_id), ctx, max_steps).model_dump_json()


@DBOS.step()
def _finish_step(invoice_id: str, step_count: int, last_step: str | None) -> None:
    store = _BOUND.store()
    store.finish_run(
        _BOUND.get().run_id,
        step_count=step_count,
        last_step=last_step,
        last_state=store.load(invoice_id).state,
    )


@dataclass(frozen=True)
class _Durable:
    """Each half of an iteration as a checkpointed step. See ``runner.Iterations``."""

    invoice_id: str

    def decide(self) -> StepOutcome:
        return StepOutcome.model_validate_json(_decide_step(self.invoice_id))

    def commit(self, outcome: StepOutcome) -> None:
        _commit_step(outcome.model_dump_json())

    def escalate(self, max_steps: int) -> None:
        # Two steps, like any iteration: the row's position is fixed by the
        # first, so a second try of the write lands on it and writes nothing.
        _commit_step(_escalation_step(self.invoice_id, max_steps))


@DBOS.workflow()
def invoice_workflow(invoice_id: str, max_steps: int) -> None:
    """One run of one invoice. The workflow id is the run id."""
    _start_step(invoice_id)
    progress = Progress()
    try:
        drive(_Durable(invoice_id), max_steps, progress)
    finally:
        _finish_step(invoice_id, progress.steps, progress.last_step)


def _config(run_id: str) -> DBOSConfig:
    return {
        "name": APP_NAME,
        "system_database_url": get_settings().database_url,
        "dbos_system_schema": SYSTEM_SCHEMA,
        "run_migrations": False,
        "sys_db_pool_size": _POOL_SIZE,
        "application_version": WORKFLOW_VERSION,
        "executor_id": f"run:{run_id}",
        "serializer": DBOSPortableJSONSerializer(),
        "log_level": "WARNING",
    }


def execute(invoice_id: str, ctx: RunContext, max_steps: int) -> None:
    """Run ``ctx.run_id`` durably in this process - or finish it, if it was started before.

    Launching DBOS under this run's executor id recovers the run if a process
    that died left it pending; otherwise the workflow is started here. Either
    way this returns when the run is over, with DBOS's connections closed.

    The caller holds the invoice's lock. Without it, two processes could both be
    recovering the same run.
    """
    if ctx.store is None:
        msg = "a durable run needs a store: the invoices table says which state"
        raise ConfigurationError(msg)
    DBOS(config=_config(ctx.run_id))
    _BOUND.ctx = ctx
    try:
        DBOS.launch()
        status = DBOS.get_workflow_status(ctx.run_id)
        if status is None:
            with SetWorkflowID(ctx.run_id):
                invoice_workflow(invoice_id, max_steps)
        else:
            DBOS.retrieve_workflow(ctx.run_id).get_result()
    finally:
        DBOS.destroy()
        _BOUND.ctx = None


def workflow_status(run_id: str) -> str | None:
    """The DBOS status of ``run_id``, or None if no workflow was ever started for it.

    Read with a client, not a launched DBOS: launching would start recovering
    the run as a side effect of asking about it.
    """
    client = DBOSClient(
        system_database_url=get_settings().database_url,
        dbos_system_schema=SYSTEM_SCHEMA,
        system_database_pool_size=1,
    )
    try:
        found = client.list_workflows(workflow_ids=[run_id], load_input=False, load_output=False)
    finally:
        client.destroy()
    return found[0].status if found else None
