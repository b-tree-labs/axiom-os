# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-015: a trigger is attached by configuration and ``connector reload``, not by a new verb.

The ``[[connector.params.triggers]]`` tables of a source's connector file are what
runs on it. The copy holding a source runs them from the moment it holds it (a
waiting copy runs none), and applies a change when ``connector reload`` asks: an
unchanged attachment keeps running, a changed one is replaced (its open episode
closed), a removed one is detached, and a change that does not build leaves the
previous one running and says why (ADR-220 §3). A trigger is built from its table
alone and never sees the source. One attachment per trigger per source.
"""

from __future__ import annotations

from typing import Any

import pytest

from axiom.extensions.builtins.video import connector as vc
from axiom.extensions.builtins.video import ledger, runner
from axiom.extensions.builtins.video import state as vs
from axiom.extensions.builtins.video import triggers as vt
from axiom.extensions.builtins.video.capture import SourceSupervisor
from axiom.extensions.builtins.video.connector import VideoConnector
from axiom.extensions.builtins.video.reload import reload_sources
from axiom.extensions.builtins.video.status import all_status

from .fakes import FOREVER, FakeDecoder, FakeFrameSource, dead_claim, registered_kind
from .harness import until
from .trigger_fakes import BUILT

SCRIPTED = "axiom.extensions.builtins.video.tests.trigger_fakes:ScriptedTrigger"


@pytest.fixture
def live(state_dir):
    with registered_kind(FakeFrameSource()) as kind:
        yield kind


@pytest.fixture
def catalog():
    BUILT.clear()
    return vt.TriggerCatalog(
        entries={
            "scripted": vt.TriggerEntry("scripted", SCRIPTED, "test"),
            "motion": vt.TriggerEntry(
                "motion", "axiom.extensions.builtins.video.motion:MotionTrigger", "builtin"
            ),
        }
    )


def _declare(state_dir, *triggers: dict[str, Any], **kw) -> VideoConnector:
    connector = VideoConnector(name="cam", source={"kind": "fake"}, triggers=tuple(triggers), **kw)
    vc.save(connector, state_dir=state_dir, overwrite=True)
    return connector


def _supervisor(state_dir, catalog, connector=None):
    sup = SourceSupervisor(
        connector or vc.load("cam", state_dir=state_dir),
        state_dir=state_dir,
        decoder=FakeDecoder(default=[FOREVER]),
        catalog=catalog,
        poll_s=0.01,
        status_interval_s=0.0,
    )
    sup.start()
    return sup


def _running(state_dir) -> dict[str, dict]:
    live = vs.read_live("cam", state_dir) or {}
    return {t["name"]: t for t in live.get("triggers") or [] if t.get("state") == "running"}


def _reload(state_dir) -> dict:
    nonce = vs.request_reload("cam", state_dir)
    return until(
        lambda: (
            (r := (vs.read_live("cam", state_dir) or {}).get("reload"))
            and r.get("request") == nonce
            and r
        )
    )


def test_triggers_in_the_file_run_once_capture_holds_the_source(state_dir, live, catalog):
    _declare(state_dir, {"name": "scripted"})
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: "scripted" in _running(state_dir))
        assert until(lambda: _running(state_dir)["scripted"]["frames"] > 0)
    finally:
        sup.stop()
    assert sup.bus.subscriptions() == []


def test_a_waiting_copy_runs_no_triggers(state_dir, live, catalog):
    _declare(state_dir, {"name": "scripted"})
    holder = _supervisor(state_dir, catalog)
    try:
        until(lambda: "scripted" in _running(state_dir))
        waiting = _supervisor(state_dir, catalog)
        try:
            until(lambda: waiting.state.value == "waiting")
            assert waiting.snapshot()["triggers"] == []
            assert len(BUILT) == 1
        finally:
            waiting.stop()
    finally:
        holder.stop()


def test_reload_attaches_a_trigger_to_the_running_capture(state_dir, live, catalog):
    _declare(state_dir)
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: (vs.read_live("cam", state_dir) or {}).get("state") == "running")
        assert _running(state_dir) == {}
        _declare(state_dir, {"name": "scripted"})
        ack = _reload(state_dir)
        assert ack["running"] == ["scripted"] and ack["errors"] == []
        until(lambda: "scripted" in _running(state_dir))
    finally:
        sup.stop()


def test_an_unchanged_attachment_survives_a_reload(state_dir, live, catalog):
    _declare(state_dir, {"name": "scripted"})
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: "scripted" in _running(state_dir))
        before = sup.triggers.runners["scripted"].trigger
        _reload(state_dir)
        assert sup.triggers.runners["scripted"].trigger is before
    finally:
        sup.stop()


def test_a_changed_attachment_is_replaced_and_its_open_episode_closed(state_dir, live, catalog):
    _declare(state_dir, {"name": "scripted", "script": {"1": [["started", "e1"]]}})
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: vs.read_open_episodes("cam", state_dir))
        before = sup.triggers.runners["scripted"].trigger
        _declare(state_dir, {"name": "scripted", "severity": "warning"})
        ack = _reload(state_dir)
        assert ack["errors"] == []
        assert sup.triggers.runners["scripted"].trigger is not before
        assert sup.triggers.runners["scripted"].config["severity"] == "warning"
    finally:
        sup.stop()
    kinds = [(r["kind"], r["detail"].get("reason")) for r in ledger.read(state_dir)]
    assert kinds == [("started", None), ("ended", runner.REASON_RECONFIGURED)]


def test_a_change_that_does_not_build_keeps_the_previous_attachment_running(
    state_dir, live, catalog
):
    _declare(state_dir, {"name": "scripted"})
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: "scripted" in _running(state_dir))
        before = sup.triggers.runners["scripted"].trigger
        _declare(state_dir, {"name": "scripted", "severity": "loud"})
        ack = _reload(state_dir)
        assert any("severity" in e for e in ack["errors"])
        assert sup.triggers.runners["scripted"].trigger is before
        assert _running(state_dir)["scripted"]["config"] == {"severity": "info"}
    finally:
        sup.stop()


def test_a_removed_attachment_is_detached_and_its_episode_closed(state_dir, live, catalog):
    _declare(state_dir, {"name": "scripted", "script": {"1": [["started", "e1"]]}})
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: vs.read_open_episodes("cam", state_dir))
        _declare(state_dir)
        ack = _reload(state_dir)
        assert ack["running"] == []
        assert "scripted" not in sup.triggers.runners
    finally:
        sup.stop()
    assert [r["detail"].get("reason") for r in ledger.read(state_dir)][-1] == runner.REASON_DETACHED


def test_one_attachment_per_trigger_per_source(state_dir, live, catalog):
    _declare(state_dir, {"name": "scripted"}, {"name": "scripted", "severity": "warning"})
    errors = runner.check_attachments(vc.load("cam", state_dir=state_dir).triggers, catalog)
    assert any("scripted" in e and "twice" in e for e in errors)


def test_a_trigger_is_built_from_its_table_and_never_sees_the_source(state_dir, live, login):
    seen = []

    def spy(config):
        seen.append(dict(config))
        from .trigger_fakes import ScriptedTrigger

        return ScriptedTrigger(config)

    catalog = vt.TriggerCatalog(entries={"spy": vt.TriggerEntry("spy", spy, "test")})
    _declare(state_dir, {"name": "spy", "severity": "warning"}, credential_ref=login.ref)
    sup = _supervisor(state_dir, catalog)
    try:
        # Built is not enough: the assertions below need the first frame to
        # have reached the trigger, which a loaded runner can take longer to do.
        until(lambda: seen and BUILT and BUILT[-1].seen)
    finally:
        sup.stop()
    assert seen == [{"severity": "warning"}]
    trigger = BUILT[-1] if BUILT else None
    assert trigger is not None and trigger.seen
    frame = trigger.seen[0]
    assert not hasattr(frame, "kind") and not hasattr(frame, "source")
    assert login.password not in repr(seen)


def test_stopping_capture_closes_the_episode_before_the_claim_is_let_go(state_dir, live, catalog):
    _declare(state_dir, {"name": "scripted", "script": {"1": [["started", "e1"]]}})
    sup = _supervisor(state_dir, catalog)
    until(lambda: vs.read_open_episodes("cam", state_dir))
    sup.stop()
    assert [r["kind"] for r in ledger.read(state_dir)] == ["started", "ended"]
    assert vs.read_open_episodes("cam", state_dir) == {}
    assert vs.claim_holder("cam", state_dir) is None


def test_taking_over_from_a_crashed_holder_closes_its_open_episode(state_dir, live, catalog):
    _declare(state_dir)
    dead_claim(vs.claim_item("cam", state_dir))
    started = vt.TriggerEvent(
        stream="cam",
        trigger="scripted@1.0.0",
        ts="2026-11-10T12:00:00+00:00",
        kind="started",
        incident_key="cam:scripted@1.0.0:e1",
        severity="warning",
    )
    ledger.append(started, state_dir)
    vs.write_open_episodes("cam", {started.incident_key: started.as_dict()}, state_dir)
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: len(ledger.read(state_dir)) == 2)
    finally:
        sup.stop()
    ended = ledger.read(state_dir)[1]
    assert (ended["kind"], ended["incident_key"], ended["severity"]) == (
        "ended",
        started.incident_key,
        "warning",
    )
    assert ended["detail"]["reason"] == runner.REASON_LOST


# ---- `connector reload`, as the video family answers it ----------------------------


def test_reload_of_a_source_nothing_captures_checks_it_and_says_when_it_applies(
    state_dir, live, catalog
):
    _declare(state_dir, {"name": "scripted"})
    (row,) = reload_sources(state_dir, "cam", catalog=catalog, wait_s=0)
    assert row["connector"] == "cam" and row["kind"] == "video"
    assert row["applied"] == "not-running" and row["triggers"] == ["scripted"]
    assert "axi instrument video start cam" in row["detail"]


def test_reload_of_a_broken_attachment_names_it_and_the_fix(state_dir, live, catalog):
    _declare(state_dir, {"name": "motoin"})
    (row,) = reload_sources(state_dir, "cam", catalog=catalog, wait_s=0)
    assert row["health"] == "failed"
    assert any("motoin" in e and "motion" in e for e in row["errors"])
    assert "axi instrument video list --triggers" in row["detail"]


def test_reload_waits_for_the_running_capture_to_apply_it(state_dir, live, catalog):
    _declare(state_dir)
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: (vs.read_live("cam", state_dir) or {}).get("state") == "running")
        _declare(state_dir, {"name": "scripted"})
        (row,) = reload_sources(state_dir, "cam", catalog=catalog, wait_s=5)
        assert row["applied"] == "running" and row["triggers"] == ["scripted"]
        assert row["errors"] == []
    finally:
        sup.stop()


def test_a_changed_source_setting_names_the_full_restart_command(state_dir, live, catalog):
    """A reload applies triggers; the source's own settings apply on a restart, and the note
    says so with the command a person types (ADR-256 D4)."""
    _declare(state_dir, {"name": "scripted"})
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: "scripted" in _running(state_dir))
        _declare(state_dir, {"name": "scripted"}, fps_cap=5)
        (row,) = reload_sources(state_dir, "cam", catalog=catalog, wait_s=5)
        assert "axi instrument video stop cam" in row["detail"], row
        assert "axi instrument video start cam" in row["detail"], row
    finally:
        sup.stop()


def test_a_trigger_table_without_a_name_says_where_the_names_are():
    with pytest.raises(vt.TriggerConfigError) as caught:
        vt.split_attachment({"severity": "info"})
    assert "axi instrument video list --triggers" in caught.value.fix


def test_reload_of_a_broken_change_while_running_is_degraded_not_failed(state_dir, live, catalog):
    _declare(state_dir, {"name": "scripted"})
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: "scripted" in _running(state_dir))
        _declare(state_dir, {"name": "scripted", "severity": "loud"})
        (row,) = reload_sources(state_dir, "cam", catalog=catalog, wait_s=5)
        assert row["health"] == "degraded" and "previous" in row["detail"]
    finally:
        sup.stop()


def test_reload_of_a_name_that_is_not_a_video_source_is_not_answered(state_dir, live, catalog):
    assert reload_sources(state_dir, "not-a-camera", catalog=catalog, wait_s=0) == []


def test_reload_without_a_name_answers_for_every_source(state_dir, live, catalog):
    _declare(state_dir)
    vc.save(VideoConnector(name="cam-2", source={"kind": "fake"}), state_dir=state_dir)
    rows = reload_sources(state_dir, catalog=catalog, wait_s=0)
    assert [r["connector"] for r in rows] == ["cam", "cam-2"]


def _ctx(state_dir):
    import logging

    from axiom.infra.skills import SkillContext, SkillRegistry

    return SkillContext(
        registry=SkillRegistry(), state_dir=state_dir, logger=logging.getLogger("test")
    )


class _NoStorage:
    """A storage manager with nothing configured, so only the families answer."""

    def reload(self, name=None):
        return {}

    def role_status(self):
        return {}


def test_connector_reload_reaches_the_video_family(state_dir, live):
    from axiom.extensions.builtins.connector.skills import reload as connector_reload

    _declare(state_dir, {"name": "motion"})
    result = connector_reload.run(
        {"connector": "cam", "manager": _NoStorage(), "no_stamp": True}, _ctx(state_dir)
    )
    assert result.ok, result.errors
    (row,) = result.value["configured"]
    assert (row["connector"], row["kind"], row["triggers"]) == ("cam", "video", ["motion"])


def test_connector_reload_of_a_name_nobody_has_is_an_error(state_dir, live):
    from axiom.extensions.builtins.connector.skills import reload as connector_reload

    result = connector_reload.run(
        {"connector": "ghost", "manager": _NoStorage(), "no_stamp": True}, _ctx(state_dir)
    )
    assert not result.ok and any("ghost" in e for e in result.errors)


def test_connector_reload_of_a_broken_attachment_fails(state_dir, live):
    from axiom.extensions.builtins.connector.skills import reload as connector_reload

    _declare(state_dir, {"name": "motion", "min_aera": 0.1})
    result = connector_reload.run(
        {"connector": "cam", "manager": _NoStorage(), "no_stamp": True}, _ctx(state_dir)
    )
    assert not result.ok and any("min_aera" in e for e in result.errors)


# ---- what status says about triggers ------------------------------------------------


def test_status_shows_each_running_triggers_health(state_dir, live, catalog):
    _declare(state_dir, {"name": "scripted"})
    vs.mark_started("cam", state_dir)
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: "scripted" in _running(state_dir))
        (row,) = all_status(state_dir=state_dir)
        (trigger,) = row["triggers"]
        assert trigger["name"] == "scripted" and trigger["state"] == "running"
        assert row["attached"] == ["scripted"]
    finally:
        sup.stop()


def test_status_of_a_source_nothing_captures_lists_what_is_attached(state_dir, live):
    _declare(state_dir, {"name": "motion"})
    (row,) = all_status(state_dir=state_dir)
    assert row["attached"] == ["motion"] and row["triggers"] == []


def test_a_failed_trigger_degrades_the_source_and_status_says_which(state_dir, live, catalog):
    _declare(state_dir, {"name": "scripted", "config_error_at": [0]})
    vs.mark_started("cam", state_dir)
    vs.write_verify_record(
        "cam",
        vs.VerifyRecord(ok=True, at="2026-11-10T12:00:00+00:00", report={"codec": "h264"}),
        state_dir,
    )
    sup = _supervisor(state_dir, catalog)
    try:
        until(
            lambda: any(
                t.get("state") == "failed"
                for t in (vs.read_live("cam", state_dir) or {}).get("triggers") or []
            )
        )
        (row,) = all_status(state_dir=state_dir)
        assert row["health"] == "degraded"
        assert "scripted" in row["note"] and "set a smaller region" in row["note"]
    finally:
        sup.stop()


def test_status_says_when_the_file_has_changed_and_not_been_reloaded(state_dir, live, catalog):
    _declare(state_dir, {"name": "scripted"})
    vs.mark_started("cam", state_dir)
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: "scripted" in _running(state_dir))
        _declare(state_dir, {"name": "scripted"}, {"name": "motion"})
        (row,) = all_status(state_dir=state_dir)
        assert "connector reload cam" in row["note"]
    finally:
        sup.stop()


def test_a_reload_with_any_broken_attachment_changes_nothing(state_dir, live, catalog):
    """A typo that renames a trigger (motion -> motoin) is not "detach motion, attach
    nothing": the reload is all or nothing, so what ran keeps running (ADR-220 §3)."""
    _declare(state_dir, {"name": "scripted"})
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: "scripted" in _running(state_dir))
        before = sup.triggers.runners["scripted"].trigger
        _declare(state_dir, {"name": "scriptd"}, {"name": "motion"})
        ack = _reload(state_dir)
        assert ack["applied"] is False and any("scriptd" in e for e in ack["errors"])
        assert ack["running"] == ["scripted"]
        assert sup.triggers.runners["scripted"].trigger is before
        assert "motion" not in sup.triggers.runners, "nothing of a broken reload is applied"
        (row,) = reload_sources(state_dir, "cam", catalog=catalog, wait_s=5)
        assert row["health"] == "degraded" and row["applied"] == "running"
        assert "not applied" in row["detail"] and "scripted" in row["detail"]
    finally:
        sup.stop()


