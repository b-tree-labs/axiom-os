# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the graduation writer and the lookup that reads it (prd-axiom-authz §5.3).

decide() writes a ``propose_to_human`` receipt; a human resolves it; the writer
counts that resolution into ``authz.graduation``; the next decide() of the same
(actor, intent, resource) reads the row. In-memory SQLite, except
``TestOnPostgres``: row locks and blocking unique checks are Postgres behaviour,
so the races are proved there against the real migrations.
"""

from __future__ import annotations

import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, event, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from axiom.extensions.builtins import authz as authz_extension
from axiom.extensions.builtins.authz import graduation
from axiom.extensions.builtins.authz.db_models import Base, Graduation, Verdict
from axiom.extensions.builtins.authz.decide import DecideContext, decide
from axiom.extensions.builtins.authz.policy import GraduationSource
from axiom.extensions.builtins.authz.rules import Rule
from axiom.extensions.builtins.authz.skills import explain as explain_skill
from axiom.governance import (
    ActionEnvelope,
    ActionIntent,
    CapabilityToken,
    Classification,
    Decision,
    IntentPattern,
    ProvenanceRef,
    ResourcePattern,
    ResourceRef,
)
from axiom.vega.identity.principal import Principal

THRESHOLD = 3
REPO = "git://example-repo/main"


@pytest.fixture()
def factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    @contextlib.contextmanager
    def _cm():
        s = session_local()
        try:
            yield s
        finally:
            s.close()

    return _cm


def _env(intent: str = "example.comment", resource: str = REPO) -> ActionEnvelope:
    agent = Principal(handle="@agent:test", public_bytes=b"\x00" * 32)
    return ActionEnvelope(
        actor=agent,
        capability=CapabilityToken.unscoped_test_token(subject=agent),
        classification=Classification.INTERNAL,
        context_fragment_id="memory://localhost/test",
        provenance_parent=ProvenanceRef.synthetic("test"),
        federation_origin=None,
        intent=ActionIntent(intent),
        resource=ResourceRef.parse(resource),
        deadline=None,
        dedup_key="test-dedup",
    )


def _decide(factory, **kw):
    """decide() and return (decision, the receipt row it wrote)."""
    verdict = decide(_env(**kw), DecideContext(session_factory=factory))
    with factory() as s:
        row = s.get(Verdict, verdict.receipt_fragment_id)
    return verdict.decision, row


def _approve(factory, n: int, **kw) -> None:
    for _ in range(n):
        decision, row = _decide(factory, **kw)
        assert decision is Decision.PROPOSE_TO_HUMAN
        with factory() as s:
            graduation.record_approval(s, row, by="@human:test", threshold=THRESHOLD)


def _rows(factory) -> list[tuple]:
    with factory() as s:
        rows = s.execute(select(Graduation)).scalars().all()
    return sorted(
        (r.actor, r.intent_class, r.resource_pattern, r.approvals, r.threshold, r.graduated)
        for r in rows
    )


def test_graduates_at_exactly_the_threshold(factory):
    _approve(factory, THRESHOLD - 1)
    assert _decide(factory)[0] is Decision.PROPOSE_TO_HUMAN
    _approve(factory, 1)
    assert _decide(factory)[0] is Decision.PERMIT
    assert _rows(factory) == [
        ("@agent:test", "example.comment", REPO, THRESHOLD, THRESHOLD, True)
    ]


def test_same_primitive_different_intent_stays_ungraduated(factory):
    _approve(factory, THRESHOLD)
    assert _decide(factory, intent="example.merge")[0] is Decision.PROPOSE_TO_HUMAN


def test_resource_outside_the_rows_pattern_stays_ungraduated(factory):
    _approve(factory, THRESHOLD)
    other = "git://other-repo/main"
    assert _decide(factory, resource=other)[0] is Decision.PROPOSE_TO_HUMAN


def test_a_wider_pattern_row_matches_by_resource_pattern(factory):
    with factory() as s:
        s.add(Graduation(
            id="g-wide", actor="@agent:test", intent_class="example.comment",
            resource_pattern="git://example-repo/*", approvals=THRESHOLD,
            threshold=THRESHOLD, graduated=True, last_update=datetime.now(UTC),
        ))
        s.commit()
    assert _decide(factory)[0] is Decision.PERMIT
    assert _decide(factory, resource="git://other-repo/main")[0] is Decision.PROPOSE_TO_HUMAN


def test_stale_graduated_flag_below_threshold_does_not_permit(factory):
    with factory() as s:
        s.add(Graduation(
            id="g-stale", actor="@agent:test", intent_class="example.comment",
            resource_pattern=REPO, approvals=0, threshold=THRESHOLD, graduated=True,
            last_update=datetime.now(UTC),
        ))
        s.commit()
    assert _decide(factory)[0] is Decision.PROPOSE_TO_HUMAN


def test_denial_after_graduating_resets(factory):
    _, pending = _decide(factory)  # still open when the pair graduates
    _approve(factory, THRESHOLD)
    with factory() as s:
        s.add(Graduation(
            id="g-wide", actor="@agent:test", intent_class="example.comment",
            resource_pattern="git://example-repo/*", approvals=THRESHOLD,
            threshold=THRESHOLD, graduated=True, last_update=datetime.now(UTC),
        ))
        s.commit()
    with factory() as s:
        graduation.record_denial(s, pending, by="@human:test")
    # Every row that covers the resource is reset, not only the exact one.
    assert [r[3:] for r in _rows(factory)] == [(0, THRESHOLD, False), (0, THRESHOLD, False)]
    assert _decide(factory)[0] is Decision.PROPOSE_TO_HUMAN


def test_revert_of_a_taken_action_resets(factory):
    _approve(factory, THRESHOLD)
    decision, taken = _decide(factory)
    assert decision is Decision.PERMIT
    with factory() as s:
        graduation.record_revert(s, taken, by="@human:test")
    assert _rows(factory)[0][3:] == (0, THRESHOLD, False)
    assert _decide(factory)[0] is Decision.PROPOSE_TO_HUMAN


def test_resolutions_are_receipts(factory):
    _approve(factory, 1)
    with factory() as s:
        proposal = s.execute(
            select(Verdict).where(Verdict.decision == "propose_to_human")
        ).scalars().one()
        resolutions = s.execute(
            select(Verdict).where(Verdict.provenance_parent == proposal.id)
        ).scalars().all()
    assert [(r.decision, r.reason) for r in resolutions] == [
        (graduation.RESOLUTION, f"{graduation.APPROVED} by @human:test")
    ]


def test_record_refuses_the_wrong_kind_of_receipt(factory):
    _approve(factory, THRESHOLD)
    decision, permitted = _decide(factory)
    assert decision is Decision.PERMIT
    with factory() as s, pytest.raises(ValueError):
        graduation.record_approval(s, permitted, by="@human:test")


def test_rebuild_from_receipts_gives_the_same_rows(factory):
    _approve(factory, THRESHOLD)
    _, pending = _decide(factory, intent="example.merge")
    _approve(factory, 2, intent="example.merge")
    with factory() as s:
        graduation.record_denial(s, pending, by="@human:test")
    _approve(factory, 1, intent="example.merge")
    before = _rows(factory)
    with factory() as s:
        graduation.rebuild(s)
    assert _rows(factory) == before


class TestRebuild:
    def _imported(self, factory, **kw):
        values = dict(
            id="g-imported", actor="@agent:test", intent_class="example.import",
            resource_pattern="git://imported/*", approvals=9, threshold=9, graduated=True,
            last_update=datetime.now(UTC),
        )
        values.update(kw)
        with factory() as s:
            s.add(Graduation(**values))
            s.commit()

    def test_a_row_no_receipt_touches_stays_as_it_stands(self, factory):
        self._imported(factory)
        _approve(factory, 2)
        with factory() as s:
            graduation.rebuild(s)
        assert ("@agent:test", "example.import", "git://imported/*", 9, 9, True) in _rows(factory)

    def test_a_row_keeps_its_threshold(self, factory):
        """A default here would graduate a row that was held to a higher bar."""
        _approve(factory, 3)  # THRESHOLD = 3
        with factory() as s:
            s.query(Graduation).update({Graduation.threshold: 8, Graduation.graduated: False})
            s.commit()
        with factory() as s:
            graduation.rebuild(s)
        assert [r[3:] for r in _rows(factory)] == [(3, 8, False)]

    def test_a_lost_table_comes_back_at_the_threshold_it_was_counted_against(self, factory):
        _approve(factory, 2)
        with factory() as s:
            s.execute(delete(Graduation))
            s.commit()
        with factory() as s:
            graduation.rebuild(s)
        assert [r[3:] for r in _rows(factory)] == [(2, THRESHOLD, False)]

    def test_replaying_twice_changes_nothing(self, factory):
        _approve(factory, 2)
        before = _rows(factory)
        for _ in range(2):
            with factory() as s:
                graduation.rebuild(s)
        assert _rows(factory) == before


class TestOneProposalCountsOnce:
    def test_a_second_approval_of_one_receipt_is_refused(self, factory):
        _, proposal = _decide(factory)
        with factory() as s:
            graduation.record_approval(s, proposal, by="@human:test", threshold=THRESHOLD)
        with factory() as s, pytest.raises(graduation.AlreadyResolved):
            graduation.record_approval(s, proposal, by="@other:test", threshold=THRESHOLD)
        assert [r[3] for r in _rows(factory)] == [1]

    def test_replaying_one_approval_cannot_graduate(self, factory):
        _, proposal = _decide(factory)
        for _ in range(THRESHOLD):
            with contextlib.suppress(graduation.AlreadyResolved), factory() as s:
                graduation.record_approval(s, proposal, by="@human:test", threshold=THRESHOLD)
        assert _decide(factory)[0] is Decision.PROPOSE_TO_HUMAN

    def test_a_denied_proposal_cannot_then_be_approved(self, factory):
        _, proposal = _decide(factory)
        with factory() as s:
            graduation.record_denial(s, proposal, by="@human:test")
        with factory() as s, pytest.raises(graduation.AlreadyResolved):
            graduation.record_approval(s, proposal, by="@human:test", threshold=THRESHOLD)
        assert _rows(factory) == []

    def test_an_approved_proposal_cannot_then_be_denied(self, factory):
        _, proposal = _decide(factory)
        with factory() as s:
            graduation.record_approval(s, proposal, by="@human:test", threshold=THRESHOLD)
        with factory() as s, pytest.raises(graduation.AlreadyResolved):
            graduation.record_denial(s, proposal, by="@human:test")

    def test_a_refused_replay_leaves_no_receipt(self, factory):
        _, proposal = _decide(factory)
        with factory() as s:
            graduation.record_approval(s, proposal, by="@human:test", threshold=THRESHOLD)
        with factory() as s, pytest.raises(graduation.AlreadyResolved):
            graduation.record_approval(s, proposal, by="@human:test", threshold=THRESHOLD)
        with factory() as s:
            count = s.scalar(
                select(func.count()).select_from(Verdict)
                .where(Verdict.provenance_parent == proposal.id)
            )
        assert count == 1

    def test_one_action_is_reverted_once(self, factory):
        _approve(factory, THRESHOLD)
        _, taken = _decide(factory)
        with factory() as s:
            graduation.record_revert(s, taken, by="@human:test")
        with factory() as s, pytest.raises(graduation.AlreadyResolved):
            graduation.record_revert(s, taken, by="@human:test")

    def test_the_refusal_is_the_databases(self, factory):
        """Not a check before a write: the primary key itself refuses."""
        with factory() as s:
            s.add(graduation.GraduationResolution(
                resolved_receipt_id="r-1", resolution_id="a", threshold=THRESHOLD))
            s.commit()
        with factory() as s:
            s.add(graduation.GraduationResolution(
                resolved_receipt_id="r-1", resolution_id="b", threshold=THRESHOLD))
            with pytest.raises(IntegrityError):
                s.commit()


def test_a_second_row_for_one_key_is_refused_by_the_database(factory):
    values = dict(
        actor="@agent:test", intent_class="example.comment", resource_pattern=REPO,
        approvals=0, threshold=THRESHOLD, graduated=False, last_update=datetime.now(UTC),
    )
    with factory() as s:
        s.add(Graduation(id="g-1", **values))
        s.commit()
    with factory() as s:
        s.add(Graduation(id="g-2", **values))
        with pytest.raises(IntegrityError):
            s.commit()


class TestWhoMayApprove:
    @pytest.mark.parametrize("by", ["@agent:test", "@agent", "@agent:elsewhere", "@AGENT:test"])
    def test_an_actor_cannot_approve_itself(self, factory, by):
        _, proposal = _decide(factory)
        with factory() as s, pytest.raises(graduation.ResolutionRefused):
            graduation.record_approval(s, proposal, by=by, threshold=THRESHOLD)
        assert _rows(factory) == []

    @pytest.mark.parametrize("by", ["", "human", "@a:b:c", "a@b.example", "@a@b"])
    @pytest.mark.parametrize("record", ["record_approval", "record_denial"])
    def test_a_resolver_must_be_a_well_formed_handle(self, factory, record, by):
        _, proposal = _decide(factory)
        with factory() as s, pytest.raises(graduation.ResolutionRefused):
            getattr(graduation, record)(s, proposal, by=by, threshold=THRESHOLD)
        assert _rows(factory) == []

    @pytest.mark.parametrize(
        "by", ["@gate:auto", "@gate", "@system:local", "@authority:tools", "@rivet", "@axi:site", "@GUARD"]
    )
    def test_a_known_machine_cannot_approve(self, factory, by):
        _, proposal = _decide(factory)
        with factory() as s, pytest.raises(graduation.ResolutionRefused):
            graduation.record_approval(s, proposal, by=by, threshold=THRESHOLD)
        assert _rows(factory) == []

    def test_a_refused_resolver_leaves_the_proposal_open(self, factory):
        _, proposal = _decide(factory)
        with factory() as s, pytest.raises(graduation.ResolutionRefused):
            graduation.record_approval(s, proposal, by="@agent:test", threshold=THRESHOLD)
        with factory() as s:
            graduation.record_approval(s, proposal, by="@human:test", threshold=THRESHOLD)
        assert [r[3] for r in _rows(factory)] == [1]

    def test_an_installed_agent_cannot_approve(self, factory, monkeypatch):
        monkeypatch.setattr(graduation, "_agent_names", lambda: frozenset({"site-agent"}))
        _, proposal = _decide(factory)
        with factory() as s, pytest.raises(graduation.ResolutionRefused):
            graduation.record_approval(s, proposal, by="@site-agent:local", threshold=THRESHOLD)

    def test_with_no_roster_to_read_nobody_can_approve(self, factory, monkeypatch):
        def unreadable(**_kw):
            raise RuntimeError("no roster")

        monkeypatch.setattr("axiom.agents.roster.load_roster", unreadable)
        graduation._roster_names.cache_clear()
        _, proposal = _decide(factory)
        with factory() as s, pytest.raises(graduation.ResolutionRefused):
            graduation.record_approval(s, proposal, by="@human:test", threshold=THRESHOLD)
        graduation._roster_names.cache_clear()

    def test_a_machine_can_lower_trust(self, factory):
        """Refusal guards raising it. A reset only ever sends the actor back to a person."""
        _approve(factory, THRESHOLD)
        _, taken = _decide(factory)
        with factory() as s:
            graduation.record_revert(s, taken, by="@rivet:local")
        assert _rows(factory)[0][3:] == (0, THRESHOLD, False)


class TestAResolutionIsNotAPermit:
    def test_nothing_counting_permits_counts_a_resolution(self, factory):
        _approve(factory, 2)
        with factory() as s:
            permits = s.scalar(
                select(func.count()).select_from(Verdict).where(Verdict.decision == "permit")
            )
        assert permits == 0

    def test_the_marker_is_no_decision_decide_can_make(self):
        assert graduation.RESOLUTION not in {d.value for d in Decision}

    def test_an_approved_action_can_be_reverted_through_its_resolution(self, factory):
        """An action a human approved has no ``permit`` receipt: the approval is
        what let it run, so that is what a revert names."""
        _approve(factory, 2)
        with factory() as s:
            resolution = s.execute(
                select(Verdict).where(Verdict.decision == graduation.RESOLUTION)
            ).scalars().first()
            graduation.record_revert(s, resolution, by="@human:test")
        assert [r[3] for r in _rows(factory)] == [0]

    def test_a_reset_cannot_itself_be_reverted(self, factory):
        _approve(factory, THRESHOLD)
        _, taken = _decide(factory)
        with factory() as s:
            graduation.record_revert(s, taken, by="@human:test")
        with factory() as s:
            reset = s.execute(
                select(Verdict).where(Verdict.provenance_parent == taken.id)
            ).scalars().one()
            with pytest.raises(graduation.ResolutionRefused):
                graduation.record_revert(s, reset, by="@human:test")

    def test_a_resolution_cannot_be_approved_as_a_proposal(self, factory):
        _approve(factory, 1)
        with factory() as s:
            resolution = s.execute(
                select(Verdict).where(Verdict.decision == graduation.RESOLUTION)
            ).scalars().first()
            with pytest.raises(graduation.ResolutionRefused):
                graduation.record_approval(s, resolution, by="@human:test")

    def test_explain_says_what_it_is(self, factory):
        _approve(factory, 1)
        with factory() as s:
            resolution = s.execute(
                select(Verdict).where(Verdict.decision == graduation.RESOLUTION)
            ).scalars().first()
            narrative = explain_skill._build_narrative(resolution, [], None, None)
        assert "was resolved" in narrative
        assert "no rules matched" not in narrative


def test_an_explicit_propose_rule_outranks_a_graduated_row(factory):
    """Graduation only fills the gap no rule speaks to, so a floor stays a floor."""
    _approve(factory, THRESHOLD)
    ctx = DecideContext(session_factory=factory)
    ctx.add_rule(Rule(
        name="site_propose",
        intent_pattern=IntentPattern("example.comment"),
        actor_pattern="*",
        resource_pattern=ResourcePattern(REPO),
        disposition="propose",
    ))
    assert decide(_env(), ctx).decision is Decision.PROPOSE_TO_HUMAN


def test_graduation_source_never_denies():
    assert GraduationSource().may_deny is False


def _upgrade_authz() -> None:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(authz_extension.__file__).parent / "migrations")
    )
    command.upgrade(config, "head")


class TestOnPostgres:
    """What SQLite cannot show: real row locks and a primary key that blocks."""

    @pytest.fixture
    def pg(self, fresh_postgres, monkeypatch):
        from axiom.infra.db import session_for

        monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)
        _upgrade_authz()
        return lambda: session_for("authz")

    def _row(self, pg) -> tuple[int, bool]:
        with pg() as s:
            row = s.execute(select(Graduation)).scalars().one()
            return row.approvals, row.graduated

    def test_the_migration_builds_what_the_writer_needs(self, pg):
        from axiom.infra.db import get_engine

        tables = inspect(get_engine()).get_table_names(schema="authz")
        assert "graduation_resolutions" in tables
        unique = inspect(get_engine()).get_indexes("graduation", schema="authz")
        assert any(
            ix["unique"] and ix["column_names"] == ["actor", "intent_class", "resource_pattern"]
            for ix in unique
        )

    def test_many_replays_at_once_count_once(self, pg):
        _, proposal = _decide(pg)
        start = threading.Barrier(8)

        def resolve(_i):
            start.wait(timeout=10)
            with pg() as s:
                try:
                    graduation.record_approval(s, proposal, by="@human:test", threshold=THRESHOLD)
                except graduation.AlreadyResolved:
                    return False
            return True

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(resolve, range(8)))

        assert results.count(True) == 1
        assert self._row(pg) == (1, False)

    def test_a_denial_survives_an_approval_that_read_first(self, pg):
        """The lost reset. The approval has read the count and not yet written it
        when the denial arrives. If it reads unlocked, the denial's reset lands and
        the approval then writes the old count + 1 over it, and the pair graduates
        on a record that holds a denial."""
        _approve(pg, THRESHOLD - 2)
        _, approving = _decide(pg)
        _, denying = _decide(pg)
        with pg() as s:
            graduation.record_approval(s, approving, by="@human:test", threshold=THRESHOLD)
        _, approving = _decide(pg)
        approval_has_read, approval_may_commit = threading.Event(), threading.Event()

        def approve():
            with pg() as s:
                def pause(_session):
                    approval_has_read.set()
                    approval_may_commit.wait(timeout=20)

                event.listen(s, "before_commit", pause)
                graduation.record_approval(s, approving, by="@human:test", threshold=THRESHOLD)

        def deny():
            with pg() as s:
                graduation.record_denial(s, denying, by="@human:test")

        approval = threading.Thread(target=approve)
        approval.start()
        assert approval_has_read.wait(timeout=20)
        denial = threading.Thread(target=deny)
        denial.start()
        denial.join(timeout=2)  # with a lock it is waiting; without one it has finished
        approval_may_commit.set()
        approval.join(timeout=20)
        denial.join(timeout=20)

        assert self._row(pg) == (0, False)
        assert _decide(pg)[0] is Decision.PROPOSE_TO_HUMAN

    def test_first_approvals_at_once_share_one_row(self, pg):
        proposals = [_decide(pg)[1] for _ in range(6)]
        start = threading.Barrier(6)

        def resolve(proposal):
            start.wait(timeout=10)
            with pg() as s:
                graduation.record_approval(s, proposal, by="@human:test", threshold=10)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(resolve, proposals))

        assert self._row(pg) == (6, False)

    def test_a_row_is_read_fresh_under_the_lock(self, pg):
        """A session holding a stale copy must not count from it."""
        _approve(pg, 2)
        _, proposal = _decide(pg)
        with pg() as s:
            stale = s.execute(select(Graduation)).scalars().one()
            assert stale.approvals == 2
            with pg() as other:
                other.query(Graduation).update({Graduation.approvals: 0})
                other.commit()
            graduation.record_approval(s, proposal, by="@human:test", threshold=THRESHOLD)
        assert self._row(pg) == (1, False)

    def test_a_denial_survives_a_rebuild_that_read_first(self, pg):
        """A rebuild that listed the receipts before a denial committed would
        recompute the row without it, which is the lost reset by another door."""
        _approve(pg, THRESHOLD - 1)
        _, pending = _decide(pg)
        _approve(pg, 1)
        assert self._row(pg) == (THRESHOLD, True)
        denial_has_locked, denial_may_commit = threading.Event(), threading.Event()

        def deny():
            with pg() as s:
                def pause(_session):
                    denial_has_locked.set()
                    denial_may_commit.wait(timeout=20)

                event.listen(s, "before_commit", pause)
                graduation.record_denial(s, pending, by="@human:test")

        def rebuild():
            with pg() as s:
                graduation.rebuild(s)

        denial = threading.Thread(target=deny)
        denial.start()
        assert denial_has_locked.wait(timeout=20)
        rebuilding = threading.Thread(target=rebuild)
        rebuilding.start()
        rebuilding.join(timeout=2)
        denial_may_commit.set()
        denial.join(timeout=20)
        rebuilding.join(timeout=20)

        assert self._row(pg) == (0, False)

    def test_rebuild_runs_on_the_real_schema(self, pg):
        _approve(pg, 1)
        with pg() as s:
            before = _rows_of(s)
            graduation.rebuild(s)
            assert _rows_of(s) == before


def _rows_of(session) -> list[tuple[int, int, bool]]:
    return sorted(
        (r.approvals, r.threshold, r.graduated)
        for r in session.execute(select(Graduation)).scalars()
    )
