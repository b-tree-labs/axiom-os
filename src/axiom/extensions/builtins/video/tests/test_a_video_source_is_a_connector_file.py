# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A video source is configuration in the connector registry (VID-001, ADR-240 D1, ADR-220 §2).

One file per source in the directory every connector lives in, ``kind = "video"``,
``credential_ref`` beside the common fields, and ``[params.source]`` holding what
the source kind reads. Everything outside ``[params.source]`` is common to every
kind. The platform's own connector listing still reads the file, so a video
source is a connector like any other, not a second registry.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
    connectors_dir,
)
from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
    list_connectors as platform_connectors,
)
from axiom.extensions.builtins.video import connector as vc
from axiom.extensions.builtins.video.sources import InvalidSourceConfig, UnknownSourceKind
from axiom.extensions.builtins.video.sources.rtsp import RtspSource

CAM = vc.VideoConnector(
    name="cam-1",
    credential_ref="env://CAM_1_LOGIN",
    fps_cap=5,
    source={"kind": "rtsp", "url": "rtsp://cam.example.org:554/stream1", "transport": "tcp"},
)


def test_it_lives_where_every_connector_lives(state_dir):
    path = vc.save(CAM, state_dir=state_dir)
    assert path == connectors_dir(state_dir=state_dir) / "cam-1.toml"
    assert vc.connector_path("cam-1", state_dir=state_dir) == path


def test_it_round_trips_with_its_source_table_intact(state_dir):
    vc.save(CAM, state_dir=state_dir)
    loaded = vc.load("cam-1", state_dir=state_dir)
    assert loaded == CAM
    assert loaded.source_kind == "rtsp"
    assert loaded.source_params() == {
        "url": "rtsp://cam.example.org:554/stream1",
        "transport": "tcp",
    }
    assert isinstance(loaded.fps_cap, int)


def test_the_ingest_listing_never_mistakes_a_camera_for_a_bronze_source(state_dir):
    """The data platform's connector listing builds ingest configs with a bronze root, and
    its callers (orchestrator, resolvers, archive reclaim) act on every row. A video source
    shares the directory and the envelope, and must not appear there: an empty bronze root
    resolves to the working directory."""
    vc.save(CAM, state_dir=state_dir)
    assert "cam-1" not in {c.name for c in platform_connectors(state_dir=state_dir)}


def test_a_name_is_unique_across_every_connector_kind(state_dir):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    save_connector(
        ConnectorConfig(name="cam-1", kind="box", bronze_root="/tmp/b"), state_dir=state_dir
    )
    with pytest.raises(FileExistsError):
        vc.save(CAM, state_dir=state_dir)


def test_the_file_says_kind_video_and_the_source_kind_separately(state_dir):
    text = vc.save(CAM, state_dir=state_dir).read_text()
    assert 'kind = "video"' in text
    assert 'kind = "rtsp"' in text
    assert "credential_ref" in text


def test_trigger_tables_are_kept_when_the_file_is_rewritten(state_dir):
    with_triggers = vc.VideoConnector(
        name="cam-2",
        source={"kind": "rtsp", "url": "rtsp://cam.example.org/s"},
        triggers=({"name": "motion", "severity": "info", "min_area": 0.02},),
    )
    vc.save(with_triggers, state_dir=state_dir)
    assert vc.load("cam-2", state_dir=state_dir).triggers == with_triggers.triggers


def test_only_video_connectors_are_listed(state_dir):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    save_connector(
        ConnectorConfig(name="docs", kind="box", bronze_root="/tmp/b"), state_dir=state_dir
    )
    vc.save(CAM, state_dir=state_dir)
    assert [c.name for c in vc.list_connectors(state_dir=state_dir)] == ["cam-1"]


def test_a_broken_file_does_not_hide_the_others(state_dir):
    vc.save(CAM, state_dir=state_dir)
    (connectors_dir(state_dir=state_dir) / "broken.toml").write_text("this is [not toml")
    assert [c.name for c in vc.list_connectors(state_dir=state_dir)] == ["cam-1"]


