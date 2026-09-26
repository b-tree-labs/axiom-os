# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the data-platform orchestrator service — PULSE's production host.

The audit gap this closes: PULSE could STORE cadences but nothing fired
them in production (executor + tick host were test-doubles only). These
tests drive the real chain — BackupPolicy → default cadences →
engine.tick → SkillExecutor → skill — against the SQLite store harness
with a synthetic clock. No sleeps, no wall-clock, no Postgres.
"""

from __future__ import annotations

import contextlib
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.data_platform import _herald
from axiom.extensions.builtins.data_platform.database.backup_policy import (
    BackupPolicy,
    save_backup_policy,
)
from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
    ConnectorConfig,
    delete_connector,
    save_connector,
)
from axiom.extensions.builtins.data_platform.orchestration.service import (
    BACKUP_ACTION,
    DEFAULT_FRESHNESS_SCHEDULE,
    FRESHNESS_ACTION,
    REFRESH_ACTION,
    VALIDATE_ACTION,
    OrchestratorService,
)
from axiom.extensions.builtins.schedule import store
from axiom.extensions.builtins.schedule.db_models import (
    Base,
    ScheduleDefinition,
    ScheduleFireLog,
)
from axiom.infra.skills import SkillRegistry, SkillResult

T0 = datetime(2026, 7, 11, 1, 0, 0, tzinfo=UTC)


@pytest.fixture
def sqlite_store():
    """Bind PULSE's session provider to in-memory SQLite (schedule-conftest
    pattern) so the fire loop runs without Postgres."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, future=True)

    @contextlib.contextmanager
    def provider():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    store.set_provider(provider)
    try:
        yield
    finally:
        store.reset_provider()
        engine.dispose()


@pytest.fixture(autouse=True)
def _clear_hooks():
    from axiom.extensions.builtins.schedule import hooks

    hooks.clear()
    yield
    hooks.clear()


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t = self.t + timedelta(**kw)


def _events(monkeypatch) -> list[tuple[str, str]]:
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        _herald,
        "publish_event",
        lambda intent, summary, **kw: captured.append((intent, summary)) or "rcpt-test",
    )
    return captured


def _enabled_policy(tmp_path: Path, **overrides) -> BackupPolicy:
    policy = BackupPolicy(
        enabled=True,
        target_root=str(tmp_path / "backups"),
        **overrides,
    )
    save_backup_policy(policy, state_dir=tmp_path)
    return policy


def _service(
    tmp_path: Path, clock: _Clock, registry: SkillRegistry | None = None, **kw
) -> OrchestratorService:
    return OrchestratorService(
        registry=registry if registry is not None else SkillRegistry(),
        state_dir=tmp_path,
        clock=clock,
        **kw,
    )


def _rows(action: str) -> list[ScheduleDefinition]:
    with store.session_scope() as s:
        rows = s.query(ScheduleDefinition).filter(ScheduleDefinition.action == action).all()
        for r in rows:
            s.expunge(r)
        return rows


def _recording_registry(results: dict[str, SkillResult] | None = None):
    """A registry whose data.backup / data.backup_validate record calls."""
    calls: list[str] = []
    registry = SkillRegistry()
    for name in (BACKUP_ACTION, VALIDATE_ACTION):

        def _mk(n):
            def skill(params, ctx):
                calls.append(n)
                if results and n in results:
                    return results[n]
                return SkillResult(ok=True, value={"receipt": f"rcpt-{n}"})

            return skill

        registry.register(name, _mk(name))
    return registry, calls


