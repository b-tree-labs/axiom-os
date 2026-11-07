# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reply bind-back has to outlive the process that sent the message.

`ThreadStore` keeps every binding in two dicts, so a deploy, a restart or a
second gateway process loses every outstanding ``[axi-corr: …]`` token and the
human's reply binds to nothing. `DbThreadStore` keeps them in the ``threads``
table behind the same interface.
"""

from __future__ import annotations

import contextlib
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from axiom.extensions.builtins.notifications.db_models import Base, Thread
from axiom.extensions.builtins.notifications.gateway import threads_db
from axiom.extensions.builtins.notifications.gateway.threads import (
    ThreadStore,
    body_token,
    mint_correlation_id,
)
from axiom.extensions.builtins.notifications.gateway.threads_db import (
    DbThreadStore,
    default_thread_store,
    set_default_thread_store,
)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'notifications.db'}")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture(params=["memory", "db"])
def store(request, engine):
    return ThreadStore() if request.param == "memory" else DbThreadStore(engine=engine)


# --- one interface, both stores ------------------------------------------ #
class TestSharedInterface:
    def test_bind_then_lookup_by_token_and_correlation(self, store):
        corr = mint_correlation_id()
        store.bind(corr, actor="@rivet:local", vendor="slack", thread_ref="123.45")
        rec = store.by_token(body_token(corr))
        assert rec is not None
        assert (rec.correlation_id, rec.actor, rec.vendor, rec.thread_ref) == (
            corr, "@rivet:local", "slack", "123.45",
        )
        assert store.by_correlation(corr) == rec

    def test_unknown_token_is_none(self, store):
        assert store.by_token("deadbeef") is None
        assert store.by_correlation("corr-nothing") is None

    def test_a_token_resolves_once(self, store):
        corr = mint_correlation_id()
        store.bind(corr, actor="@rivet:local", vendor="slack")
        token = body_token(corr)
        assert store.claim(token)
        assert store.claim(token) is None  # the duplicate reply is a no-op

    def test_a_failed_action_reopens_the_token(self, store):
        corr = mint_correlation_id()
        store.bind(corr, actor="@rivet:local", vendor="slack")
        token = body_token(corr)
        claim = store.claim(token)
        assert claim
        assert store.release(token, claim) is True
        assert store.claim(token)

    def test_release_needs_the_claim(self, store):
        """The interface cannot express "reopen whatever is there"."""
        corr = mint_correlation_id()
        store.bind(corr, actor="@rivet:local", vendor="slack")
        with pytest.raises(TypeError):
            store.release(body_token(corr))

    def test_a_reply_that_lost_the_claim_cannot_reopen_the_winners(self, store):
        corr = mint_correlation_id()
        store.bind(corr, actor="@rivet:local", vendor="slack")
        token = body_token(corr)
        winner = store.claim(token)
        loser = store.claim(token)
        assert winner
        assert loser is None

        assert store.release(token, loser) is False
        assert store.release(token, "a-guess") is False

        assert store.claim(token) is None  # still held by the winner

    def test_an_old_claim_cannot_reopen_a_newer_one(self, store):
        corr = mint_correlation_id()
        store.bind(corr, actor="@rivet:local", vendor="slack")
        token = body_token(corr)
        first = store.claim(token)
        assert store.release(token, first) is True
        second = store.claim(token)
        assert second

        assert store.release(token, first) is False

        assert store.claim(token) is None

    def test_releasing_twice_reopens_once(self, store):
        corr = mint_correlation_id()
        store.bind(corr, actor="@rivet:local", vendor="slack")
        token = body_token(corr)
        claim = store.claim(token)
        assert store.release(token, claim) is True
        assert store.release(token, claim) is False

    def test_an_open_token_has_nothing_to_release(self, store):
        corr = mint_correlation_id()
        store.bind(corr, actor="@rivet:local", vendor="slack")
        assert store.release(body_token(corr), "a-guess") is False

    def test_an_unknown_token_has_nothing_to_release(self, store):
        assert store.release("deadbeef", "a-guess") is False

    def test_an_unknown_token_cannot_be_claimed(self, store):
        assert store.claim("deadbeef") is None


# --- what only the database gives ---------------------------------------- #
def test_a_binding_survives_a_restart(tmp_path):
    url = f"sqlite:///{tmp_path / 'restart.db'}"
    first = create_engine(url)
    Base.metadata.create_all(first)
    corr = mint_correlation_id()
    DbThreadStore(engine=first).bind(corr, actor="@rivet:local", vendor="teams")
    first.dispose()

    # A fresh engine and store on the same database: the process restarted.
    rec = DbThreadStore(engine=create_engine(url)).by_token(body_token(corr))
    assert rec is not None and rec.actor == "@rivet:local"


def test_a_binding_needs_no_delivery_receipt(engine):
    """An in-thread answer mints a correlation with no receipt behind it."""
    corr = mint_correlation_id()
    DbThreadStore(engine=engine).bind(corr, actor="@axi", vendor="teams")
    assert DbThreadStore(engine=engine).by_correlation(corr) is not None


def test_a_token_past_the_window_is_none(engine):
    store = DbThreadStore(engine=engine)
    corr = mint_correlation_id()
    store.bind(corr, actor="@rivet:local", vendor="slack")
    with engine.begin() as conn:
        conn.execute(
            update(Thread)
            .where(Thread.correlation_id == corr)
            .values(created_at=datetime.now(UTC) - timedelta(hours=25))
        )
    assert store.by_token(body_token(corr)) is None
    assert store.by_correlation(corr) is None
    assert store.claim(body_token(corr)) is None


# --- the process-wide default -------------------------------------------- #
#: `threads` as migration 0003 leaves it: no binding columns yet.
THREADS_AT_0003 = """
    CREATE TABLE threads (
        correlation_id VARCHAR PRIMARY KEY,
        receipt_id VARCHAR NOT NULL,
        channel VARCHAR NOT NULL,
        vendor_thread_id VARCHAR,
        cross_channel BOOLEAN NOT NULL,
        created_at DATETIME NOT NULL
    )