def test_loading_a_name_that_is_not_there(state_dir):
    with pytest.raises(vc.ConnectorNotFound, match="cam-9"):
        vc.load("cam-9", state_dir=state_dir)


def test_loading_a_connector_of_another_kind_is_refused(state_dir):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    save_connector(
        ConnectorConfig(name="docs", kind="box", bronze_root="/tmp/b"), state_dir=state_dir
    )
    with pytest.raises(vc.NotAVideoConnector):
        vc.load("docs", state_dir=state_dir)


def test_an_unknown_source_kind_fails_loudly(state_dir):
    bad = vc.VideoConnector(name="cam-3", source={"kind": "carrier-pigeon"})
    with pytest.raises(UnknownSourceKind, match="carrier-pigeon"):
        vc.validate(bad)
    with pytest.raises(UnknownSourceKind):
        vc.save(bad, state_dir=state_dir)


def test_a_source_with_no_kind_is_refused():
    with pytest.raises(InvalidSourceConfig, match="kind"):
        vc.validate(vc.VideoConnector(name="cam-4", source={"url": "rtsp://cam.example.org/s"}))


def test_a_url_with_a_login_can_never_be_saved(state_dir):
    bad = vc.VideoConnector(
        name="cam-5", source={"kind": "rtsp", "url": "rtsp://u:pw@cam.example.org/s"}
    )
    with pytest.raises(InvalidSourceConfig):
        vc.save(bad, state_dir=state_dir)
    assert not vc.connector_path("cam-5", state_dir=state_dir).exists()


@pytest.mark.parametrize("name", ["", "Cam 1", "../etc", "cam/1", "-cam", "x" * 65])
def test_a_name_must_be_safe_as_a_file_name(name):
    with pytest.raises(InvalidSourceConfig):
        vc.validate(vc.VideoConnector(name=name, source=dict(CAM.source)))


@pytest.mark.parametrize("fps_cap", [0, -1, 61, 2.5, "ten"])
def test_fps_cap_is_a_whole_number_of_frames_per_second(fps_cap):
    with pytest.raises(InvalidSourceConfig, match="fps_cap"):
        vc.validate(vc.VideoConnector(name="cam-6", source=dict(CAM.source), fps_cap=fps_cap))


def test_saving_over_an_existing_source_is_refused_unless_asked(state_dir):
    vc.save(CAM, state_dir=state_dir)
    with pytest.raises(FileExistsError):
        vc.save(CAM, state_dir=state_dir)
    vc.save(CAM, state_dir=state_dir, overwrite=True)


def test_the_credential_is_resolved_from_its_ref(login):
    secret = vc.resolve_credential(login.ref)
    assert secret is not None and secret.as_str() == f"{login.username}:{login.password}"
    assert vc.resolve_credential(None) is None


def test_build_source_dispatches_on_source_kind_with_the_credential(state_dir, login):
    cam = vc.VideoConnector(name="cam-7", credential_ref=login.ref, source=dict(CAM.source))
    source = vc.build_source(cam)
    assert isinstance(source, RtspSource)
    assert source.authenticated
    assert login.password not in " ".join(source.ffmpeg_input_args())
    assert login.password in source.ffmpeg_secret_input().decode()


def test_the_file_never_holds_the_password(state_dir, login):
    cam = vc.VideoConnector(name="cam-8", credential_ref=login.ref, source=dict(CAM.source))
    text = vc.save(cam, state_dir=state_dir).read_text()
    assert login.password not in text and login.username not in text


def test_delete_removes_the_file(state_dir):
    vc.save(CAM, state_dir=state_dir)
    assert vc.delete("cam-1", state_dir=state_dir) is True
    assert vc.delete("cam-1", state_dir=state_dir) is False