class TestEnsureBackupCadences:
    def test_enabled_policy_registers_both_cadences(self, sqlite_store, tmp_path):
        _enabled_policy(tmp_path)
        svc = _service(tmp_path, _Clock(T0))
        report = svc.ensure_backup_cadences()
        assert report["enabled"] is True
        backup_rows = _rows(BACKUP_ACTION)
        validate_rows = _rows(VALIDATE_ACTION)
        assert len(backup_rows) == 1 and len(validate_rows) == 1
        assert backup_rows[0].state == "active"
        assert backup_rows[0].cadence_kind == "cron"
        assert backup_rows[0].cadence_payload["cron_expr"] == "0 2 * * *"
        assert backup_rows[0].misfire_policy == "fire_once"
        assert validate_rows[0].cadence_payload["cron_expr"] == "30 6 * * *"

    def test_idempotent(self, sqlite_store, tmp_path):
        _enabled_policy(tmp_path)
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_backup_cadences()
        svc.ensure_backup_cadences()
        assert len(_rows(BACKUP_ACTION)) == 1
        assert len(_rows(VALIDATE_ACTION)) == 1

    def test_policy_edit_reschedules(self, sqlite_store, tmp_path):
        _enabled_policy(tmp_path)
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_backup_cadences()
        _enabled_policy(tmp_path, schedule="0 3 * * *")
        svc.ensure_backup_cadences()
        rows = _rows(BACKUP_ACTION)
        assert len(rows) == 1
        assert rows[0].cadence_payload["cron_expr"] == "0 3 * * *"

    def test_disabled_policy_pauses(self, sqlite_store, tmp_path):
        _enabled_policy(tmp_path)
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_backup_cadences()
        save_backup_policy(
            BackupPolicy(enabled=False, target_root=str(tmp_path / "backups")),
            state_dir=tmp_path,
        )
        report = svc.ensure_backup_cadences()
        assert report["enabled"] is False
        assert _rows(BACKUP_ACTION)[0].state == "paused"
        assert _rows(VALIDATE_ACTION)[0].state == "paused"

    def test_reenabled_policy_resumes(self, sqlite_store, tmp_path):
        _enabled_policy(tmp_path)
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_backup_cadences()
        save_backup_policy(
            BackupPolicy(enabled=False, target_root=str(tmp_path / "backups")),
            state_dir=tmp_path,
        )
        svc.ensure_backup_cadences()
        _enabled_policy(tmp_path)
        svc.ensure_backup_cadences()
        assert _rows(BACKUP_ACTION)[0].state == "active"

    def test_no_policy_is_a_noop(self, sqlite_store, tmp_path):
        svc = _service(tmp_path, _Clock(T0))
        report = svc.ensure_backup_cadences()
        assert report["enabled"] is False
        assert _rows(BACKUP_ACTION) == []

    def test_invalid_policy_registers_nothing(self, sqlite_store, tmp_path):
        save_backup_policy(
            BackupPolicy(enabled=True, target_root=str(tmp_path / "b"), retention_count=0),
            state_dir=tmp_path,
        )
        svc = _service(tmp_path, _Clock(T0))
        report = svc.ensure_backup_cadences()
        assert report["errors"]
        assert _rows(BACKUP_ACTION) == []


class TestTickDispatch:
    def test_due_cadence_fires_the_skill(self, sqlite_store, tmp_path):
        _enabled_policy(tmp_path)
        registry, calls = _recording_registry()
        clock = _Clock(T0)
        svc = _service(tmp_path, clock, registry)
        svc.ensure_backup_cadences()

        # Nothing due yet at 01:00.
        report = svc.tick()
        assert report.fired == 0 and calls == []

        # Cross the 02:00 cron boundary.
        clock.advance(minutes=90)
        report = svc.tick()
        assert report.fired == 1
        assert calls == [BACKUP_ACTION]

        # Receipt lands in the fire log.
        with store.session_scope() as s:
            logs = s.query(ScheduleFireLog).all()
            outcomes = [(r.outcome, r.receipt_fragment_id) for r in logs]
        assert ("success", f"rcpt-{BACKUP_ACTION}") in outcomes

    def test_fire_advances_next_fire_at(self, sqlite_store, tmp_path):
        _enabled_policy(tmp_path)
        registry, calls = _recording_registry()
        clock = _Clock(T0)
        svc = _service(tmp_path, clock, registry)
        svc.ensure_backup_cadences()
        clock.advance(minutes=90)
        svc.tick()
        rows = _rows(BACKUP_ACTION)
        next_fire = rows[0].next_fire_at
        if next_fire.tzinfo is None:
            next_fire = next_fire.replace(tzinfo=UTC)
        assert next_fire > clock.t

    def test_misfire_run_once_if_overdue(self, sqlite_store, tmp_path):
        """Engine down for 3 days → exactly ONE catch-up fire, then future."""
        _enabled_policy(tmp_path)
        registry, calls = _recording_registry()
        clock = _Clock(T0)
        svc = _service(tmp_path, clock, registry)
        svc.ensure_backup_cadences()

        clock.advance(days=3)
        report = svc.tick()
        # Both cadences are overdue: each catches up exactly once.
        assert report.fired == 2
        assert calls.count(BACKUP_ACTION) == 1
        assert calls.count(VALIDATE_ACTION) == 1
        # A second tick right after fires nothing more — next_fire_at
        # jumped past the missed backlog (fire_once), not through it.
        svc.tick()
        assert calls.count(BACKUP_ACTION) == 1
        assert calls.count(VALIDATE_ACTION) == 1

    def test_failed_dispatch_dead_letters_and_alerts(self, sqlite_store, tmp_path, monkeypatch):
        events = _events(monkeypatch)
        _enabled_policy(tmp_path)
        registry, calls = _recording_registry(
            results={BACKUP_ACTION: SkillResult(ok=False, errors=["disk full"])}
        )
        clock = _Clock(T0)
        svc = _service(tmp_path, clock, registry)
        svc.attach_alerts()
        svc.ensure_backup_cadences()
        clock.advance(minutes=90)
        report = svc.tick()
        assert report.fired == 0
        with store.session_scope() as s:
            outcomes = {r.outcome for r in s.query(ScheduleFireLog).all()}
        assert "dead_letter" in outcomes
        assert any(intent == "data.dispatch.dead_letter" for intent, _ in events)

    def test_single_flight_across_two_engines(self, sqlite_store, tmp_path):
        """Two hosts sharing the store: the fire-log claim (unique
        constraint) makes each instant fire exactly once."""
        _enabled_policy(tmp_path)
        registry, calls = _recording_registry()
        clock = _Clock(T0)
        svc_a = _service(tmp_path, clock, registry, node_id="node-a")
        svc_b = _service(tmp_path, clock, registry, node_id="node-b")
        svc_a.ensure_backup_cadences()

        clock.advance(minutes=90)
        # Freeze the due instant: both engines see the same next_fire_at
        # bucket; only one claim can win.
        ra = svc_a.tick()
        rb = svc_b.tick()
        assert calls.count(BACKUP_ACTION) == 1
        assert ra.fired + rb.fired == 1


