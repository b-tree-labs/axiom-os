# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The connector lifecycle, ADR-238 D5: detect, connect, verify, status, revoke (VID-003).

``verify`` asks the source for one frame and records codec, width, height, fps and
latency; until it has passed, the source is not active. Health follows ADR-220 §3:
``ready | degraded | failed | unmounted``.
"""

from __future__ import annotations

import json

from axiom.extensions.builtins.connector.detect import ConnectorState
from axiom.extensions.builtins.video import connector as vc
from axiom.extensions.builtins.video import state as vs

from .fakes import dead_claim

RTSP = {"kind": "rtsp", "url": "rtsp://cam.example.org/ok"}


def _declare(state_dir, name="cam-1", **kw):
    vc.connect(
        vc.VideoConnector(name=name, source={**RTSP, **kw.pop("source", {})}, **kw),
        state_dir=state_dir,
    )


def test_a_declared_source_that_was_never_verified_is_unmounted_and_inactive(state_dir):
    _declare(state_dir)
    health, reason = vc.health("cam-1", state_dir=state_dir)
    assert health == "unmounted" and "verify" in reason
    assert vc.is_active("cam-1", state_dir=state_dir) is False


def test_verify_records_what_the_source_reported(state_dir, fake_ffmpeg, monkeypatch):
    monkeypatch.setenv("FAKE_VIDEO_SIZE", "32x16")
    _declare(state_dir)
    record = vc.verify("cam-1", state_dir=state_dir)
    assert record.ok
    assert {k: record.report[k] for k in ("codec", "width", "height", "fps")} == {
        "codec": "h264",
        "width": 32,
        "height": 16,
        "fps": 10.0,
    }
    assert record.report["latency_ms"] >= 0
    assert vs.read_verify_record("cam-1", state_dir) == record
    assert vc.is_active("cam-1", state_dir=state_dir)
    assert vc.health("cam-1", state_dir=state_dir)[0] == "ready"


def test_a_failed_verify_leaves_the_source_failed_with_its_fix(state_dir, fake_ffmpeg):
    _declare(state_dir, source={"url": "rtsp://cam.example.org/unauthorized"})
    record = vc.verify("cam-1", state_dir=state_dir)
    assert not record.ok and record.code == "auth-failed" and "vault" in record.fix
    health, reason = vc.health("cam-1", state_dir=state_dir)
    assert health == "failed" and reason
    assert vc.is_active("cam-1", state_dir=state_dir) is False


def test_a_later_failure_takes_away_an_earlier_pass(state_dir, fake_ffmpeg):
    _declare(state_dir)
    assert vc.verify("cam-1", state_dir=state_dir).ok
    vc.save(
        vc.VideoConnector(name="cam-1", source={**RTSP, "url": "rtsp://cam.example.org/missing"}),
        state_dir=state_dir,
        overwrite=True,
    )
    assert not vc.verify("cam-1", state_dir=state_dir).ok
    assert vc.is_active("cam-1", state_dir=state_dir) is False


def test_verify_without_ffmpeg_fails_with_the_install_hint(state_dir, no_ffmpeg):
    _declare(state_dir)
    record = vc.verify("cam-1", state_dir=state_dir)
    assert not record.ok and record.code == "ffmpeg-missing" and "ffmpeg" in record.fix


def test_the_verify_record_never_holds_the_login(state_dir, fake_ffmpeg, login):
    _declare(
        state_dir, credential_ref=login.ref, source={"url": "rtsp://cam.example.org/unauthorized"}
    )
    vc.verify("cam-1", state_dir=state_dir)
    text = json.dumps(vs.read_verify_record("cam-1", state_dir).as_dict())
    assert login.password not in text and f"{login.username}:" not in text
    assert all(login.password not in p.read_text() for p in state_dir.rglob("*") if p.is_file())


def test_verify_of_an_undeclared_source(state_dir, fake_ffmpeg):
    record = vc.verify("ghost", state_dir=state_dir)
    assert not record.ok and record.code == "not-declared" and "instrument video add" in record.fix


def test_a_credential_ref_that_does_not_resolve_is_a_failed_verify(
    state_dir, fake_ffmpeg, monkeypatch
):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    _declare(state_dir, credential_ref="env://NOT_SET_ANYWHERE")
    record = vc.verify("cam-1", state_dir=state_dir)
    assert not record.ok and record.code == "credential-unresolved" and "vault set" in record.fix


def test_detect_is_delegated_to_the_kind(state_dir, fake_source):
    vc.connect(vc.VideoConnector(name="cam-1", source={"kind": "fake"}), state_dir=state_dir)
    assert vc.detect("cam-1", state_dir=state_dir).state is ConnectorState.CONFIGURED


def test_connect_will_not_replace_a_declared_source(state_dir):
    _declare(state_dir)
    try:
        _declare(state_dir)
    except FileExistsError as exc:
        assert "cam-1" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("connect replaced an existing source")


def test_revoke_removes_the_file_its_records_and_its_claim(state_dir, fake_ffmpeg):
    _declare(state_dir)
    vc.verify("cam-1", state_dir=state_dir)
    vs.mark_started("cam-1", state_dir)
    held_path = dead_claim(vs.claim_item("cam-1", state_dir))

    outcome = vc.revoke("cam-1", state_dir=state_dir)

    assert outcome["revoked"] is True
    assert not vc.connector_path("cam-1", state_dir=state_dir).exists()
    assert vs.read_verify_record("cam-1", state_dir) is None
    assert not vs.is_started("cam-1", state_dir)
    assert not held_path.exists(), "the claim is released"


def test_revoking_what_is_not_there_says_so(state_dir):
    assert vc.revoke("ghost", state_dir=state_dir)["revoked"] is False
