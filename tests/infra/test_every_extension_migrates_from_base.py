# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every extension that ships migrations migrates from an empty database.

The "Migration Tests" job used to import one extension by name, run only on
main, and read the version from the place that extension wrongly wrote it, so
two of eight extensions could not migrate from base while it stayed green
(issue 869). This drives ``provision_extension`` over whatever
``extensions_with_migrations()`` discovers, so an extension added tomorrow is
covered without anyone remembering to list it.

Per extension, on a throwaway database:

* one head (two heads is how an ``upgrade head`` becomes ambiguous; attest hit it)
* base to head leaves the version in the extension's OWN schema (ADR-052)
* nothing is written to ``public``

Downgrade is not exercised: migrations here are expand-then-contract and some
are deliberately irreversible (attest 0012 refuses to downgrade).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from axiom.infra.db import (
    current_revision,
    extensions_with_migrations,
    normalize_extension_name,
    provision_extension,
)

EXTENSIONS = extensions_with_migrations()
IDS = [name for name, _ in EXTENSIONS]

#: Known violations of ADR-052, named so the gap is visible rather than green by
#: omission. ``strict`` makes the fix flip this red until the entry is deleted.
KNOWN_BROKEN = {
    "signals": (
        "signals owns no schema: env.py stamps alembic_version and its five tables "
        "in public (ADR-052). Moving it needs a data migration for installs that "
        "already hold those tables; tracked in issue 869."
    ),
}


def _case(name, directory):
    marks = []
    if name in KNOWN_BROKEN:
        marks.append(pytest.mark.xfail(reason=KNOWN_BROKEN[name], strict=True))
    return pytest.param(name, directory, id=name, marks=marks)


CASES = [_case(name, directory) for name, directory in EXTENSIONS]


def test_discovery_finds_the_extensions_this_job_exists_to_cover():
    # The floor, not the exact set: a new extension must not fail this, and a
    # discovery that quietly returns nothing must.
    assert {"attest", "authz", "chat", "notifications", "schedule", "signals", "vault", "webapp"} <= set(IDS)


@pytest.mark.parametrize(("name", "directory"), EXTENSIONS, ids=IDS)
def test_one_head(name, directory):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(directory))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"{name} has {len(heads)} migration heads {heads}; merge them"


def _public_tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            )
            return {row[0] for row in rows}
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(("name", "directory"), CASES)
def test_migrates_from_base(name, directory, fresh_postgres, monkeypatch):
    from alembic.script import ScriptDirectory

    # The migrations under test create their tables in the production schema
    # name. Under xdist the conftest appends a per-worker suffix (``chat_gw1``)
    # so persisted-state tests stay apart; here the database is already this
    # test's own (``fresh_postgres``), so the suffix only makes the provisioned
    # schema disagree with the one the migration writes to. Run as production does.
    monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)

    schema = normalize_extension_name(name)
    before_public = _public_tables(fresh_postgres)

    result = provision_extension(name)
    if result.skipped:
        pytest.skip(result.skipped)
    assert result.ok, f"{name}: {result.error}"

    head = ScriptDirectory(str(directory)).get_current_head()
    assert current_revision(name) == head, f"{name} not stamped in schema {schema!r}"
    assert _public_tables(fresh_postgres) == before_public, (
        f"{name} wrote to public (ADR-052): {sorted(_public_tables(fresh_postgres) - before_public)}"
    )