class TestValidateCadence:
    def test_validate_fires_on_its_own_schedule(self, sqlite_store, tmp_path):
        _enabled_policy(tmp_path)
        registry, calls = _recording_registry()
        clock = _Clock(T0)
        svc = _service(tmp_path, clock, registry)
        svc.ensure_backup_cadences()
        # 03:00 — past the 02:00 backup cron, before the 06:30 validate.
        clock.advance(hours=2)
        svc.tick()
        assert calls == [BACKUP_ACTION]
        # 07:00 — past the 06:30 validate cron.
        clock.advance(hours=4)
        svc.tick()
        assert calls == [BACKUP_ACTION, VALIDATE_ACTION]


class TestRunForever:
    def test_loop_is_deterministic_with_injected_sleep(self, sqlite_store, tmp_path):
        """No wall-clock: a fake sleep advances the synthetic clock and
        stops the loop after three intervals."""
        _enabled_policy(tmp_path)
        registry, calls = _recording_registry()
        clock = _Clock(T0)
        stop = threading.Event()
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.advance(hours=2)  # crosses 02:00 on the first interval
            if len(sleeps) >= 3:
                stop.set()

        svc = _service(
            tmp_path,
            clock,
            registry,
            tick_interval_seconds=7.5,
            sleep_fn=fake_sleep,
        )
        svc.run_forever(stop_event=stop)

        assert sleeps == [7.5, 7.5, 7.5]
        # Cadences got registered on startup and the due fire dispatched.
        assert len(_rows(BACKUP_ACTION)) == 1
        assert BACKUP_ACTION in calls

    def test_stop_method_sets_event(self, sqlite_store, tmp_path):
        svc = _service(tmp_path, _Clock(T0))
        svc.stop()
        svc.run_forever()  # exits immediately — would hang otherwise