"""
SECRET_IN_THE_DRIVER_ERROR = "postgresql://axiom:hunter2@db.internal/axiom"


def _session_for_on(engine):
    """`session_for`, on a test engine."""

    @contextlib.contextmanager
    def _session_for(_name):
        with Session(engine) as session:
            yield session

    return _session_for


@pytest.fixture
def default_store_reset():
    set_default_thread_store(None)
    yield
    set_default_thread_store(None)


@pytest.fixture
def empty_database(tmp_path):
    """A database nothing has migrated."""
    return create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")


@pytest.fixture
def database_at_0003(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'at_0003.db'}")
    with eng.begin() as conn:
        conn.execute(text(THREADS_AT_0003))
    return eng


def _migrate_threads(engine):
    """Bring `threads` to the shape 0004 leaves it in. No rows to keep here."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE threads"))
    Base.metadata.create_all(engine)


@pytest.mark.usefixtures("default_store_reset")
class TestDefaultInDev:
    @pytest.fixture(autouse=True)
    def _dev(self, monkeypatch):
        monkeypatch.setenv("AXIOM_MODE", "dev")

    def test_no_database_falls_back_to_memory_and_says_so(self, monkeypatch, caplog):
        def _unreachable(_name):
            raise RuntimeError("no database here")

        monkeypatch.setattr("axiom.infra.db.session_for", _unreachable)
        with caplog.at_level(logging.WARNING, logger=threads_db.__name__):
            store = default_thread_store()
        assert isinstance(store, ThreadStore)
        assert "IN-MEMORY" in caplog.text

    def test_a_fresh_database_falls_back_to_memory(self, monkeypatch, empty_database, caplog):
        monkeypatch.setattr("axiom.infra.db.session_for", _session_for_on(empty_database))
        with caplog.at_level(logging.WARNING, logger=threads_db.__name__):
            store = default_thread_store()
        assert isinstance(store, ThreadStore)
        assert "IN-MEMORY" in caplog.text

    def test_a_database_at_0003_falls_back_to_memory(self, monkeypatch, database_at_0003, caplog):
        monkeypatch.setattr("axiom.infra.db.session_for", _session_for_on(database_at_0003))
        with caplog.at_level(logging.WARNING, logger=threads_db.__name__):
            store = default_thread_store()
        assert isinstance(store, ThreadStore)
        assert "IN-MEMORY" in caplog.text

    def test_a_migrated_database_gives_the_durable_store(self, monkeypatch, engine, caplog):
        monkeypatch.setattr("axiom.infra.db.session_for", _session_for_on(engine))
        with caplog.at_level(logging.WARNING, logger=threads_db.__name__):
            assert isinstance(default_thread_store(), DbThreadStore)
        assert caplog.text == ""


