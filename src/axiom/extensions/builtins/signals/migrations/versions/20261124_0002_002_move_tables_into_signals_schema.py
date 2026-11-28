# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Move signals, media, participants and people from public into the signals schema.

Revision ID: 002
Revises: 001
Create Date: 2026-11-24

ADR-052 gives an extension its own schema. Revision 001 built these four tables in ``public`` and
every install still holds them there (issue 869).

``ALTER TABLE ... SET SCHEMA`` is a catalogue change, not a copy: the rows, the nine indexes, the
primary keys and the participants -> media foreign key all go with the table, and nothing is
rewritten. It takes a brief exclusive lock on each table.

Expand then contract (ADR-182). The release before this one already looks for the tables in
``signals`` first, so a node still running it keeps working once they have moved. No compatibility
view is left behind: the old release's start-up runs ``CREATE INDEX IF NOT EXISTS`` on the table
name, which Postgres refuses on a view.

Revision 001 is left as it was. A fresh database still builds the tables in ``public`` for an
instant and this revision moves them, so a new node and an old one end in the same place.

If a store that started before this ran has already made its own tables in ``signals``, there can
be a set in each schema:

* the ``public`` set is empty: it is dropped, and the ``signals`` set is kept;
* the ``signals`` set is empty: it is dropped, and the ``public`` set is moved;
* both hold rows: this stops. Merging two sets of the same table by guessing loses data
  quietly, so the operator is told which tables to reconcile.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# contract: the signals tables in public unused since 0.70.47
#
# 0.70.47 is the release that looks for the tables in ``signals`` first. A node applies this
# revision only once it is shown to run that release or a later one (ADR-182 D4a).

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "signals"
#: Dependency order: participants references media. Dropping runs in reverse.
TABLES = ("signals", "media", "people", "participants")


def _exists(bind, schema: str, table: str) -> bool:
    return (
        bind.execute(sa.text("SELECT to_regclass(:name)"), {"name": f"{schema}.{table}"}).scalar()
        is not None
    )


def _rows(bind, schema: str, table: str) -> int:
    # nosemgrep: avoid-sqlalchemy-text - identifiers are this module's constants, never input
    return bind.execute(sa.text(f'SELECT count(*) FROM "{schema}"."{table}"')).scalar()  # noqa: S608


def _held(bind, schema: str) -> list[str]:
    """The tables of this extension that *schema* holds rows in."""
    return [t for t in TABLES if _exists(bind, schema, t) and _rows(bind, schema, t) > 0]


def _move(bind, source: str, target: str) -> None:
    for table in TABLES:
        if _exists(bind, source, table):
            # nosemgrep: avoid-sqlalchemy-text - identifiers are this module's constants, never input
            op.execute(sa.text(f'ALTER TABLE "{source}"."{table}" SET SCHEMA "{target}"'))


def _drop_set(bind, schema: str) -> None:
    for table in reversed(TABLES):
        if _exists(bind, schema, table):
            # nosemgrep: avoid-sqlalchemy-text - identifiers are this module's constants, never input
            op.execute(sa.text(f'DROP TABLE "{schema}"."{table}" CASCADE'))


def upgrade() -> None:
    bind = op.get_bind()
    # nosemgrep: avoid-sqlalchemy-text - identifiers are this module's constants, never input
    op.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))

    in_public = [t for t in TABLES if _exists(bind, "public", t)]
    if not in_public:
        return  # nothing to move: already in place

    in_signals = [t for t in TABLES if _exists(bind, SCHEMA, t)]
    if in_signals:
        public_rows, signals_rows = _held(bind, "public"), _held(bind, SCHEMA)
        if public_rows and signals_rows:
            raise RuntimeError(
                "signals tables hold rows in both public and the signals schema "
                f"(public: {', '.join(public_rows)}; signals: {', '.join(signals_rows)}). "
                "These two sets of the same tables cannot be merged safely by a migration: decide "
                "which holds the real data, copy across anything the other needs, empty or drop "
                "the other, then run the migration again (issue 869)."
            )
        if signals_rows:
            _drop_set(bind, "public")  # the public set is empty
            return
        _drop_set(bind, SCHEMA)  # the signals set is empty; the public one moves in below

    _move(bind, "public", SCHEMA)


def downgrade() -> None:
    """Put the tables back in ``public``, where revision 001 expects them."""
    bind = op.get_bind()
    _move(bind, SCHEMA, "public")
