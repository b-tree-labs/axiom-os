# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The default database URL must name its driver.

``DEFAULT_DB_URL`` was a bare ``postgresql://``, which leaves the driver
to whatever SQLAlchemy happens to default to. That default is not a
constant: SQLAlchemy 2.0 resolves it to psycopg2, and 2.1 resolves it to
psycopg (v3). ``pyproject`` declares ``sqlalchemy>=2.0`` with no ceiling
and ``psycopg2-binary`` as the only driver, so CI — which installs fresh
every run — moved to 2.1, the default flipped underneath us, and three
unrelated tests started failing with ``ModuleNotFoundError: No module
named 'psycopg'`` while every developer machine, pinned at 2.0 from an
older install, stayed green.

Nothing in the repository changed to cause that. It is the shape of bug
that arrives on a Tuesday.

The check below is deliberately about the URL and not about which driver
SQLAlchemy would pick, because the whole failure was letting somebody
else pick.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from axiom.infra.db import DEFAULT_DB_URL


def test_the_default_url_names_its_driver_explicitly():
    """A scheme of ``postgresql`` alone means "whatever the library
    prefers today". A scheme of ``postgresql+psycopg2`` means what it
    says, in every version."""
    url = make_url(DEFAULT_DB_URL)
    assert "+" in url.drivername, (
        f"DEFAULT_DB_URL is {DEFAULT_DB_URL!r}: it leaves the driver to "
        "SQLAlchemy's default, which changed between 2.0 and 2.1. Name it."
    )


def test_the_driver_it_names_is_the_one_we_depend_on():
    assert make_url(DEFAULT_DB_URL).drivername == "postgresql+psycopg2", (
        "pyproject declares psycopg2-binary and no other driver"
    )


def test_an_engine_can_be_built_from_it():
    """No connection is made — this only proves the driver imports."""
    engine = create_engine(DEFAULT_DB_URL)
    assert engine.dialect.driver == "psycopg2"


def test_no_test_builds_an_engine_from_a_driverless_postgres_url():
    """The same landmine, one layer up: a TEST that constructs an engine
    from a bare ``postgresql://`` fails at fixture-build time under a
    SQLAlchemy whose default driver we do not ship, and the failure names
    a module nobody wrote — which is how an hour goes."""
    import re

    root = Path(__file__).resolve()
    while not (root / "pyproject.toml").is_file():
        root = root.parent
    pattern = re.compile(r"create_engine\(\s*f?[\"']postgres(?:ql)?://")
    offenders = []
    for path in list((root / "tests").rglob("*.py")) + list((root / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(text):
            offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        "these build an engine from a driver-less postgres URL, so which "
        f"driver they need depends on the SQLAlchemy version: {offenders}"
    )


# --- The other half of the same landmine (2026-09-25) ---
#
# Naming DEFAULT_DB_URL fixed the DEFAULT, and only the default. A URL that
# arrives from the ENVIRONMENT never passes through that constant, and CI
# supplies exactly that: ci.yml set AXIOM_DB_URL to a bare postgresql://.
# So main kept failing after the fix above landed, in the one job whose
# install brings no incidental psycopg3 — Migration Tests died on
# `ensure_pgvector_extension`, CREATE EXTENSION vector never ran, and
# Integration Tests then failed on every vector column with
# `type "vector" does not exist`. Two red jobs, six consecutive red mains,
# one cause.
#
# The regex guard above could not see it twice over: the offending string
# was in YAML, and the call site reads `create_engine(get_db_url())`, which
# is not a literal URL. Both blind spots are closed below.


def test_the_engine_is_built_with_a_named_driver_even_from_a_bare_env_url(monkeypatch):
    """Whatever the environment spells, the Engine names its driver."""
    from axiom.infra import db as db_mod

    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u@127.0.0.1:1/db")
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_engine_url", None)
    monkeypatch.setattr(db_mod, "_session_factory", None)
    engine = db_mod.get_engine()  # no connection is made
    assert engine.dialect.driver == "psycopg2"


def test_the_shared_dsn_is_left_libpq_parseable(monkeypatch):
    """The fix belongs at Engine construction and NOT in the shared DSN.

    ``platform_db_url`` is read by raw psycopg2 as well as by SQLAlchemy, and
    ``psycopg2.connect`` rejects a driver-qualified URL outright:

        ProgrammingError: invalid dsn: missing "=" after
        "postgresql+psycopg2://..." in connection info string

    An earlier attempt at this fix normalised the resolver instead, which
    would have broken `data.ensure-schema`, `data.backup` and every other
    DDL-over-psycopg2 caller. `test_the_two_doors_to_the_database_agree`
    caught it. This test is here so the same shortcut is not retried.
    """
    from axiom.infra.db import platform_db_url

    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u@h/db")
    assert platform_db_url() == "postgresql://u@h/db", (
        "platform_db_url must return the DSN as configured; name the driver "
        "where an Engine is built, not here"
    )


def test_normalising_leaves_a_deliberate_driver_choice_alone():
    """Somebody who asks for psycopg v3 on purpose gets psycopg v3. The bug
    was never "psycopg3 is wrong", it was "nobody said"."""
    from axiom.infra.db import require_named_driver

    assert require_named_driver("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"
    assert require_named_driver("sqlite:///x.db") == "sqlite:///x.db"
    assert require_named_driver("") == ""
    # postgres:// is rewritten too — SQLAlchemy has rejected that spelling
    # outright since 1.4, so passing it through only delays the error.
    assert require_named_driver("postgres://u@h/db") == "postgresql+psycopg2://u@h/db"


def test_the_signals_migration_path_names_its_driver(monkeypatch):
    """This is the exact call that reddened main."""
    from axiom.extensions.builtins.signals.migrations import get_db_url

    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u@h/db")
    assert make_url(get_db_url()).drivername == "postgresql+psycopg2"
    monkeypatch.delenv("AXIOM_DB_URL")
    assert make_url(get_db_url()).drivername == "postgresql+psycopg2"


def test_no_source_file_declares_a_driverless_postgres_default():
    """The same landmine written as a *default* rather than as a literal
    engine URL: ``os.environ.get("AXIOM_DB_URL", "postgresql://...")``, or a
    module-level ``DEFAULT_DB_URL``. That pattern had been copied into five
    places, one of which is what CI actually ran.

    Deliberately narrow. A bare ``postgresql://`` is also written all over
    the tree as a *scheme prefix* — ``url.startswith("postgresql://")`` in
    store_factory, the provider modules, the CLI help text — and those are
    correct: they classify a URL rather than supply one. A guard that
    flagged them would have twenty false positives and would be switched
    off within a week, which is worse than no guard.
    """
    import re

    root = Path(__file__).resolve()
    while not (root / "pyproject.toml").is_file():
        root = root.parent
    supplies_a_default = re.compile(
        r"(?:environ\.get|getenv)\([^)]*,\s*[\"']postgres(?:ql)?://"
        r"|^\s*[A-Z_]*(?:DB_URL|DSN|DATABASE_URL)[A-Z_]*\s*=\s*[\"']postgres(?:ql)?://"
    )
    offenders = []
    for path in (root / "src").rglob("*.py"):
        if "/tests/" in str(path):
            continue  # a test may legitimately FEED a bare URL as input
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if supplies_a_default.search(line):
                offenders.append(f"{path.relative_to(root)}:{n}")
    assert not offenders, (
        "these supply a postgres URL without naming its driver, so which "
        "driver they need depends on the SQLAlchemy version pip resolved "
        f"that morning: {offenders}"
    )
