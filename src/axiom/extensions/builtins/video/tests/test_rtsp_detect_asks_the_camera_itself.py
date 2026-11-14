# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``rtsp`` detect: an RTSP ``DESCRIBE`` with the resolved credential (spec-video §rtsp).

The rungs it reports are the ones diagnose climbs for this kind: the URL resolves,
``DESCRIBE`` answers, the login is accepted, the codec is one this ``ffmpeg`` can
decode. Each failing rung names its fix. Run against a fake camera that answers
Basic and Digest the way IP cameras do; no binaries.
"""

from __future__ import annotations

import socket

import pytest

from axiom.extensions.builtins.connector.detect import ConnectorState
from axiom.extensions.builtins.secrets import Secret
from axiom.extensions.builtins.video.sources.describe import describe, sdp_video_codec
from axiom.extensions.builtins.video.sources.rtsp import build

from .fake_rtsp import FakeCamera


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- DESCRIBE ----------------------------------------------------------------


@pytest.mark.parametrize("auth", ["basic", "digest"])
def test_describe_authenticates_and_returns_the_sdp(auth):
    with FakeCamera(users={"viewer": "pw"}, auth=auth) as cam:
        result = describe(cam.url("/t"), username="viewer", password="pw", timeout=5)
        assert result.status == 200 and result.ok
        assert sdp_video_codec(result.sdp) == "H264"
        assert [s.auth_scheme for s in cam.seen if s.method == "DESCRIBE"][-1].lower() == auth


@pytest.mark.parametrize("auth", ["basic", "digest"])
def test_describe_with_the_wrong_login_is_401(auth):
    with FakeCamera(users={"viewer": "pw"}, auth=auth) as cam:
        result = describe(cam.url("/t"), username="viewer", password="nope", timeout=5)
        assert result.status == 401 and result.auth_required


def test_describe_without_a_login_reports_the_challenge():
    with FakeCamera(users={"viewer": "pw"}, auth="digest") as cam:
        result = describe(cam.url("/t"), timeout=5)
        assert result.status == 401
        assert result.challenge and result.challenge.lower().startswith("digest")


def test_describe_an_unknown_path_is_404():
    with FakeCamera() as cam:
        assert describe(cam.url("/nope"), timeout=5).status == 404


def test_describe_a_closed_port_raises_oserror():
    with pytest.raises(OSError):
        describe(f"rtsp://127.0.0.1:{_free_port()}/t", timeout=2)


@pytest.mark.parametrize(
    ("sdp", "codec"),
    [
        ("m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n", "H264"),
        (
            "m=audio 0 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\nm=video 0 RTP/AVP 97\r\na=rtpmap:97 H265/90000\r\n",
            "H265",
        ),
        ("m=video 0 RTP/AVP 26\r\n", "JPEG"),  # static payload type 26 is JPEG (RFC 3551)
        ("m=audio 0 RTP/AVP 0\r\n", None),
    ],
)
def test_the_video_codec_is_read_from_the_sdp(sdp, codec):
    assert sdp_video_codec(sdp) == codec


# ---- detect ------------------------------------------------------------------


def _steps(result) -> dict[str, dict]:
    return {s["step"]: s for s in result.details["steps"]}


def test_detect_climbs_every_rung_when_the_camera_answers(fake_ffmpeg):
    with FakeCamera(users={"viewer": "pw"}, auth="digest") as cam:
        result = build({"url": cam.url("/t")}, Secret(value=b"viewer:pw")).detect()
    assert result.state is ConnectorState.CONFIGURED, result
    steps = _steps(result)
    assert list(steps) == ["url-resolves", "describe-answers", "auth-accepted", "codec-decodable"]
    assert all(s["ok"] for s in steps.values())


def test_detect_a_refused_login_names_the_vault(fake_ffmpeg):
    with FakeCamera(users={"viewer": "pw"}) as cam:
        result = build({"url": cam.url("/t")}, Secret(value=b"viewer:wrong")).detect()
    assert result.state is ConnectorState.BROKEN
    steps = _steps(result)
    assert steps["describe-answers"]["ok"] is True
    assert steps["auth-accepted"]["ok"] is False
    assert "vault set" in steps["auth-accepted"]["fix"]
    assert result.next_action == steps["auth-accepted"]["fix"]
    assert steps["codec-decodable"]["ok"] is None, "a rung after the first failure is skipped"
    assert "wrong" not in repr(result)


def test_detect_a_camera_that_wants_a_login_when_none_is_set(fake_ffmpeg):
    with FakeCamera(users={"viewer": "pw"}) as cam:
        result = build({"url": cam.url("/t")}).detect()
    fix = _steps(result)["auth-accepted"]["fix"]
    assert "credential-ref" in fix or "credential_ref" in fix


def test_detect_an_unknown_stream_path(fake_ffmpeg):
    with FakeCamera() as cam:
        result = build({"url": cam.url("/elsewhere")}).detect()
    step = _steps(result)["auth-accepted"]
    assert step["ok"] is False and "path" in step["fix"]


def test_detect_nothing_listening(fake_ffmpeg):
    result = build({"url": f"rtsp://127.0.0.1:{_free_port()}/t"}).detect()
    steps = _steps(result)
    assert steps["url-resolves"]["ok"] is True
    assert steps["describe-answers"]["ok"] is False
    assert "554" in steps["describe-answers"]["fix"] or "port" in steps["describe-answers"]["fix"]


def test_detect_a_host_that_does_not_resolve(fake_ffmpeg):
    result = build({"url": "rtsp://no-such-camera.invalid/t"}).detect()
    step = _steps(result)["url-resolves"]
    assert step["ok"] is False and "no-such-camera.invalid" in step["detail"]


def test_detect_a_codec_this_ffmpeg_cannot_decode(fake_ffmpeg, monkeypatch):
    monkeypatch.setenv("FAKE_VIDEO_DECODERS", "mjpeg")
    with FakeCamera(codec="H265") as cam:
        result = build({"url": cam.url("/t")}).detect()
    step = _steps(result)["codec-decodable"]
    assert step["ok"] is False and "H265" in step["detail"]


def test_detect_without_ffmpeg_still_reports_the_network_rungs(no_ffmpeg):
    with FakeCamera() as cam:
        result = build({"url": cam.url("/t")}).detect()
    steps = _steps(result)
    assert steps["describe-answers"]["ok"] is True
    assert steps["codec-decodable"]["ok"] is False
    assert "ffmpeg" in steps["codec-decodable"]["fix"]
