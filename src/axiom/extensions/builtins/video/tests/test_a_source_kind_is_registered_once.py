# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The source-kind registry: what ``params.source.kind`` dispatches on (VID-001, ADR-240 D1).

KindRegistry-shaped (ADR-110 §1): ``register(kind, factory)``, a duplicate is loud,
process-local default. A kind declares whether it is ephemeral or replayable, and
its factory takes ``(source_params, credential)``. Built-ins register at the
extension's import; ``rtsp`` is the first and only one.
"""

from __future__ import annotations

import pytest

import axiom.extensions.builtins.video  # noqa: F401 - built-ins register at import
from axiom.extensions.builtins.video.sources import (
    SourceKind,
    SourceParam,
    UnknownSourceKind,
    VideoSource,
    create_source,
    register_source_kind,
    source_kind,
    source_kinds,
    unregister_source_kind,
)

from .fakes import FakeFrameSource, registered_kind


def test_rtsp_is_registered_at_import_and_is_ephemeral():
    rtsp = source_kind("rtsp")
    assert rtsp.kind == "rtsp"
    assert rtsp.replayable is False


def test_rtsp_declares_the_flags_add_offers():
    params = {p.name: p for p in source_kind("rtsp").params}
    assert params["url"].required is True
    assert params["transport"].default == "tcp"
    assert set(params["transport"].choices) == {"tcp", "udp"}


def test_a_second_registration_under_a_live_kind_is_refused():
    with registered_kind(FakeFrameSource()):
        other = SourceKind(kind="fake", replayable=True, factory=lambda p, c: FakeFrameSource())
        with pytest.raises(ValueError, match="fake"):
            register_source_kind(other)


def test_re_registering_the_same_entry_is_harmless():
    """Import-time self-registration must survive a module being imported twice."""
    entry = source_kind("rtsp")
    register_source_kind(entry)
    assert source_kind("rtsp") is entry


def test_an_unknown_kind_fails_loudly_and_names_the_known_ones():
    with pytest.raises(UnknownSourceKind) as info:
        source_kind("carrier-pigeon")
    message = str(info.value)
    assert "carrier-pigeon" in message and "rtsp" in message
    assert isinstance(info.value, KeyError) and isinstance(info.value, ValueError)


def test_the_factory_gets_the_params_without_kind_and_the_credential():
    seen = {}

    def factory(params, credential):
        seen["params"], seen["credential"] = dict(params), credential
        return FakeFrameSource()

    register_source_kind(SourceKind(kind="probe", replayable=False, factory=factory))
    try:
        sentinel = object()
        built = create_source("probe", {"kind": "probe", "url": "x"}, sentinel)
        assert isinstance(built, VideoSource)
        assert seen == {"params": {"url": "x"}, "credential": sentinel}
    finally:
        unregister_source_kind("probe")


def test_kinds_are_listed_with_replayable():
    with registered_kind(FakeFrameSource(replayable=True), kind="recording"):
        listed = {k.kind: k.as_dict() for k in source_kinds()}
    assert listed["rtsp"]["replayable"] is False
    assert listed["recording"]["replayable"] is True
    assert [p["name"] for p in listed["rtsp"]["params"]][:1] == ["url"]


def test_a_kind_validates_its_params_without_a_credential():
    from axiom.extensions.builtins.video.sources import InvalidSourceConfig

    with pytest.raises(InvalidSourceConfig):
        source_kind("rtsp").validate({"url": "http://not-rtsp.example/"})
    source_kind("rtsp").validate({"url": "rtsp://cam.example.org:554/stream1"})


def test_a_source_param_needs_a_name():
    with pytest.raises(ValueError):
        SourceParam(name="", help="nothing")