@pytest.mark.usefixtures("default_store_reset")
@pytest.mark.parametrize("mode", ["staging", "production"])
class TestDefaultOutsideDev:
    """Memory would look like it works until the first restart, so it is never
    chosen here. The durable store is kept, each use fails loudly, and the store
    starts working once the migration lands."""

    @pytest.fixture(autouse=True)
    def _mode(self, monkeypatch, mode):
        monkeypatch.setenv("AXIOM_MODE", mode)

    def test_a_fresh_database_keeps_the_durable_store_and_logs_an_error(
        self, monkeypatch, empty_database, caplog
    ):
        monkeypatch.setattr("axiom.infra.db.session_for", _session_for_on(empty_database))
        with caplog.at_level(logging.WARNING, logger=threads_db.__name__):
            store = default_thread_store()
        assert isinstance(store, DbThreadStore)
        assert [r.levelname for r in caplog.records] == ["ERROR"]
        assert "IN-MEMORY" not in caplog.text
        with pytest.raises(SQLAlchemyError):
            store.bind(mint_correlation_id(), actor="@rivet:local", vendor="slack")

    def test_a_database_at_0003_recovers_once_migrated_without_a_restart(
        self, monkeypatch, database_at_0003, caplog
    ):
        monkeypatch.setattr("axiom.infra.db.session_for", _session_for_on(database_at_0003))
        with caplog.at_level(logging.WARNING, logger=threads_db.__name__):
            store = default_thread_store()
        assert isinstance(store, DbThreadStore)
        assert [r.levelname for r in caplog.records] == ["ERROR"]
        corr = mint_correlation_id()
        with pytest.raises(SQLAlchemyError):
            store.bind(corr, actor="@rivet:local", vendor="slack")

        _migrate_threads(database_at_0003)

        store.bind(corr, actor="@rivet:local", vendor="slack")
        assert store.by_token(body_token(corr)) is not None

    def test_the_log_names_the_failure_not_its_text(self, monkeypatch, caplog):
        def _unreachable(_name):
            raise RuntimeError(f"could not connect to {SECRET_IN_THE_DRIVER_ERROR}")

        monkeypatch.setattr("axiom.infra.db.session_for", _unreachable)
        with caplog.at_level(logging.WARNING, logger=threads_db.__name__):
            default_thread_store()
        assert "RuntimeError" in caplog.text
        assert "hunter2" not in caplog.text


class TestTheProbeChangesNothing:
    def test_it_creates_no_table_in_a_fresh_database(self, monkeypatch, empty_database):
        """`migrate` owns the schema: 0001's plain create_table has to find it empty."""
        monkeypatch.setattr("axiom.infra.db.session_for", _session_for_on(empty_database))
        assert threads_db.DbThreadStore().schema_problem()
        assert inspect(empty_database).get_table_names() == []

    def test_it_alters_nothing_at_0003(self, monkeypatch, database_at_0003):
        monkeypatch.setattr("axiom.infra.db.session_for", _session_for_on(database_at_0003))
        before = [c["name"] for c in inspect(database_at_0003).get_columns("threads")]
        assert threads_db.DbThreadStore().schema_problem()
        assert [c["name"] for c in inspect(database_at_0003).get_columns("threads")] == before


# --- the real migrations on a real Postgres ------------------------------- #
def _upgrade(revision: str) -> None:
    from axiom.extensions.builtins import notifications

    config = Config()
    config.set_main_option("script_location", str(Path(notifications.__file__).parent / "migrations"))
    command.upgrade(config, revision)


@pytest.fixture
def postgres(fresh_postgres, monkeypatch, default_store_reset):
    """One throwaway database, at the unsuffixed schema the migrations name."""
    monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)
    monkeypatch.setenv("AXIOM_MODE", "production")
    yield fresh_postgres


def _tables(schema: str) -> set[str]:
    from axiom.infra.db import get_engine

    return set(inspect(get_engine()).get_table_names(schema=schema))


class TestOnPostgres:
    def test_asking_for_the_store_before_migrate_leaves_migrate_its_tables(self, postgres):
        """The old bootstrap wrote `public`, where `session_for` keeps finding those
        rows only until `migrate` builds the real tables in front of them."""
        store = default_thread_store()
        assert isinstance(store, DbThreadStore)
        assert _tables("public") == set()

        _upgrade("head")

        assert "threads" in _tables("notifications")
        assert _tables("public") == set()
        corr = mint_correlation_id()
        store.bind(corr, actor="@rivet:local", vendor="slack")
        assert store.by_token(body_token(corr)) is not None

    def test_a_node_at_0003_is_reported_then_recovers_on_migrate(self, postgres, caplog):
        _upgrade("0003")
        with caplog.at_level(logging.ERROR, logger=threads_db.__name__):
            store = default_thread_store()
        assert "threads" in caplog.text
        assert _tables("public") == set()
        corr = mint_correlation_id()
        with pytest.raises(SQLAlchemyError):
            store.bind(corr, actor="@rivet:local", vendor="slack")

        _upgrade("head")

        assert store.schema_problem() is None
        store.bind(corr, actor="@rivet:local", vendor="slack")
        assert store.by_correlation(corr) is not None

    def test_exactly_one_of_many_replies_claims_a_token(self, postgres):
        _upgrade("head")
        store = DbThreadStore()
        corr = mint_correlation_id()
        store.bind(corr, actor="@rivet:local", vendor="slack")
        token = body_token(corr)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _i: store.claim(token), range(8)))

        assert len([r for r in results if r]) == 1
