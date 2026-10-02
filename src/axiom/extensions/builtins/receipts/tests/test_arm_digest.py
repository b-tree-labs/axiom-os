# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Arming the digest on its declared cadence.

The fifth link in the arrival chain. The other four are a composed message,
an honest delivery report, a declared audience, and a cadence string
somebody owns. This is the one that turns a message you can send into one
that recurs.

The sixth link is a host that actually fires it: PULSE's tick loop lives in
the ``data_platform_orchestrator`` service, so a cadence armed on a node
without that service is an inert row. These tests hold the verb to saying so,
and to naming a service that exists.
"""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.receipts.audience import Audience, save_audience
from axiom.extensions.builtins.receipts.skills.arm_digest import ACTION, NEEDS_A_HOST, run


@pytest.fixture()
def pulse():
    """A real PULSE store on SQLite, so registration is exercised rather
    than mocked — the point of this verb is that it writes a row."""
    from axiom.extensions.builtins.schedule import store as pulse_store
    from axiom.extensions.builtins.schedule.db_models import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def provider():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    pulse_store.set_provider(provider)
    try:
        yield pulse_store
    finally:
        pulse_store.reset_provider()
        engine.dispose()


def _rows(pulse):
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    with pulse.session_scope() as s:
        return [
            (r.action, r.state, r.cadence_kind, r.name) for r in s.query(ScheduleDefinition).all()
        ]


def test_nothing_declared_refuses_and_names_the_file(monkeypatch, tmp_path, pulse):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    result = run({})
    assert not result.ok
    assert "digest_audience" in result.errors[0]
    assert _rows(pulse) == [], "a refusal must not leave a half-armed row behind"


def test_a_malformed_declaration_refuses_with_every_problem(monkeypatch, tmp_path, pulse):
    from axiom.extensions.builtins.receipts.audience import audience_path

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    path = audience_path(state_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[digest_audience]\nrecipients = ["robin"]\nschedule = "every tuesday"\n',
        encoding="utf-8",
    )
    result = run({})
    assert not result.ok
    assert len(result.errors) == 2
    assert _rows(pulse) == []


def test_arming_registers_the_declared_cadence(monkeypatch, tmp_path, pulse):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    save_audience(Audience(recipients=("@robin",), schedule="0 7 * * *"), state_dir=tmp_path)
    result = run({})
    assert result.ok
    assert result.value["armed"] is True
    rows = _rows(pulse)
    assert len(rows) == 1
    action, state, kind, _name = rows[0]
    assert action == ACTION
    assert state == "active"
    assert kind == "cron"


def test_an_armed_result_says_nothing_will_fire_it(monkeypatch, tmp_path, pulse):
    """The verb registers a row in a table nothing reads. Reporting "armed"
    without that is the same false green the rest of this stage was fixed
    for — and it would be a worse one, because it is reassuring."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    save_audience(Audience(recipients=("@robin",)), state_dir=tmp_path)
    result = run({})
    assert any(NEEDS_A_HOST in a for a in result.actions_taken)


def test_the_host_the_sentence_names_actually_exists():
    """The armed result points somebody at a service by name. A sentence
    naming a host that has been renamed or removed is worse than none,
    because it sends them looking for something that is not there.

    This test is also a correction. Its predecessor asserted that NOTHING
    ticks the PULSE engine, which was wrong: `data_platform_orchestrator`
    declares the tick loop as a service with a server deployment profile.
    The predecessor failing on `data_platform/orchestration/service.py` is
    what caught it.
    """
    import tomllib
    from pathlib import Path

    from axiom.extensions.builtins.receipts.skills.arm_digest import PULSE_HOST

    root = Path(__file__).resolve()
    while not (root / "pyproject.toml").is_file():
        root = root.parent
    manifest = (
        root / "src/axiom/extensions/builtins/data_platform/axiom-extension.toml"
    ).read_text(encoding="utf-8")
    services = [
        b for b in tomllib.loads(manifest)["extension"]["provides"] if b.get("kind") == "service"
    ]
    named = [s for s in services if s.get("name") == PULSE_HOST]
    assert named, (
        f"NEEDS_A_HOST points at a service called {PULSE_HOST!r}, which no longer "
        f"exists. Declared services: {[s.get('name') for s in services]}"
    )
    assert named[0].get("deployment_profile") == "server", (
        "the sentence says deployment_profile = server; the manifest disagrees"
    )


def test_arming_twice_changes_nothing_the_second_time(monkeypatch, tmp_path, pulse):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    save_audience(Audience(recipients=("@robin",)), state_dir=tmp_path)
    run({})
    second = run({})
    assert second.ok
    assert len(_rows(pulse)) == 1, "arming twice registered two cadences"
    assert second.value["changed"] == []
    assert "nothing changed" in " ".join(second.actions_taken)


def test_a_disabled_audience_pauses_rather_than_cancels(monkeypatch, tmp_path, pulse):
    """Turning it off while away is not the same as deleting the cadence,
    and a cancelled schedule is terminal."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    save_audience(Audience(recipients=("@robin",)), state_dir=tmp_path)
    run({})
    save_audience(Audience(recipients=("@robin",), enabled=False), state_dir=tmp_path)
    result = run({})
    assert result.ok
    assert result.value["armed"] is False
    assert [state for _a, state, _k, _n in _rows(pulse)] == ["paused"]


def test_re_enabling_resumes_the_same_schedule(monkeypatch, tmp_path, pulse):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    save_audience(Audience(recipients=("@robin",)), state_dir=tmp_path)
    first = run({})
    save_audience(Audience(recipients=("@robin",), enabled=False), state_dir=tmp_path)
    run({})
    save_audience(Audience(recipients=("@robin",)), state_dir=tmp_path)
    again = run({})
    assert again.value["schedule_id"] == first.value["schedule_id"], (
        "re-enabling created a second cadence instead of resuming the first"
    )
    assert [state for _a, state, _k, _n in _rows(pulse)] == ["active"]


def test_dry_run_reports_without_registering(monkeypatch, tmp_path, pulse):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    save_audience(Audience(recipients=("@robin",), schedule="0 6 * * *"), state_dir=tmp_path)
    result = run({"dry_run": True})
    assert result.ok
    assert result.value["armed"] is False
    assert result.value["would_register"] == "0 6 * * *"
    assert _rows(pulse) == []


def test_the_verb_is_registered_and_declared():
    """A capability that exists and is not reachable is the defect class
    this extension has now produced twice."""
    import tomllib
    from pathlib import Path

    from axiom.extensions.builtins.receipts.skills import bind_default

    assert bind_default().has("receipts.arm_digest")

    manifest = Path(__file__).resolve().parent.parent / "axiom-extension.toml"
    declared = {
        block.get("name")
        for block in tomllib.loads(manifest.read_text(encoding="utf-8"))["extension"]["provides"]
    }
    assert "receipts.arm_digest" in declared
