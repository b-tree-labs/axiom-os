# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A status code is read from what the tool said, never from the source's address.

``ffmpeg`` echoes the input URL in its error. A port or host that happens to
contain ``401``, ``404`` or ``461`` (``rtsp://cam:54010/...``) must not turn a
codec failure into "the source refused the login". That misreading failed
``test_the_last_rung_is_a_real_frame`` on main whenever the fake camera drew
such a port (CI, 2026-11-24), and it would name the wrong fix for a real camera
on such a port.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.video.ffmpeg import classify_failure

_ADDRESSES = [
    "rtsp://127.0.0.1:54010/t",
    "rtsp://127.0.0.1:40412/t",
    "rtsp://127.0.0.1:44610/t",
    "rtsp://user:pw@10.40.1.4:8401/t",
    "rtsp://cam-401.example/t",
    "rtsp://127.0.0.1:461/stream404",
]


@pytest.mark.parametrize("url", _ADDRESSES)
def test_a_codec_failure_is_a_codec_failure_at_any_address(url):
    code, fix = classify_failure(f"{url}: Invalid data found when processing input")
    assert code == "codec-unsupported" and "codec" in fix.lower()


@pytest.mark.parametrize("url", _ADDRESSES)
def test_a_refused_connection_is_unreachable_at_any_address(url):
    host_port = url.split("//", 1)[1].split("/", 1)[0].rsplit("@", 1)[-1]
    said = f"[tcp @ 0x1] Connection to tcp://{host_port} failed: Connection refused\n{url}: Connection refused"
    assert classify_failure(said)[0] == "unreachable"


@pytest.mark.parametrize("url", _ADDRESSES)
def test_an_unrecognised_failure_stays_unrecognised_at_any_address(url):
    assert classify_failure(f"{url}: Immediate exit requested")[0] == "error"


def test_a_bare_host_and_port_is_an_address_too():
    assert (
        classify_failure("Connection to 127.0.0.1:4010 failed: Connection refused")[0]
        == "unreachable"
    )


@pytest.mark.parametrize(
    ("said", "code"),
    [
        (
            "rtsp://127.0.0.1:5540/t: Server returned 401 Unauthorized (authorization failed)",
            "auth-failed",
        ),
        ("[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized", "auth-failed"),
        ("rtsp://127.0.0.1:5540/t: Server returned 404 Not Found", "not-found"),
        ("[rtsp @ 0x1] method DESCRIBE failed: 404 Stream Not Found", "not-found"),
        ("[rtsp @ 0x1] method SETUP failed: 461 Unsupported transport", "transport-unsupported"),
        ("[rtsp @ 0x1] method SETUP failed: 461", "transport-unsupported"),
    ],
)
def test_the_codes_the_tool_reports_are_still_read(said, code):
    assert classify_failure(said)[0] == code
