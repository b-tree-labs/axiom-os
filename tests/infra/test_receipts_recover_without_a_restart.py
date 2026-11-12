# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A node whose database comes back recovers without a restart.

Every surface caches its ``DecideContext`` for the life of the process, and
``decide()`` refuses a call it cannot write a receipt for. So a once-only probe
outside dev would turn a database that was down at process start into a node
that refuses every call until someone restarts it, however long ago the
database recovered. Outside dev the factory is always wired and each failed
write denies that one call. The once-only probe is a dev convenience.

The "database" here is real receipt rows in SQLite behind a switch, so a pass
means the row exists, not that a fake was called.
"""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.authz.db_models import Base
from axiom.extensions.builtins.authz.db_models import Verdict as VerdictRow
from axiom.extensions.builtins.authz.decide import RECEIPT_WRITE_FAILED, UNRECORDED_RECEIPT
from axiom.governance import Decision
from axiom.governance import simple as governance_simple
from axiom.governance.classification import Classification
from axiom.governance.simple import AuthorizationDenied, setup_extension
from axiom.infra import authority, db, site_authority


class _SwitchableDb:
    """``session_for`` and ``get_engine`` over SQLite, with a power switch."""

    def __init__(self) -> None:
        self.up = False
        self.opens = 0
        self.engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(self.engine)
        self._sessions = sessionmaker(bind=self.engine, future=True, expire_on_commit=False)

    def get_engine(self):
        if not self.up:
            raise ConnectionError("database is down")
        return self.engine

    @contextlib.contextmanager
    def session_for(self, extension_name: str):
        assert extension_name == "authz"
        self.opens += 1
        if not self.up:
            raise ConnectionError("database is down")
        session = self._sessions()
        try:
            yield session
        finally:
            session.close()

    def drop_receipt_table(self) -> None:
        VerdictRow.__table__.drop(self.engine)

    def receipts(self) -> list[VerdictRow]:
        with self._sessions() as session:
            return list(session.scalars(select(VerdictRow)))


@pytest.fixture
def database(monkeypatch, tmp_path):
    fake = _SwitchableDb()
    monkeypatch.setattr(db, "session_for", fake.session_for)
    monkeypatch.setattr(db, "get_engine", fake.get_engine)
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "auto")
    monkeypatch.setenv("AXIOM_AUTHORITY_SITE_RULES", str(tmp_path / "absent.toml"))
    authority._reset_default_tool_context()
    site_authority._reset_default_site_context()
    yield fake
    authority._reset_default_tool_context()
    site_authority._reset_default_site_context()


def _tool_decision():
    return authority.decide_tool_call(
        authority.build_tool_envelope("read_file", {}, "@alice:axiom")
    )


def _site_decision():
    return site_authority.decide_site_read(
        site_authority.build_site_read_envelope("site-a", "@alice:axiom")
    )


@pytest.mark.parametrize("mode", ["staging", "production"])
class TestOutsideDev:
    def test_a_tool_call_recovers_when_the_database_does(self, database, monkeypatch, mode):
        monkeypatch.setenv("AXIOM_MODE", mode)

        denied = _tool_decision()
        assert denied.decision is Decision.DENY
        assert denied.reason == RECEIPT_WRITE_FAILED
        assert denied.receipt_fragment_id == UNRECORDED_RECEIPT
        assert database.receipts() == []

        database.up = True

        permitted = _tool_decision()
        assert permitted.decision is Decision.PERMIT
        assert [row.id for row in database.receipts()] == [permitted.receipt_fragment_id]

    def test_a_site_read_recovers_when_the_database_does(self, database, monkeypatch, mode):
        monkeypatch.setenv("AXIOM_MODE", mode)

        denied = _site_decision()
        assert denied.decision is Decision.DENY
        assert denied.reason == RECEIPT_WRITE_FAILED

        database.up = True

        permitted = _site_decision()
        assert permitted.decision is Decision.PERMIT
        assert [row.id for row in database.receipts()] == [permitted.receipt_fragment_id]

    def test_the_database_going_away_later_denies_that_call_only(self, database, monkeypatch, mode):
        monkeypatch.setenv("AXIOM_MODE", mode)
        database.up = True
        assert _tool_decision().decision is Decision.PERMIT

        database.up = False
        assert _tool_decision().reason == RECEIPT_WRITE_FAILED

        database.up = True
        assert _tool_decision().decision is Decision.PERMIT

    def test_each_call_tries_the_database_rather_than_a_remembered_answer(
        self, database, monkeypatch, mode
    ):
        monkeypatch.setenv("AXIOM_MODE", mode)
        _tool_decision()
        _tool_decision()
        assert database.opens == 2

    def test_receipts_off_still_denies(self, database, monkeypatch, mode):
        monkeypatch.setenv("AXIOM_MODE", mode)
        monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")
        database.up = True

        verdict = _tool_decision()

        assert verdict.decision is Decision.DENY
        assert verdict.reason == RECEIPT_WRITE_FAILED
        assert database.receipts() == []


class TestSetupExtensionUsesTheRuntimeModeNotTheFlag:
    def test_dev_mode_flag_in_staging_is_not_stuck_on_a_dead_probe(self, database, monkeypatch):
        """``dev_mode=True`` is a warning in staging, not a refusal, so a once-only
        probe keyed on the flag would pin a staging node to DENY for good."""
        monkeypatch.setenv("AXIOM_MODE", "staging")

        ctx = setup_extension("receipt_probe_staging", dev_mode=True)

        assert ctx.authz_ctx is not None
        assert ctx.authz_ctx.session_factory is not None

    def test_dev_mode_flag_in_production_override_is_not_stuck_either(self, database, monkeypatch):
        monkeypatch.setenv("AXIOM_MODE", "production")
        monkeypatch.setenv("AXIOM_ALLOW_DEV_MODE_IN_PRODUCTION", "1")

        ctx = setup_extension("receipt_probe_production", dev_mode=True)

        assert ctx.authz_ctx.session_factory is not None


class TestInDev:
    def test_the_tool_probe_runs_once_and_picks_the_no_op_sink(self, database, monkeypatch):
        monkeypatch.setenv("AXIOM_MODE", "dev")
        probes: list[int] = []
        real = database.get_engine

        def counting():
            probes.append(1)
            return real()

        monkeypatch.setattr(db, "get_engine", counting)

        first = _tool_decision()
        second = _tool_decision()

        assert first.decision is Decision.PERMIT
        assert second.decision is Decision.PERMIT
        assert len(probes) == 1
        assert database.opens == 0

    def test_a_dev_database_that_answers_gets_receipts(self, database, monkeypatch):
        monkeypatch.setenv("AXIOM_MODE", "dev")
        database.up = True

        verdict = _tool_decision()

        assert [row.id for row in database.receipts()] == [verdict.receipt_fragment_id]

    def test_setup_extension_in_dev_uses_the_sink_when_the_db_is_down(self, database, monkeypatch):
        monkeypatch.setenv("AXIOM_MODE", "dev")
        assert governance_simple._authz_db_answers() is False

        ctx = setup_extension("receipt_probe_dev", dev_mode=True)

        assert ctx.authz_ctx.session_factory is None

    def test_setup_extension_in_dev_probes_whatever_the_flag_says(self, database, monkeypatch):
        """The sink belongs to the runtime mode. ``dev_mode=False`` on a dev
        machine is a stricter rule set, not a promise that a table exists."""
        monkeypatch.setenv("AXIOM_MODE", "dev")

        ctx = setup_extension("receipt_probe_dev_flag_off", dev_mode=False)

        assert ctx.authz_ctx.session_factory is None


def _extension_receipt(ctx, verb: str = "go") -> str:
    with ctx.action(
        verb=verb, resource="receipt-probe://thing", classification=Classification.INTERNAL
    ) as act:
        return act.receipt_id


class TestATableThatStopsAnsweringAfterTheContextWasCached:
    """Every surface caches its context, so a probe's answer can outlive its target.

    A dev process that built its context against a migrated database and now
    writes to an unmigrated one (a repointed ``AXIOM_DB_URL``, a dropped schema)
    has a wired factory and no table.
    """

    def test_dev_tool_call_falls_to_the_no_op_sink(self, database, monkeypatch):
        monkeypatch.setenv("AXIOM_MODE", "dev")
        database.up = True
        assert _tool_decision().decision is Decision.PERMIT

        database.drop_receipt_table()

        verdict = _tool_decision()
        assert verdict.decision is Decision.PERMIT
        assert verdict.receipt_fragment_id.startswith("authz-unrecorded-")

    def test_dev_site_read_falls_to_the_no_op_sink(self, database, monkeypatch):
        monkeypatch.setenv("AXIOM_MODE", "dev")
        database.up = True
        assert _site_decision().decision is Decision.PERMIT

        database.drop_receipt_table()

        verdict = _site_decision()
        assert verdict.decision is Decision.PERMIT
        assert verdict.receipt_fragment_id.startswith("authz-unrecorded-")

    def test_dev_extension_action_falls_to_the_no_op_sink(self, database, monkeypatch):
        monkeypatch.setenv("AXIOM_MODE", "dev")
        database.up = True
        ctx = setup_extension("receipt_probe_cached", verbs=["go"], wire_vault=False)
        assert ctx.authz_ctx.session_factory is not None

        database.drop_receipt_table()

        assert _extension_receipt(ctx).startswith("authz-unrecorded-")

    def test_dev_table_comes_back_and_receipts_resume(self, database, monkeypatch):
        monkeypatch.setenv("AXIOM_MODE", "dev")
        database.up = True
        _tool_decision()
        database.drop_receipt_table()
        _tool_decision()

        Base.metadata.create_all(database.engine)

        verdict = _tool_decision()
        assert [row.id for row in database.receipts()] == [verdict.receipt_fragment_id]

    def test_dev_failed_write_with_a_good_table_still_denies(self, database, monkeypatch):
        """Only an unusable table reaches the sink. A write that fails for any
        other reason is still a decision with no receipt."""
        monkeypatch.setenv("AXIOM_MODE", "dev")
        database.up = True
        assert _tool_decision().decision is Decision.PERMIT

        def broken(extension_name: str):
            raise RuntimeError("disk full")

        monkeypatch.setattr(db, "session_for", broken)

        verdict = _tool_decision()
        assert verdict.decision is Decision.DENY
        assert verdict.reason == RECEIPT_WRITE_FAILED

    @pytest.mark.parametrize("mode", ["staging", "production"])
    def test_outside_dev_a_missing_table_denies_on_every_surface(
        self, database, monkeypatch, mode
    ):
        monkeypatch.setenv("AXIOM_MODE", mode)
        monkeypatch.setenv("AXIOM_ALLOW_DEV_MODE_IN_PRODUCTION", "1")
        database.up = True
        ctx = setup_extension(f"receipt_probe_{mode}", verbs=["go"], dev_mode=True, wire_vault=False)

        database.drop_receipt_table()

        assert _tool_decision().reason == RECEIPT_WRITE_FAILED
        assert _site_decision().reason == RECEIPT_WRITE_FAILED
        with pytest.raises(AuthorizationDenied, match=RECEIPT_WRITE_FAILED):
            _extension_receipt(ctx)


class TestTheProbeLooksWhereTheWriteGoes:
    def test_postgres_probe_asks_about_the_schema_session_for_uses(self, monkeypatch):
        """Asking about a schema the session never writes to answers for the
        wrong table. Per-worker test schemas are how that showed up."""
        asked: list[str | None] = []

        class _Inspector:
            def has_table(self, name, schema=None):
                asked.append(schema)
                return True

        class _Engine:
            class dialect:
                name = "postgresql"

        monkeypatch.setattr(db, "get_engine", lambda: _Engine())
        monkeypatch.setattr("sqlalchemy.inspect", lambda _engine: _Inspector())
        monkeypatch.setenv("AXIOM_TEST_SCHEMA_SUFFIX", "_gw3")

        assert governance_simple._authz_db_answers() is True
        assert asked == [db.normalize_extension_name("authz")] == ["authz_gw3"]
