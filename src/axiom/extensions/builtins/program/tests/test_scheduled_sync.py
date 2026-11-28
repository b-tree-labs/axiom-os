# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The program schedules its own sync and consolidation (prd-program R28).

- the cadence rows land on PULSE, idempotently, and follow ``program.cadence``;
- a scheduled sync is single-flight (a held lock means a quiet skip) and a
  quiet no-op on a node without a program;
- a sync that changed something chains into the publisher only when the
  program switches it on, and a publish failure never fails the sync.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.program import cadence, freshness
from axiom.extensions.builtins.program import skills as program_skills
from axiom.extensions.builtins.program.skills import status as status_skill
from axiom.extensions.builtins.program.skills import sync
from axiom.extensions.builtins.schedule import store as pulse_store
from axiom.extensions.builtins.schedule.db_models import Base as PulseBase
from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

from .conftest import GENERIC_PROGRAM

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def pulse():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    PulseBase.metadata.create_all(engine)
    maker = sessionmaker(engine, future=True)

    @contextlib.contextmanager
    def provider():
        s = maker()
        try:
            yield s
        finally:
            s.close()

    pulse_store.set_provider(provider)
    yield provider
    pulse_store.reset_provider()
    engine.dispose()


def _rows(provider) -> dict[str, ScheduleDefinition]:
    with provider() as s:
        return {r.action: r for r in s.query(ScheduleDefinition).filter(ScheduleDefinition.state != "cancelled")}


def _node(tmp_path: Path, **program_extra: Any) -> Path:
    state = tmp_path / "node"
    (state / "program").mkdir(parents=True)
    raw = json.loads(json.dumps(GENERIC_PROGRAM))
    raw["program"].update(program_extra)
    (state / "program" / "data.json").write_text(json.dumps(raw), encoding="utf-8")
    return state


class TestCadence:
    def test_sync_and_consolidate_are_registered_once(self, pulse, tmp_path):
        state = _node(tmp_path)
        first = cadence.ensure_all(state, now=NOW)
        assert all(v.startswith("registered:") for v in first.values()), first
        again = cadence.ensure_all(state, now=NOW)
        assert all(v.startswith("unchanged:") for v in again.values()), again
        rows = _rows(pulse)
        sync_row = rows[cadence.SYNC_ACTION]
        assert sync_row.cadence_kind == "interval"
        assert sync_row.cadence_payload["interval_seconds"] == 15 * 60
        assert sync_row.capability_envelope["params"] == {"scheduled": True, "single_flight": True, "publish": True}
        assert rows[cadence.CONSOLIDATE_ACTION].cadence_kind == "cron"

    def test_a_changed_interval_reschedules_and_disabled_pauses(self, pulse, tmp_path):
        state = _node(tmp_path)
        cadence.ensure_all(state, now=NOW)
        data = state / "program" / "data.json"
        raw = json.loads(data.read_text())
        raw["program"]["cadence"] = {"sync_minutes": 5, "consolidate_enabled": False}
        data.write_text(json.dumps(raw))
        report = cadence.ensure_all(state, now=NOW)
        assert report[cadence.SYNC_ACTION].startswith("rescheduled:")
        assert report[cadence.CONSOLIDATE_ACTION].startswith("paused:")
        assert _rows(pulse)[cadence.SYNC_ACTION].cadence_payload["interval_seconds"] == 300

    def test_a_node_without_a_program_still_gets_quiet_rows(self, pulse, tmp_path):
        report = cadence.ensure_all(tmp_path / "empty", now=NOW)
        assert all(v.startswith("registered:") for v in report.values())

    def test_the_host_registers_the_declared_schedules_with_their_params(self, pulse, tmp_path, monkeypatch):
        from axiom.extensions.builtins.data_platform.services.orchestrator import (
            OrchestratorService,
        )

        state = _node(tmp_path)
        monkeypatch.setenv("AXI_STATE_DIR", str(state))  # program state here: the node uses it
        svc = OrchestratorService(registry=SkillRegistry(), state_dir=state, clock=lambda: NOW)
        report = svc.ensure_declared_schedules()
        assert {"program:program.sync", "program:program.consolidate", "program:program.followup_sweep"} <= set(
            report["registered"]
        ), report
        row = _rows(pulse)[cadence.SYNC_ACTION]
        assert row.extension == "program" and row.cadence_payload["interval_seconds"] == 900
        assert row.capability_envelope["params"] == cadence.SYNC_PARAMS
        again = svc.ensure_declared_schedules()
        assert "program:program.sync" in again["skipped"]

    def test_the_program_retunes_its_own_row(self, pulse, tmp_path, monkeypatch):
        """The host registered the declared default; program.cadence retunes it."""
        from axiom.extensions.builtins.data_platform.services.orchestrator import (
            OrchestratorService,
        )

        state = _node(tmp_path, cadence={"sync_minutes": 5})
        monkeypatch.setenv("AXI_STATE_DIR", str(state))
        OrchestratorService(registry=SkillRegistry(), state_dir=state, clock=lambda: NOW).ensure_declared_schedules()
        out = sync.run(dict(cadence.SYNC_PARAMS), _ctx(state))
        assert out.value["cadence"][cadence.SYNC_ACTION].startswith("rescheduled:")
        assert _rows(pulse)[cadence.SYNC_ACTION].cadence_payload["interval_seconds"] == 300

    def test_switched_off_its_rows_are_paused(self, pulse, tmp_path, monkeypatch):
        from axiom.extensions.builtins.data_platform.services.orchestrator import (
            OrchestratorService,
        )

        state = _node(tmp_path)
        monkeypatch.setenv("AXI_STATE_DIR", str(state))
        svc = OrchestratorService(registry=SkillRegistry(), state_dir=state, clock=lambda: NOW)
        svc.ensure_declared_schedules()
        node = tmp_path / "node.toml"
        node.write_text('[node]\nfunctions = []\nfeatures = []\noff = ["program"]\n', encoding="utf-8")
        monkeypatch.setenv("AXIOM_NODE_CONFIG", str(node))
        svc.ensure_declared_schedules()
        states = {r.action: r.state for r in _rows(pulse).values() if r.extension == "program"}
        assert states and set(states.values()) == {"paused"}, states

    def test_the_scheduler_fires_the_sync(self, pulse, tmp_path, monkeypatch):
        """End to end through the engine: the declared row fires program.sync."""
        from axiom.extensions.builtins.data_platform.services.orchestrator import (
            OrchestratorService,
        )

        state = _node(tmp_path)
        monkeypatch.setenv("AXI_STATE_DIR", str(state))
        registry = SkillRegistry()
        program_skills.bind(registry)
        clock = {"now": NOW}
        svc = OrchestratorService(registry=registry, state_dir=state, clock=lambda: clock["now"], node_id="n1")
        svc.ensure_declared_schedules()
        clock["now"] = NOW + timedelta(minutes=16)
        svc.tick()
        assert freshness.read_record(state).get("last_attempt_at")