def test_the_triggers_a_capture_starts_with_run_even_if_one_is_broken(state_dir, live, catalog):
    """At start there is nothing previous to keep, so what builds runs and the rest is failed."""
    _declare(state_dir, {"name": "scripted"}, {"name": "scriptd"})
    sup = _supervisor(state_dir, catalog)
    try:
        until(lambda: "scripted" in _running(state_dir))
        states = {t["name"]: t["state"] for t in vs.read_live("cam", state_dir)["triggers"]}
        assert states == {"scripted": "running", "scriptd": "failed"}
    finally:
        sup.stop()


class _StorageThatAnswersAnyName:
    """What the real storage manager does: any name it is asked about comes back, unmounted."""

    def __init__(self):
        self.asked = []

    def names(self):
        return []

    def reload(self, name=None):
        from types import SimpleNamespace

        self.asked.append(name)
        if name is None:
            return {}
        row = SimpleNamespace(name=name, health=None, reason="no longer configured")
        row.as_dict = lambda: {
            "name": name,
            "health": "unmounted",
            "kind": "",
            "reason": "no longer configured",
        }
        return {name: row}

    def role_status(self):
        from types import SimpleNamespace

        role = SimpleNamespace(
            as_dict=lambda: {
                "role": "archive",
                "connector": "default",
                "health": "unmounted",
                "note": "",
            }
        )
        return {"archive": role}