class TestEnsureFreshnessCadence:
    """The sweep that closes "a partner stream went quiet and nothing said so".

    ACU sat four months stale while the DAQ core worked perfectly; the signal
    had no consumer. These hold the properties that make the consumer real.
    """

    def test_it_registers_without_anyone_enabling_it(self, sqlite_store, tmp_path):
        """Deliberately not opt-in. A check an operator has to remember to turn
        on is the original defect with an extra step."""
        svc = _service(tmp_path, _Clock(T0))
        report = svc.ensure_freshness_cadence()

        rows = _rows(FRESHNESS_ACTION)
        assert len(rows) == 1
        assert rows[0].state == "active"
        assert report[FRESHNESS_ACTION].startswith("registered:")

    def test_no_backup_policy_is_needed(self, sqlite_store, tmp_path):
        """Freshness is not part of the backup posture and must not inherit its
        gate — a node that never configured backups still needs this."""
        assert not (tmp_path / "plinth" / "backup_policy.toml").exists()
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_freshness_cadence()
        assert len(_rows(FRESHNESS_ACTION)) == 1

    def test_the_cadence_alerts_because_a_human_running_it_does_not(self, sqlite_store, tmp_path):
        """The asymmetry is the whole reason the envelope carries params:
        inspecting freshness must not page anyone, the sweep must."""
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_freshness_cadence()
        row = _rows(FRESHNESS_ACTION)[0]
        # The column is `capability_envelope`; there is no `envelope`. Checked
        # against the model rather than assumed, because a getattr with a
        # default over a wrong column name asserts nothing forever.
        assert row.capability_envelope["params"] == {"alert": True}
        assert row.capability_envelope["action"] == FRESHNESS_ACTION

    def test_it_runs_daily(self, sqlite_store, tmp_path):
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_freshness_cadence()
        row = _rows(FRESHNESS_ACTION)[0]
        assert row.cadence_kind == "cron"
        assert row.cadence_payload["cron_expr"] == DEFAULT_FRESHNESS_SCHEDULE

    def test_a_missed_sweep_runs_once_rather_than_catching_up(self, sqlite_store, tmp_path):
        """Replaying every missed daily sweep would emit a burst of alerts
        about one outage."""
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_freshness_cadence()
        assert _rows(FRESHNESS_ACTION)[0].misfire_policy == "fire_once"

    def test_idempotent(self, sqlite_store, tmp_path):
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_freshness_cadence()
        svc.ensure_freshness_cadence()
        svc.ensure_freshness_cadence()
        assert len(_rows(FRESHNESS_ACTION)) == 1

    def test_a_paused_sweep_is_resumed(self, sqlite_store, tmp_path):
        from axiom.extensions.builtins.schedule import api as pulse_api
        from axiom.extensions.builtins.schedule.api import ScheduleId

        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_freshness_cadence()
        pulse_api.pause(ScheduleId(_rows(FRESHNESS_ACTION)[0].id), "manual", now=T0)
        assert _rows(FRESHNESS_ACTION)[0].state == "paused"

        report = svc.ensure_freshness_cadence()
        assert _rows(FRESHNESS_ACTION)[0].state == "active"
        assert report[FRESHNESS_ACTION].startswith("resumed:")

    def test_a_broken_backup_policy_does_not_take_the_sweep_down(
        self, sqlite_store, tmp_path, monkeypatch
    ):
        """Two independent cadences. One failing to project is not a reason to
        stop watching partner data."""
        svc = _service(tmp_path, _Clock(T0))

        def boom(**kwargs):
            raise RuntimeError("backup policy is unreadable")

        monkeypatch.setattr(svc, "ensure_backup_cadences", boom)
        stop = threading.Event()
        stop.set()  # run_forever does its startup sync then exits immediately
        svc.run_forever(stop_event=stop)

        assert len(_rows(FRESHNESS_ACTION)) == 1, (
            "the freshness sweep was never registered because the backup policy failed first"
        )

    def test_the_startup_loop_registers_it(self, sqlite_store, tmp_path):
        svc = _service(tmp_path, _Clock(T0))
        stop = threading.Event()
        stop.set()
        svc.run_forever(stop_event=stop)
        assert len(_rows(FRESHNESS_ACTION)) == 1
def _connector(tmp_path: Path, name: str, **params: str) -> None:
    """Enroll a connector under tmp_path with optional refresh_* params."""
    save_connector(
        ConnectorConfig(
            name=name,
            kind="box",
            bronze_root=str(tmp_path / name),
            params={str(k): str(v) for k, v in params.items()},
        ),
        state_dir=tmp_path,
    )


def _refresh_by_connector() -> dict[str, ScheduleDefinition]:
    return {
        (r.capability_envelope or {}).get("params", {}).get("connector"): r
        for r in _rows(REFRESH_ACTION)
    }