def _ctx(state: Path, registry: SkillRegistry | None = None) -> SkillContext:
    return SkillContext(registry=registry or SkillRegistry(), state_dir=state, logger=logging.getLogger("t"), user_prompt=None)


class TestSingleFlight:
    def test_a_held_lock_means_a_quiet_skip(self, tmp_path):
        state = _node(tmp_path)
        ctx = _ctx(state)
        with sync.single_flight(sync.lock_path(ctx)) as taken:
            assert taken
            out = sync.run({"single_flight": True, "scheduled": True}, ctx)
        assert out.ok and out.value["skipped_run"] == "overlap"
        after = sync.run({"single_flight": True, "scheduled": True}, ctx)
        assert after.ok and "skipped_run" not in after.value

    def test_no_program_on_the_node_records_nothing(self, tmp_path):
        state = tmp_path / "empty"
        out = sync.run({"single_flight": True, "scheduled": True}, _ctx(state))
        assert out.ok and out.value["skipped_run"] == "no_program"
        assert freshness.read_record(state) == {}

    def test_an_unchanged_second_run_logs_nothing(self, tmp_path):
        ctx = _ctx(_node(tmp_path))
        sync.run({"single_flight": True, "scheduled": True}, ctx)
        again = sync.run({"single_flight": True, "scheduled": True}, ctx)
        assert again.value["count"] == 0 and again.actions_taken == []


