"""SQLAlchemy declarative base and engine/session factories."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ap_agent.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.engine import Engine

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""Deterministic constraint names.

Without this, Alembic autogenerates anonymous names and a later migration that
needs to drop a constraint cannot name it. Set once, at the start, because
changing it later means renaming every constraint in production.

Note that SQLAlchemy applies a convention only to *unnamed* constraints. Pass
``name=`` and it is used verbatim - which is why unique constraints in
``models.py`` are left unnamed and check constraints pass only the short
``constraint_name`` the ``ck`` template interpolates.
"""


class Base(DeclarativeBase):
    """Declarative base for every table in this application."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_db_engine(url: str | None = None, **kwargs: Any) -> Engine:  # noqa: ANN401
    """Create an engine against ``url`` or the configured ``DATABASE_URL``.

    ``kwargs`` is an untyped passthrough to :func:`sqlalchemy.create_engine`,
    whose options are dialect-dependent and not worth mirroring here.
    """
    settings = get_settings()
    return create_engine(url or settings.database_url, pool_pre_ping=True, future=True, **kwargs)


def create_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Create a session factory bound to ``engine`` or a fresh one."""
    return sessionmaker(bind=engine or create_db_engine(), expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    """Run a unit of work in one transaction, rolling back on error.

    The audit trail depends on this: a state change and the audit event that
    records it must commit together or not at all.
    """
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
