# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``video.diagnose``: a ladder whose first failing rung names its fix (VID-008).

spec-video §CLI: ``ffmpeg`` on PATH, the kind is registered, the kind's ``detect``
(for ``rtsp``: the URL resolves, ``DESCRIBE`` answers, the login is accepted, the
codec is decodable), then ``verify``. Every rung after the first failure is
reported as skipped rather than run against a source already known to be broken.
"""

from __future__ import annotations

import json
import socket

from axiom.extensions.builtins.video import connector as vc

from .fake_rtsp import FakeCamera


def _ladder(result) -> list[dict]:
    return result.value["steps"]


def _by_step(result) -> dict[str, dict]:
    return {s["step"]: s for s in _ladder(result)}


def _first_failure(result) -> dict:
    return next(s for s in _ladder(result) if s["ok"] is False)


def _declare(state_dir, url, **kw):
    vc.connect(
        vc.VideoConnector(name="cam-1", source={"kind": "rtsp", "url": url}, **kw),
        state_dir=state_dir,
    )


def test_every_rung_passes_for_a_working_camera(state_dir, call, fake_ffmpeg, login):
    with FakeCamera(users={login.username: login.password}, auth="digest") as cam:
        _declare(state_dir, cam.url("/t"), credential_ref=login.ref)
        result = call("video.diagnose", name="cam-1")
    assert result.ok, result.value
    assert [s["step"] for s in _ladder(result)] == [
        "ffmpeg",
        "numpy",
        "declared",
        "kind-registered",
        "credential",
        "url-resolves",
        "describe-answers",
        "auth-accepted",
        "codec-decodable",
        "verify",
    ]
    assert all(s["ok"] for s in _ladder(result))
    assert result.value["fix"] == ""


def test_missing_ffmpeg_is_the_first_rung_and_names_the_install(state_dir, call, no_ffmpeg):
    _declare(state_dir, "rtsp://h/ok")
    result = call("video.diagnose", name="cam-1")
    assert not result.ok
    failed = _first_failure(result)
    assert failed["step"] == "ffmpeg" and "install" in failed["fix"]
    assert result.value["fix"] == failed["fix"]
    assert {s["ok"] for s in _ladder(result)[1:]} == {None}, "the rest are skipped"


def test_an_undeclared_source_names_video_add(state_dir, call, fake_ffmpeg):
    failed = _first_failure(call("video.diagnose", name="ghost"))
    assert failed["step"] == "declared" and "instrument video add" in failed["fix"]


def test_an_unregistered_kind_names_the_known_kinds(state_dir, call, fake_ffmpeg):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import connectors_dir

    path = connectors_dir(state_dir=state_dir) / "cam-1.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[connector]\nname = "cam-1"\nkind = "video"\n\n[connector.params.source]\nkind = "pigeon"\n'
    )
    failed = _first_failure(call("video.diagnose", name="cam-1"))
    assert failed["step"] == "kind-registered" and "rtsp" in failed["fix"]


def test_a_credential_ref_that_does_not_resolve_names_the_vault(
    state_dir, call, fake_ffmpeg, monkeypatch
):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    _declare(state_dir, "rtsp://h/ok", credential_ref="env://NOT_SET_ANYWHERE")
    failed = _first_failure(call("video.diagnose", name="cam-1"))
    assert failed["step"] == "credential" and "vault set" in failed["fix"]


def test_nothing_answering_names_the_network(state_dir, call, fake_ffmpeg):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    _declare(state_dir, f"rtsp://127.0.0.1:{port}/t")
    failed = _first_failure(call("video.diagnose", name="cam-1"))
    assert failed["step"] == "describe-answers" and failed["fix"]


def test_a_rejected_login_names_the_vault_and_never_shows_the_login(
    state_dir, call, fake_ffmpeg, login
):
    with FakeCamera(users={login.username: "a-different-password"}) as cam:
        _declare(state_dir, cam.url("/t"), credential_ref=login.ref)
        result = call("video.diagnose", name="cam-1")
    failed = _first_failure(result)
    assert failed["step"] == "auth-accepted" and "vault set" in failed["fix"]
    assert login.password not in json.dumps(result.value)


def test_an_undecodable_codec_names_it(state_dir, call, fake_ffmpeg, monkeypatch):
    monkeypatch.setenv("FAKE_VIDEO_DECODERS", "mjpeg")
    with FakeCamera(codec="H265") as cam:
        _declare(state_dir, cam.url("/t"))
        failed = _first_failure(call("video.diagnose", name="cam-1"))
    assert failed["step"] == "codec-decodable" and "H265" in failed["detail"]


def test_the_last_rung_is_a_real_frame(state_dir, call, fake_ffmpeg):
    """DESCRIBE can pass and decoding still fail: verify is the rung that proves a frame."""
    with FakeCamera(paths=("/garbled",)) as cam:
        _declare(state_dir, cam.url("/garbled"))
        failed = _first_failure(call("video.diagnose", name="cam-1"))
    assert failed["step"] == "verify" and "codec" in failed["fix"].lower()


def test_diagnose_changes_nothing(state_dir, call, fake_ffmpeg):
    from axiom.extensions.builtins.video import state as vs

    _declare(state_dir, "rtsp://h/ok")
    before = sorted(p.relative_to(state_dir) for p in state_dir.rglob("*") if p.is_file())
    call("video.diagnose", name="cam-1")
    assert vs.read_verify_record("cam-1", state_dir) is None
    after = sorted(p.relative_to(state_dir) for p in state_dir.rglob("*") if p.is_file())
    assert [p for p in after if p not in before and p.parts[0] == "video"] == []