class _Upstream:
    """A distinct source whose content differs from the node's file: a real change."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.origin = "github:example-org/tracker"

    def verify(self):
        from axiom.extensions.builtins.program.skills.sources import ConnectorReadiness

        return ConnectorReadiness(source=self.origin, reachable=True, authenticated=True, project_read=True)

    def load(self):
        from axiom.extensions.builtins.program.model import ProgramData

        return ProgramData(raw=self.raw)


class TestPublishAfterSync:
    def _setup(self, tmp_path, *, publish_cfg: dict[str, Any] | None, behaviour: str = "ok"):
        extra = {"publish": publish_cfg} if publish_cfg is not None else {}
        state = _node(tmp_path, **extra)
        calls: list[dict[str, Any]] = []

        def fake_publish(params, ctx):
            calls.append(params)
            if behaviour == "raise":
                raise RuntimeError("wiki unreachable")
            if behaviour == "fail":
                return SkillResult(ok=False, errors=["push rejected"])
            return SkillResult(ok=True, value={"committed": True})

        registry = SkillRegistry()
        registry.register_skill(SkillSpec(name="program.publish", fn=fake_publish, description="fake",
                                          inputs={}, side_effects=True, surfaces=("cli",), risk="reversible-write"))
        program_skills.bind(registry)
        ctx = _ctx(state, registry)
        sync.run({"single_flight": True, "scheduled": True}, ctx)  # baseline: snapshot exists
        changed = json.loads((state / "program" / "data.json").read_text())
        changed["schedule"][0]["pct"] = 55
        return state, ctx, calls, changed

    def _scheduled(self, ctx, upstream=None):
        params: dict[str, Any] = {"single_flight": True, "scheduled": True, "publish": True}
        if upstream is not None:
            params["_sources"] = [upstream]
        return sync.run(params, ctx)

    ON = {"wiki": {"repo": "example-org/tracker"}, "after_sync": True}

    def test_a_change_publishes(self, tmp_path):
        _, ctx, calls, changed = self._setup(tmp_path, publish_cfg=self.ON)
        out = self._scheduled(ctx, _Upstream(changed))
        assert out.ok and len(calls) == 1
        assert out.value["publish"]["ok"] is True and out.value["publish"]["skill"] == "program.publish"

    def test_no_change_does_not_publish(self, tmp_path):
        _, ctx, calls, _ = self._setup(tmp_path, publish_cfg=self.ON)
        out = self._scheduled(ctx)
        assert calls == [] and out.value["publish"] == {"ran": False, "reason": "nothing changed"}

    def test_no_target_does_not_publish(self, tmp_path):
        _, ctx, calls, changed = self._setup(tmp_path, publish_cfg={"after_sync": True})
        out = self._scheduled(ctx, _Upstream(changed))
        assert calls == [] and out.value["publish"]["ran"] is False

    def test_off_by_default(self, tmp_path):
        _, ctx, calls, changed = self._setup(tmp_path, publish_cfg={"wiki": {"repo": "example-org/tracker"}})
        out = self._scheduled(ctx, _Upstream(changed))
        assert calls == [] and "not enabled" in out.value["publish"]["reason"]

    @pytest.mark.parametrize("behaviour", ["fail", "raise"])
    def test_a_publish_error_leaves_the_sync_ok_and_is_visible(self, tmp_path, behaviour, monkeypatch):
        state, ctx, calls, changed = self._setup(tmp_path, publish_cfg=self.ON, behaviour=behaviour)
        out = self._scheduled(ctx, _Upstream(changed))
        assert out.ok and out.value["count"] > 0 and out.value["data_updated"]
        assert len(calls) == 1 and out.value["publish"]["ok"] is False
        # the change stuck: the sync is not rolled back
        assert json.loads((state / "program" / "data.json").read_text())["schedule"][0]["pct"] == 55
        # and the failure surfaces on every status read's freshness block
        cli_ctx = SkillContext(registry=ctx.registry, state_dir=state, logger=ctx.logger, surface="cli")
        monkeypatch.setattr(status_skill, "_followup_block", lambda c: {})
        read = status_skill.run({"scope": "freshness"}, cli_ctx)
        assert read.value["freshness"]["publish"]["ok"] is False
        assert read.value["freshness"]["publish"]["detail"]

    def test_the_publisher_is_configurable(self, tmp_path):
        state, ctx, calls, changed = self._setup(
            tmp_path, publish_cfg={**self.ON, "after_sync_skill": "example.publisher"}
        )
        seen: list[str] = []
        ctx.registry.register_skill(SkillSpec(
            name="example.publisher", fn=lambda p, c: (seen.append("x"), SkillResult(ok=True, value={}))[1],
            description="another publisher", inputs={}, side_effects=True, surfaces=("cli",), risk="reversible-write"))
        out = self._scheduled(ctx, _Upstream(changed))
        assert seen == ["x"] and calls == [] and out.value["publish"]["skill"] == "example.publisher"