def test_reloading_a_video_source_says_nothing_about_storage(state_dir, live):
    from axiom.extensions.builtins.connector.skills import reload as connector_reload

    _declare(state_dir, {"name": "motion"})
    storage = _StorageThatAnswersAnyName()
    result = connector_reload.run(
        {"connector": "cam", "manager": storage, "no_stamp": True}, _ctx(state_dir)
    )
    assert result.ok, result.errors
    assert result.value["connectors"] == [], (
        "a video source is not a storage connector that went away"
    )
    assert result.value["roles"] == [], (
        "storage roles are not what a video source's reload is about"
    )
    assert storage.asked == []


def test_a_reload_is_answered_while_capture_waits_for_its_first_frame(state_dir, live, catalog):
    """A camera can take seconds to send its first frame (handshake, first keyframe: 9 s on a
    real one). A reload asked meanwhile is answered within about a second, not after it."""
    import threading

    from .fakes import FakeSession, frame_bytes

    first_frame = threading.Event()

    class SlowFirstFrame(FakeSession):
        def read(self, timeout):
            if not first_frame.wait(min(timeout, 8.0)):
                return None
            return frame_bytes(self.width, self.height, 1)

    class Decoder(FakeDecoder):
        def open(self, source, *, width, height, fps_cap):
            session = SlowFirstFrame(script=[], width=width, height=height, log=self.log)
            self.sessions.append(session)
            return session

    _declare(state_dir)
    sup = SourceSupervisor(
        vc.load("cam", state_dir=state_dir),
        state_dir=state_dir,
        decoder=Decoder(),
        catalog=catalog,
        poll_s=0.01,
        status_interval_s=0.0,
    )
    sup.start()
    try:
        until(lambda: sup.triggers is not None and sup.state.value == "connecting")
        _declare(state_dir, {"name": "scripted"})
        nonce = vs.request_reload("cam", state_dir)
        ack = until(
            lambda: (
                ((vs.read_live("cam", state_dir) or {}).get("reload") or {}).get("request") == nonce
            ),
            timeout=3,
        )
        assert ack and not first_frame.is_set()
    finally:
        first_frame.set()
        sup.stop()
