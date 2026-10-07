# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

# Root conftest — makes shared fixtures available to ALL test directories,
# including colocated extension tests in src/axiom/extensions/builtins/.
#
# Fixtures are defined in tests/conftest.py and re-exported here so that
# pytest discovers them regardless of which testpath a test lives under.

import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from tests.conftest import *  # noqa: F401,F403,E402

_ROOT = Path(__file__).resolve().parent

#: The same roots ``[tool.pytest.ini_options] pythonpath`` names. Kept in step
#: with it by ``tests/test_worktree_isolation.py``, which reads the toml rather
#: than trusting this list.
_SOURCE_ROOTS = (_ROOT / "src", _ROOT / "packages" / "axiom-tests" / "src")


def pytest_configure(config):
    """Make child processes import THIS worktree, not whichever one owns the install.

    ``pythonpath`` in ``pyproject.toml`` puts these roots on ``sys.path`` for
    the test session, so in-process tests correctly exercise the checkout they
    live in. A subprocess inherits ``os.environ`` and nothing else, so it
    resolves ``axiom`` through site-packages — which points at whichever
    worktree last ran ``pip install -e``. Every one of the 100-plus test files
    that spawns ``sys.executable`` was therefore testing that checkout instead
    of its own.

    That is a check that cannot fail, and worse, one that can fail for reasons
    with nothing to do with the code under test: a session editing the anchor
    worktree red-lights every other worktree's pre-push gate, which is how this
    was found.

    Setting ``PYTHONPATH`` here rather than in each test fixes all of them at
    once and needs no per-test discipline. It is set in ``os.environ`` so a
    child gets it whether or not the spawning test remembered to pass ``env``.
    """
    roots = [str(p) for p in _SOURCE_ROOTS if p.is_dir()]
    if not roots:  # pragma: no cover — a source tree that is not there
        return
    existing = os.environ.get("PYTHONPATH", "")
    parts = roots + [p for p in existing.split(os.pathsep) if p and p not in roots]
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)

    # The in-process path matters too when pytest is invoked in a way that does
    # not apply the ini setting (``python -m pytest`` from another directory,
    # some IDE runners). Idempotent, and ordered so the worktree wins.
    for root in reversed(roots):
        if root not in sys.path:
            sys.path.insert(0, root)

    # A test run must not write to the operator's capability series. The CLI
    # entry point and both MCP servers arm it at startup, and its default state
    # dir is the real ``~/.axi`` — so a suite that exercises those paths
    # appended test invocations to real usage. That is not just untidy: usage
    # is what removes a capability from the discovery block, so a test run
    # could hide a capability the person had never actually used. Observed
    # doing exactly that (four ``config.validate`` "uses", zero of them real).
    #
    # Set in ``os.environ`` so spawned subprocesses inherit it too. Tests that
    # exercise the series turn it back on with an explicit state dir.
    os.environ["AXIOM_CAPABILITY_TELEMETRY"] = "0"

    # And point the state dir somewhere disposable. Writes are off above, but
    # READS are not — the discovery block is deliberately independent of the
    # telemetry opt-out — so without this a test that builds an MCP server
    # renders a block from the developer's own ``~/.axi`` and its output varies
    # by machine and by what that person happened to run this week.
    os.environ.setdefault(
        "AXIOM_STATE_DIR", tempfile.mkdtemp(prefix="axiom-test-state-")
    )

    # And keep foreign-credential VALUES out of the developer's real keychain.
    #
    # `ForeignCredentialStore(tmp_path)` isolates only the metadata index. The
    # values go to `open_default_value_store`, which uses the darwin keychain
    # whenever one is available — so a test handed a `tmp_path` still wrote
    # into the live `axiom-secrets` namespace, beside this machine's actual
    # credentials.
    #
    # It showed up as a failure nobody could reproduce: green on its own, red
    # in the full suite, because parallel workers raced to delete-and-recreate
    # the SAME keychain item and the loser got "already exists". The entry then
    # outlived the run, so every later run started poisoned.
    #
    # `file` is the backend the dev/CI path already degrades to, and several
    # suites set it per-test. Setting it for the session means a test cannot
    # reach the real keychain by forgetting to. `setdefault`, so anyone
    # deliberately exercising a real backend still can; the keychain provider's
    # own tests are unaffected because they open the provider directly under a
    # throwaway `axiom-test-*` service.
    os.environ.setdefault("AXIOM_FOREIGN_SECRETS_BACKEND", "file")

    # And pin the terminal width. `cli_format.table` asks the terminal when no
    # width is given, which is right for a person and wrong for an assertion:
    # without this, the same table test passes in an 80-column window and fails
    # in a 120-column one, on the developer's machine only. Same class as
    # FORCE_COLOR, which reddened five tests CI never saw.
    os.environ.setdefault("COLUMNS", "80")