class TestEnsureRefreshCadences:
    def test_enrolled_connectors_get_a_daily_cdc_cadence(self, sqlite_store, tmp_path):
        _connector(tmp_path, "netl-triga-ops")
        _connector(tmp_path, "dmsr")
        svc = _service(tmp_path, _Clock(T0))
        report = svc.ensure_refresh_cadences()
        by = _refresh_by_connector()
        assert set(by) == {"netl-triga-ops", "dmsr"}
        r = by["netl-triga-ops"]
        assert r.state == "active"
        assert r.action == REFRESH_ACTION
        assert r.cadence_kind == "cron" and r.cadence_payload["cron_expr"] == "0 0 * * *"  # @daily
        assert r.misfire_policy == "fire_once"
        assert report["cadences"]["netl-triga-ops"].startswith("registered:")

    def test_idempotent(self, sqlite_store, tmp_path):
        _connector(tmp_path, "netl-triga-ops")
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_refresh_cadences()
        svc.ensure_refresh_cadences()
        assert len(_rows(REFRESH_ACTION)) == 1

    def test_disabled_connector_gets_no_cadence(self, sqlite_store, tmp_path):
        _connector(tmp_path, "off-one", refresh_enabled="false")
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_refresh_cadences()
        assert _rows(REFRESH_ACTION) == []

    def test_removed_connector_is_paused(self, sqlite_store, tmp_path):
        _connector(tmp_path, "gone")
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_refresh_cadences()
        assert _refresh_by_connector()["gone"].state == "active"
        delete_connector("gone", state_dir=tmp_path)
        report = svc.ensure_refresh_cadences()
        assert _refresh_by_connector()["gone"].state == "paused"
        assert report["cadences"]["gone"].startswith("paused:")

    def test_custom_schedule_reschedules(self, sqlite_store, tmp_path):
        _connector(tmp_path, "one", refresh_schedule="@daily")
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_refresh_cadences()
        _connector(tmp_path, "one", refresh_schedule="0 * * * *")  # → hourly
        report = svc.ensure_refresh_cadences()
        assert len(_rows(REFRESH_ACTION)) == 1
        assert _refresh_by_connector()["one"].cadence_payload["cron_expr"] == "0 * * * *"
        assert report["cadences"]["one"].startswith("rescheduled:")

    def test_bad_schedule_reports_error_without_crashing(self, sqlite_store, tmp_path):
        _connector(tmp_path, "bad-one", refresh_schedule="not-a-cadence")
        svc = _service(tmp_path, _Clock(T0))
        report = svc.ensure_refresh_cadences()
        assert _rows(REFRESH_ACTION) == []
        assert any("bad-one" in e for e in report["errors"])


def _seed_fire(schedule_id: str, at: datetime, outcome: str = "success") -> None:
    import uuid

    with store.session_scope() as s:
        s.add(
            ScheduleFireLog(
                id=uuid.uuid4().hex,
                schedule_id=schedule_id,
                fire_time_bucket=int(at.timestamp()),
                params_hash="h",
                intended_fire_at=at,
                outcome=outcome,
                attempt=1,
            )
        )
        s.commit()


class TestRefreshFreshness:
    def test_recent_success_is_fresh_no_alert(self, sqlite_store, tmp_path, monkeypatch):
        events = _events(monkeypatch)
        _connector(tmp_path, "netl-triga-ops")
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_refresh_cadences()
        sid = _refresh_by_connector()["netl-triga-ops"].id
        _seed_fire(sid, T0 - timedelta(hours=1))
        report = svc.refresh_freshness_report()
        assert report["connectors"]["netl-triga-ops"]["stale"] is False
        assert not any(i == "data.refresh.stale" for i, _ in events)

    def test_no_success_within_window_alerts(self, sqlite_store, tmp_path, monkeypatch):
        events = _events(monkeypatch)
        _connector(tmp_path, "netl-triga-ops")
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_refresh_cadences()
        sid = _refresh_by_connector()["netl-triga-ops"].id
        _seed_fire(sid, T0 - timedelta(hours=72))  # older than the 48h window
        report = svc.refresh_freshness_report()
        assert report["connectors"]["netl-triga-ops"]["stale"] is True
        assert report["stale"] == ["netl-triga-ops"]
        assert any(i == "data.refresh.stale" for i, _ in events)

    def test_fresh_enrollment_is_in_grace(self, sqlite_store, tmp_path, monkeypatch):
        # No fires yet + a just-registered cadence → grace, not stale. Clock ~ now
        # so the cadence's wall-clock created_at aligns with the freshness math.
        events = _events(monkeypatch)
        _connector(tmp_path, "just-added")
        svc = _service(tmp_path, _Clock(datetime.now(UTC)))
        svc.ensure_refresh_cadences()
        report = svc.refresh_freshness_report()
        assert report["connectors"]["just-added"]["stale"] is False
        assert not any(i == "data.refresh.stale" for i, _ in events)

    def test_custom_window_shrinks_grace(self, sqlite_store, tmp_path, monkeypatch):
        events = _events(monkeypatch)
        _connector(tmp_path, "tight", freshness_window_hours="6")
        svc = _service(tmp_path, _Clock(T0))
        svc.ensure_refresh_cadences()
        sid = _refresh_by_connector()["tight"].id
        _seed_fire(sid, T0 - timedelta(hours=8))  # >6h → stale on the tight window
        report = svc.refresh_freshness_report()
        assert report["connectors"]["tight"]["stale"] is True
        assert report["connectors"]["tight"]["window_hours"] == 6.0
        # `events` was bound and never used, which ruff caught. The sibling
        # test above asserts the event does NOT fire; the missing half here is
        # that it DOES. Verified it actually fires before asserting it, rather
        # than deleting the binding and quietly dropping the check.
        assert any(i == "data.refresh.stale" for i, _ in events)
