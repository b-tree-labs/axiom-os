# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The capture service: one supervisor thread per started source (spec-video §Design).

A source is captured while it is STARTED (a durable mark ``instrument video start`` sets and
``instrument video stop`` clears), so a new copy of the service brought up during a switch
knows what to take over without being told. Run for named sources (what ``video
start`` spawns) or for every started source (what a deploy runs). The bus of a
running source is where a trigger will subscribe (phase 2).
"""

from __future__ import annotations

import threading

from axiom.extensions.builtins.video import connector as vc
from axiom.extensions.builtins.video import state as vs
from axiom.extensions.builtins.video.services.capture import CaptureService

from .fakes import FOREVER, FakeDecoder
from .harness import wait_for


def _declare(state_dir, *names, started=()):
    for name in names:
        vc.connect(vc.VideoConnector(name=name, source={"kind": "fake"}), state_dir=state_dir)
    for name in started:
        vs.mark_started(name, state_dir)


def _service(state_dir, names=None):
    return CaptureService(
        names, state_dir=state_dir, decoder=FakeDecoder(default=[FOREVER]), watch_s=0.02
    )


def _state(svc, name):
    return (svc.status()["sources"].get(name) or {}).get("state")


def test_it_captures_the_named_source_and_reports_it(state_dir, fake_source):
    _declare(state_dir, "cam-1", started=["cam-1"])
    svc = _service(state_dir, ["cam-1"])
    svc.start()
    try:
        assert wait_for(
            lambda: (svc.status()["sources"]["cam-1"] or {}).get("frames", 0) > 3, timeout=10
        )
        health = svc.health_check()
        assert health["ok"] is True and health["sources"] == {"cam-1": "running"}
    finally:
        svc.stop(timeout=10)
    assert vs.claim_holder("cam-1", state_dir) is None
    assert svc.health_check()["ok"] is False, "a stopped service is not healthy"


def test_without_names_it_follows_the_started_marks(state_dir, fake_source):
    _declare(state_dir, "cam-1", "cam-2", started=["cam-1"])
    svc = _service(state_dir)
    svc.start()
    try:
        assert wait_for(lambda: _state(svc, "cam-1") == "running", timeout=10)
        assert "cam-2" not in svc.status()["sources"]

        vs.mark_started("cam-2", state_dir)
        assert wait_for(lambda: _state(svc, "cam-2") == "running", timeout=10)

        vs.clear_started("cam-1", state_dir)
        assert wait_for(lambda: _state(svc, "cam-1") in (None, "stopped"), timeout=10)
        # "stopped" is written while the claim is still held (the last word goes in before
        # the release), so the release follows it rather than coinciding with it.
        assert wait_for(lambda: vs.claim_holder("cam-1", state_dir) is None, timeout=10)
    finally:
        svc.stop(timeout=10)


def test_a_named_source_that_is_not_declared_is_reported_not_fatal(state_dir, fake_source):
    _declare(state_dir, "cam-1", started=["cam-1"])
    svc = _service(state_dir, ["cam-1", "ghost"])
    svc.start()
    try:
        assert wait_for(lambda: _state(svc, "cam-1") == "running", timeout=10)
        ghost = svc.status()["sources"]["ghost"]
        assert ghost["state"] == "failed" and "ghost" in ghost["error"]
    finally:
        svc.stop(timeout=10)


def test_a_trigger_can_subscribe_to_a_running_source(state_dir, fake_source):
    _declare(state_dir, "cam-1", started=["cam-1"])
    svc = _service(state_dir, ["cam-1"])
    svc.start()
    try:
        assert wait_for(lambda: svc.bus("cam-1") is not None, timeout=10)
        sub = svc.bus("cam-1").subscribe("probe")
        frame = sub.get(timeout=10)
        assert frame is not None and frame.stream == "cam-1"
    finally:
        svc.stop(timeout=10)


def test_run_forever_returns_once_its_named_sources_are_stopped(state_dir, fake_source):
    _declare(state_dir, "cam-1", started=["cam-1"])
    svc = _service(state_dir, ["cam-1"])
    code: list[int] = []
    thread = threading.Thread(
        target=lambda: code.append(svc.run_forever(install_signals=False)), daemon=True
    )
    thread.start()
    assert wait_for(lambda: _state(svc, "cam-1") == "running", timeout=10)
    vs.clear_started("cam-1", state_dir)
    thread.join(timeout=15)
    assert not thread.is_alive() and code == [0]