@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """No test's answer may depend on the developer's shell.

    `ProcessEnvProbe` reports credential material held in THIS process's
    environment — which, under pytest, is whatever the person running the
    suite happens to export. A real `ANTHROPIC_API_KEY` in a maintainer's
    shell silently added a finding to every discovery test, so two vault
    sweep tests counted one credential on CI and two on a laptop.

    Stripped for every test rather than in the handful that noticed: a test
    whose subject is the environment passes or fails for reasons that are
    not in the repository, and the next one written will not know to guard.
    A test that WANTS a credential in the environment sets one itself.
    """
    from axiom.extensions.builtins.secrets.discovery.matchers import match_text

    for key, value in list(os.environ.items()):
        if value and match_text(value):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _memory_layer_on_an_in_memory_seam():
    """The memory layer's runtime store is Postgres (ADR-174); tests run on a seam.

    Every test gets a fresh in-memory SQLite session provider for the ledger and
    concept graph, and a SQLite factory for the recall corpus, so nothing needs a
    server and nothing leaks between tests. SQLite is allowed here and nowhere
    else. A test that wants the real database binds it itself (see
    ``tests/memory/test_pg_ledger.py``); ``reset_provider`` in its teardown also
    clears this one.
    """
    import contextlib

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from axiom.memory import pg_store, stores
    from axiom.memory.pg_models import Base

    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)

    @contextlib.contextmanager
    def provider():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    def sqlite_recall(base):
        from axiom.rag.sqlite_store import SQLiteRAGStore

        return SQLiteRAGStore(f"sqlite:///{base / 'recall.db'}")

    pg_store.set_provider(provider)
    stores.set_recall_factory(sqlite_recall)
    yield
    pg_store.reset_provider()
    stores.set_recall_factory(None)
    engine.dispose()


def _postgres_candidate_url() -> str:
    from axiom.infra.db import DEFAULT_DB_URL

    candidates = [os.environ.get(v, "") for v in ("AXIOM_PG_TEST_URL", "AXIOM_DB_URL")]
    url = next((c for c in candidates if c.startswith("postgresql")), DEFAULT_DB_URL)
    return url.replace("postgresql://", "postgresql+psycopg2://", 1)


@pytest.fixture(scope="session")
def _scratch_postgres_url():
    """A throwaway database on the reachable server, dropped when the session ends.

    Tests that need a real Postgres never touch the server's own database: a developer's
    local stack has real data in it, and a test run must not add rows or schemas there. The
    server is whichever ``AXIOM_PG_TEST_URL`` / ``AXIOM_DB_URL`` / default DSN names; each
    session (and each xdist worker) creates its own database, so runs cannot see each other.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    base = make_url(_postgres_candidate_url())
    name = f"axiom_test_{os.getpid()}_{uuid4().hex[:8]}"
    try:
        admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001
        message = f"Postgres not reachable or cannot create a test database: {exc}"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    scratch = base.set(database=name)
    try:
        own = create_engine(scratch, isolation_level="AUTOCOMMIT")
        with own.connect() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        own.dispose()
    except Exception:  # noqa: BLE001
        pass  # tests that need pgvector will say so; the memory ledger does not
    admin.dispose()  # do not hold a connection across a long session: the server drops idle ones
    yield scratch.render_as_string(hide_password=False)
    closer = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with closer.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        closer.dispose()


@pytest.fixture
def real_postgres(monkeypatch, _scratch_postgres_url):
    """A real Postgres for tests that cross a process boundary or prove the migration.

    A subprocess (an MCP server over stdio, a CLI) cannot see the in-process seam, so it
    needs a database. This binds the session's throwaway database (see
    ``_scratch_postgres_url``), so nothing else on the server is touched. Locally the test
    skips when no server is reachable; **on CI it fails instead**, because a skipped test
    cannot fail and this is the coverage that proves the production store works. Binding it
    unbinds the default seam and the SQLite recall factory for this test.
    """
    from axiom.memory import pg_store, stores

    monkeypatch.setenv("AXIOM_DB_URL", _scratch_postgres_url)
    pg_store.reset_provider()
    stores.set_recall_factory(None)
    yield _scratch_postgres_url
