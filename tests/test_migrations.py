"""The migration chain, checked without a database.

Rendering to SQL offline catches the errors that matter here - a broken chain, a
revision that will not compile, a missing append-only revoke - without needing a
running Postgres, so these run in every environment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from ap_agent.config import REPO_ROOT
from ap_agent.db.base import Base
from ap_agent.db.models import ApprovalRequest, AuditEventRow, Document, ErpWrite, Invoice

EXPECTED_TABLES = frozenset(
    {
        ApprovalRequest.__tablename__,
        AuditEventRow.__tablename__,
        Document.__tablename__,
        ErpWrite.__tablename__,
        Invoice.__tablename__,
    }
)


@pytest.fixture(scope="module")
def script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))


def test_there_is_exactly_one_head(script: ScriptDirectory) -> None:
    """Two heads means a merge nobody noticed, and `upgrade head` will fail."""
    assert len(script.get_heads()) == 1


def test_every_revision_has_a_docstring(script: ScriptDirectory) -> None:
    for revision in script.walk_revisions():
        assert revision.doc, f"{revision.revision} has no message"


def test_the_models_and_the_metadata_agree() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def _initial_revision_source() -> str:
    versions = REPO_ROOT / "src" / "ap_agent" / "db" / "migrations" / "versions"
    files = sorted(p for p in versions.glob("*.py") if p.name != "__init__.py")
    assert files, "no migrations found"
    return files[0].read_text(encoding="utf-8")


def test_the_initial_revision_creates_every_table() -> None:
    source = _initial_revision_source()
    for table in EXPECTED_TABLES:
        assert f'op.create_table("{table}"' in source or f"op.create_table('{table}'" in source


def test_the_audit_table_is_revoked_not_merely_documented() -> None:
    """Rule 4's teeth: append-only is a grant, not a convention."""
    source = _initial_revision_source()
    assert "REVOKE UPDATE, DELETE, TRUNCATE" in source
    assert "audit_events" in source


def test_no_migration_drops_the_audit_table_outside_downgrade() -> None:
    """A migration that quietly drops the trail is the one to catch in review."""
    versions = REPO_ROOT / "src" / "ap_agent" / "db" / "migrations" / "versions"
    for path in versions.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        upgrade = source.split("def downgrade")[0]
        assert not re.search(r"drop_table\(\s*['\"]audit_events", upgrade), path.name


def test_the_alembic_url_is_not_committed() -> None:
    """A committed connection string is a committed password."""
    ini = (REPO_ROOT / "alembic.ini").read_text(encoding="utf-8")
    assert "sqlalchemy.url" not in ini.replace("# ", "")
    assert (
        Path(REPO_ROOT / "src/ap_agent/db/migrations/env.py")
        .read_text()
        .count("get_settings().database_url")
    )
