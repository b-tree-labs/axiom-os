# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A migration op with no ``schema=`` writes to ``public``, silently.

ADR-052 says an extension owns its own schema and never writes to ``public``.
Nothing enforced it for *migrations*, and the result was an extension built
half in one schema and half in another for four revisions without anyone
noticing — because no node had ever successfully run them.

    schedule/0001  schema="schedule" on every op   -> schedule.schedule_definition
    schedule/0002  no schema= anywhere             -> public.schedule_time_slot
    schedule/0003  ALTER TABLE schedule_definition -> resolved through
                                                      search_path ("$user",
                                                      public) -> not found

`session_for` sets ``search_path`` to the extension's schema; ``engine_for``,
which every Alembic ``env.py`` uses, does not. So the two doors to the same
database disagree about where an unqualified name lives, and a migration is
always behind the door that disagrees.

Fixing the four broken revisions closes one instance. This closes the class:
it is a static check, so it runs everywhere without a database, and it names
the file and the op rather than failing somewhere downstream in a way that
looks unrelated.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from axiom.infra.db import extensions_with_migrations

#: Alembic operations that name a table and therefore need a schema. Ops that
#: take no table (``execute``, ``bulk_insert``) are not listed: they carry
#: their own SQL and are the author's problem, not this guard's.
SCHEMA_BEARING_OPS = frozenset({
    "create_table",
    "drop_table",
    "add_column",
    "drop_column",
    "alter_column",
    "create_index",
    "drop_index",
    "create_unique_constraint",
    "create_foreign_key",
    "create_check_constraint",
    "drop_constraint",
    "rename_table",
})


def _unqualified_ops(path: Path) -> list[tuple[int, str]]:
    """``(lineno, op)`` for every schema-bearing op with no ``schema=``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in SCHEMA_BEARING_OPS:
            continue
        # only `op.<thing>(...)`, not some unrelated object with the same method
        if not (isinstance(func.value, ast.Name) and func.value.id == "op"):
            continue
        if any(kw.arg == "schema" for kw in node.keywords):
            continue
        found.append((node.lineno, func.attr))
    return found


#: Revisions that violate this and CANNOT simply be corrected, because their
#: tables already exist in ``public`` on live installs with data in them.
#: Qualifying the migration would make a fresh database right and leave every
#: existing one with two copies of the same table in two schemas.
#:
#: xfail(strict=True) rather than an exemption: the trunk stays green, the
#: problem is recorded where it happens, and the marker fails as XPASS the
#: moment someone does the data migration — so it cannot be quietly left behind.
#:
#: The decision each needs: move the existing ``public`` tables into the
#: extension's schema (with the node's data), then qualify the revision.
KNOWN_UNQUALIFIED = {
    "signals/20260224_0001_001_initial_schema": (
        "signals builds its four tables and nine indexes in public on every "
        "live install; it is also the extension whose alembic_version lands "
        "in public (axiom#869). Needs a data migration, not a code edit."
    ),
    "expman/20260602_expman_irr_rename": (
        "a rename against tables that already exist in public on live installs."
    ),
}


def _revision_files() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for name, directory in extensions_with_migrations():
        for path in sorted(Path(directory).glob("versions/*.py")):
            out.append((name, path))
    return out


def _params():
    params = []
    for name, path in _revision_files():
        key = f"{name}/{path.stem}"
        reason = KNOWN_UNQUALIFIED.get(key)
        marks = (
            [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
        )
        params.append(pytest.param(name, path, id=key, marks=marks))
    return params


def test_there_are_migrations_to_check():
    """A guard over an empty set passes forever and proves nothing."""
    files = _revision_files()
    assert len(files) >= 10, f"only found {len(files)} revision files — discovery is broken"


@pytest.mark.parametrize("extension,path", _params())
def test_every_migration_op_names_its_schema(extension: str, path: Path):
    """An op without ``schema=`` lands in ``public``, whatever the extension owns."""
    offences = _unqualified_ops(path)
    assert not offences, (
        f"{extension}/{path.name}: "
        + "; ".join(f"line {line}: op.{op}() has no schema=" for line, op in offences)
        + f" — unqualified ops resolve through search_path, which does not include "
        f"'{extension}', so they write to public (ADR-052)"
    )


def test_the_known_violations_are_named_not_a_blanket_exemption():
    """Each entry carries the decision it is waiting on, not just a pass.

    A skip list without reasons becomes a place to hide things.
    """
    assert KNOWN_UNQUALIFIED, "if this empties, delete the mechanism"
    for key, reason in KNOWN_UNQUALIFIED.items():
        assert "/" in key, f"{key} should be <extension>/<revision stem>"
        assert len(reason) > 60, f"{key}: say what decision it is waiting on"


def test_the_check_can_actually_fail(tmp_path):
    """Negative control: a guard nobody has watched fail is not yet evidence."""
    bad = tmp_path / "0001_bad.py"
    bad.write_text(
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "def upgrade():\n"
        "    op.create_table('t', sa.Column('id', sa.String()))\n"
        "    op.add_column('t', sa.Column('x', sa.String()), schema='ok')\n"
    )
    offences = _unqualified_ops(bad)
    assert offences == [(4, "create_table")], offences

    good = tmp_path / "0002_good.py"
    good.write_text(
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "def upgrade():\n"
        "    op.create_table('t', sa.Column('id', sa.String()), schema='mine')\n"
    )
    assert _unqualified_ops(good) == []


def test_it_ignores_methods_that_merely_share_a_name(tmp_path):
    """`something_else.create_index(...)` is not an Alembic op."""
    f = tmp_path / "0003.py"
    f.write_text(
        "def upgrade():\n"
        "    store.create_index('t')\n"
        "    op.create_index('t', schema='mine')\n"
    )
    assert _unqualified_ops(f) == []
