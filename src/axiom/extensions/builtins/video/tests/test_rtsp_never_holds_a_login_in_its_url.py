# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The ``rtsp`` kind: a URL without userinfo plus a credential ref (VID-002).

A URL that carries a login is REFUSED, with the vault instruction, never scrubbed:
scrubbing would mean the password had already been typed into a file or a shell.
The credential reaches the ``ffmpeg`` input arguments only, percent-encoded, and
``redacted()`` (what ``status`` prints) never holds it.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.secrets import Secret
from axiom.extensions.builtins.video.ffmpeg import SECRET_INPUT
from axiom.extensions.builtins.video.sources import InvalidSourceConfig
from axiom.extensions.builtins.video.sources.rtsp import (
    PROTOCOLS,
    RtspSource,
    build,
    parse_credential,
)


def test_a_url_with_a_login_is_refused_with_the_vault_instruction():
    with pytest.raises(InvalidSourceConfig) as info:
        build({"url": "rtsp://admin:hunter2@cam.example.org:554/stream1"})
    text = f"{info.value} {info.value.fix}"
    assert "vault set" in text and "credential-ref" in text
    assert "hunter2" not in text, "the refusal must not repeat the password it refuses"


def test_a_url_with_only_a_username_is_refused_too():
    with pytest.raises(InvalidSourceConfig):
        build({"url": "rtsp://admin@cam.example.org/stream1"})


@pytest.mark.parametrize(
    "url",
    ["http://cam.example.org/stream", "cam.example.org/stream", "rtsp:///stream1", ""],
)
def test_a_url_that_is_not_an_rtsp_address_is_refused(url):
    with pytest.raises(InvalidSourceConfig):
        build({"url": url})


def test_transport_is_tcp_by_default_and_only_tcp_or_udp():
    assert build({"url": "rtsp://cam.example.org/s"}).transport == "tcp"
    assert build({"url": "rtsp://cam.example.org/s", "transport": "udp"}).transport == "udp"
    with pytest.raises(InvalidSourceConfig, match="transport"):
        build({"url": "rtsp://cam.example.org/s", "transport": "carrier"})


def test_the_kind_is_live_not_replayable():
    source = build({"url": "rtsp://cam.example.org/s"})
    assert source.kind == "rtsp" and source.replayable is False


def test_with_a_login_the_arguments_name_only_a_pipe():
    """ffmpeg takes a login only in the URL, and argv is readable by every user of the
    machine (``ps``), so the URL goes in an ffconcat script on an inherited pipe."""
    source = build({"url": "rtsp://cam.example.org:554/stream1"}, Secret(value=b"us@er:p:ss/w#rd"))
    args = source.ffmpeg_input_args()
    assert args == [
        "-f",
        "concat",
        "-safe",
        "0",
        "-protocol_whitelist",
        PROTOCOLS,
        "-i",
        SECRET_INPUT,
    ]
    assert (
        "p:ss" not in " ".join(args)
        and "us@er" not in " ".join(args)
        and "%3Ass" not in " ".join(args)
    )


def test_the_login_travels_in_the_pipe_percent_encoded_with_the_transport():
    source = build(
        {"url": "rtsp://cam.example.org:554/stream1", "transport": "udp"},
        Secret(value=b"us@er:p:ss/w#rd'q"),
    )
    script = source.ffmpeg_secret_input().decode()
    assert script.splitlines() == [
        "ffconcat version 1.0",
        "file 'rtsp://us%40er:p%3Ass%2Fw%23rd%27q@cam.example.org:554/stream1'",
        "option rtsp_transport udp",
    ]


def test_without_a_credential_the_input_has_no_userinfo():
    source = build({"url": "rtsp://cam.example.org/stream1", "transport": "udp"})
    assert source.ffmpeg_input_args() == [
        "-rtsp_transport",
        "udp",
        "-i",
        "rtsp://cam.example.org/stream1",
    ]
    assert source.ffmpeg_secret_input() is None, "no login, nothing to hide, no pipe"


def test_redacted_is_what_status_prints_and_holds_no_secret():
    source = build({"url": "rtsp://cam.example.org/stream1"}, Secret(value=b"viewer:s3cret-pw"))
    shown = source.redacted()
    assert shown["kind"] == "rtsp"
    assert shown["url"] == "rtsp://cam.example.org/stream1"
    assert shown["transport"] == "tcp"
    assert shown["authenticated"] is True
    dumped = json.dumps(shown) + repr(source) + str(source)
    assert "s3cret-pw" not in dumped and "viewer:" not in dumped


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"viewer:pw", ("viewer", "pw")),
        (b"viewer:pw:with:colons", ("viewer", "pw:with:colons")),
        (b'{"username": "viewer", "password": "pw"}', ("viewer", "pw")),
        (b"viewer:pw\n", ("viewer", "pw")),
    ],
)
def test_a_login_is_username_colon_password_or_json(raw, expected):
    assert parse_credential(Secret(value=raw)) == expected


@pytest.mark.parametrize("raw", [b"", b"no-colon-here", b'{"username": "viewer"}', b":pw"])
def test_a_malformed_login_is_refused_without_echoing_it(raw):
    with pytest.raises(InvalidSourceConfig) as info:
        parse_credential(Secret(value=raw))
    if raw:
        assert raw.decode() not in str(info.value)


def test_constructing_directly_refuses_userinfo_as_well():
    with pytest.raises(InvalidSourceConfig):
        RtspSource("rtsp://u:p@cam.example.org/s")
