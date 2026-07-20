# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the P4 managed sync service (ADR-087 D2, PRD F6, scope item 3).

The sync service runs on the existing schedule substrate: a service block the
engine ticks, event-driven (a change trigger enqueues; the tick dispatches),
under the service-reliability contract — LeaseManager single-flight, a durable
fire-log + pending queue, recovery after downtime with NO loss and NO echo
storm, and an injectable clock (no wall-clock reads).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.schedule.lease import LeaseManager
from axiom.memory.absorb.markdown_hierarchy import agents_md_adapter
from axiom.memory.rendering import SESSION_BOUNDARY
from axiom.memory.serving import TIER_LOCAL
from axiom.memory.sync.detect import ChangeDetector
from axiom.memory.sync.engine import SyncEngine
from axiom.memory.sync.service import (
    SYNC_TICK_ACTION,
    SyncExecutor,
    SyncPeer,
    SyncService,
)
from axiom.memory.sync.writeback import MultiTargetWriteBack

PRINCIPAL = "@alice:home"
T0 = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t = self.t + timedelta(seconds=seconds)


def _make_composition(base: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base.mkdir(parents=True, exist_ok=True)
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


ACCOUNTS = frozenset({"acct-a", "acct-b"})


def _peer(name, account, root, clock) -> SyncPeer:
    adapter = agents_md_adapter(account=account, roots=[root])
    det = ChangeDetector(adapter=adapter, now_fn=lambda: clock().isoformat())
    return SyncPeer(
        harness=name,
        account=account,
        detector=det,
        writeback=MultiTargetWriteBack(root=root, products=("agents_md",)),
        deployment_tier=TIER_LOCAL,
    )


def _service(composition, peers, clock) -> SyncService:
    engine = SyncEngine(
        composition=composition, principal=PRINCIPAL, account_set=ACCOUNTS,
        now_fn=lambda: clock().isoformat(),
    )
    return SyncService(
        composition=composition,
        engine=engine,
        peers=peers,
        lease=LeaseManager(node_id="node-a", ttl_seconds=30),
        now_fn=clock,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _live(composition):
    return composition.artifact_registry.list(kind="fragment")


@pytest.fixture
def env(tmp_path):
    clock = _Clock(T0)
    composition = _make_composition(tmp_path / "node")
    root_a = tmp_path / "harness-a"
    root_b = tmp_path / "harness-b"
    peer_a = _peer("agents-md", "acct-a", root_a, clock)
    peer_b = _peer("agents-md", "acct-b", root_b, clock)
    svc = _service(composition, [peer_a, peer_b], clock)
    return {
        "clock": clock, "composition": composition, "svc": svc,
        "root_a": root_a, "root_b": root_b,
    }


# ---------------------------------------------------------------------------
# Event-driven: a change trigger enqueues; the tick dispatches
# ---------------------------------------------------------------------------


class TestEventDriven:
    def test_empty_tick_is_a_noop(self, env):
        report = env["svc"].tick()
        assert report.applied == 0
        assert env["svc"].pending_count() == 0

    def test_poll_enqueues_then_tick_applies(self, env):
        _write(env["root_a"] / "AGENTS.md", "# Rules\n\nPrefer ruff.\n")
        enq = env["svc"].poll_and_enqueue()
        assert enq == 1
        assert env["svc"].pending_count() == 1
        # Nothing imported until the engine ticks.
        assert _live(env["composition"]) == []
        report = env["svc"].tick()
        assert report.applied == 1
        assert env["svc"].pending_count() == 0
        assert len(_live(env["composition"])) == 1


# ---------------------------------------------------------------------------
# Single-flight: only the lease holder drains
# ---------------------------------------------------------------------------


class TestSingleFlight:
    def test_non_leader_tick_skips(self, env):
        _write(env["root_a"] / "AGENTS.md", "# Rules\n\nPrefer ruff.\n")
        env["svc"].poll_and_enqueue()
        # Another node already holds the lease; our node cannot acquire it.
        contended = LeaseManager(node_id="other-node", ttl_seconds=30)
        contended.try_acquire(env["clock"]())  # held by 'other-node'
        contended.node_id = "node-a"  # our identity differs from the holder
        env["svc"].lease = contended
        report = env["svc"].tick()
        assert report.skipped == "not-leader"
        assert env["svc"].pending_count() == 1  # nothing drained


# ---------------------------------------------------------------------------
# Two-harness lock-step through the Axiom hub
# ---------------------------------------------------------------------------


class TestLockStep:
    def test_change_in_a_propagates_to_b(self, env):
        svc, composition = env["svc"], env["composition"]
        _write(env["root_a"] / "AGENTS.md", "# Rules\n\nAlways run ruff.\n")
        svc.poll_and_enqueue()
        svc.tick()  # inbound: A → Axiom
        # Outbound flush at a session boundary → B's instruction file.
        report = svc.tick(cadence=SESSION_BOUNDARY)
        b_agents = env["root_b"] / "AGENTS.md"
        assert b_agents.exists()
        assert "Always run ruff." in b_agents.read_text()
        assert any(str(b_agents) in w for w in report.written)


# ---------------------------------------------------------------------------
# Kill-and-restart: no loss, no echo storm, exactly once
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_restart_drains_pending_no_loss(self, env):
        svc, composition = env["svc"], env["composition"]
        _write(env["root_a"] / "AGENTS.md", "# Rules\n\nPrefer ruff.\n")
        svc.poll_and_enqueue()
        # "Crash" before ticking — pending is durable.
        assert svc.pending_count() == 1
        # A brand-new service instance over the same store (a restart).
        clock2 = _Clock(T0 + timedelta(seconds=5))
        peer_a = _peer("agents-md", "acct-a", env["root_a"], clock2)
        peer_b = _peer("agents-md", "acct-b", env["root_b"], clock2)
        svc2 = _service(composition, [peer_a, peer_b], clock2)
        assert svc2.pending_count() == 1  # recovered the durable pending item
        rep = svc2.recover()
        assert rep.applied == 1
        assert len(_live(composition)) == 1

    def test_reprocessing_lands_exactly_once(self, env):
        svc, composition = env["svc"], env["composition"]
        _write(env["root_a"] / "AGENTS.md", "# Rules\n\nPrefer ruff.\n")
        svc.poll_and_enqueue()
        svc.tick()
        assert len(_live(composition)) == 1
        # Re-poll (baseline reset, as after a restart) + re-tick: idempotent.
        svc.poll_and_enqueue()
        svc.tick()
        svc.recover()
        assert len(_live(composition)) == 1  # exactly once

    def test_no_echo_storm_after_writeback(self, env):
        svc, composition = env["svc"], env["composition"]
        _write(env["root_a"] / "AGENTS.md", "# Rules\n\nAlways run ruff.\n")
        svc.poll_and_enqueue()
        svc.tick()
        svc.tick(cadence=SESSION_BOUNDARY)  # writes A's memory into B's file
        n_before = len(_live(composition))
        # B's detector now reads its own file (with our managed block).
        # Recovery re-polls every peer: our block must NOT re-import.
        svc.recover()
        svc.recover()
        assert len(_live(composition)) == n_before  # no echo storm


# ---------------------------------------------------------------------------
# Schedule-engine integration: the runner ticks the service block
# ---------------------------------------------------------------------------


class TestScheduleEngineTicksService:
    def test_engine_dispatches_to_sync_executor(self, env, monkeypatch):
        from axiom.extensions.builtins.schedule import engine as sched_engine
        from axiom.extensions.builtins.schedule.engine import EngineContext, tick

        svc = env["svc"]
        _write(env["root_a"] / "AGENTS.md", "# Rules\n\nPrefer ruff.\n")
        svc.poll_and_enqueue()

        executor = SyncExecutor(service=svc)

        class _Authz:
            def decide(self, envelope):
                return True

        class _FireLog:
            def claim(self, *a, **k):
                return True

            def record_skipped(self, *a, **k):
                pass

            def record_outcome(self, *a, **k):
                pass

        due_row = {
            "id": "sync-schedule",
            "action": SYNC_TICK_ACTION,
            "cadence_kind": "interval",
            "cadence_payload": {"seconds": 60},
            "retry_policy": {},
            "misfire_policy": "fire_once",
            "compliance_window_seconds": None,
            "compliance_action": "flag",
            "capability_envelope": {"cadence": None},
            "next_fire_at": env["clock"](),
        }
        monkeypatch.setattr(sched_engine, "_pull_due", lambda c, n: [due_row])
        monkeypatch.setattr(sched_engine, "_advance", lambda *a, **k: None)
        # Stub the schedule-DB touchpoints in _fire_one (no schedule schema here).
        from axiom.extensions.builtins.schedule import blackout, hooks
        monkeypatch.setattr(blackout, "in_blackout", lambda now: False)
        monkeypatch.setattr(hooks, "gate", lambda phase, payload: (True, None))
        monkeypatch.setattr(hooks, "emit", lambda phase, payload: None)

        ctx = EngineContext(
            session=lambda: None,
            authz=_Authz(),
            fire_log=_FireLog(),
            executor=executor,
            lease=LeaseManager(node_id="sched", ttl_seconds=30),
            now_fn=env["clock"],
        )
        report = tick(ctx)
        assert report.fired == 1
        # The dispatch actually ran the sync tick: the pending change applied.
        assert len(_live(env["composition"])) == 1
