# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every declared video source is listed by ``connector status``, with every other connector.

ADR-240 D1: "`axi connector status` lists it with every other connector". Run
as a person runs it, in a fresh process, where the in-memory store of delivery
outcomes is always empty, so the rows can only come from the video family's
status provider.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from axiom.extensions.builtins.video import connector as vc
from axiom.extensions.builtins.video.connector_status import rows

from .harness import PYTHON, child_env

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the stand-in tools are POSIX shell wrappers"
)


def _declare(state_dir, ref=None):
    vc.connect(
        vc.VideoConnector(
            name="cam-1", credential_ref=ref, source={"kind": "rtsp", "url": "rtsp://h/ok"}
        ),
        state_dir=state_dir,
    )


def _connector_status(state_dir) -> dict:
    done = subprocess.run(
        [PYTHON, "-m", "axiom.extensions.builtins.connector", "--json", "status"],
        env=child_env(state_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


def test_a_declared_source_is_listed_unmounted_until_it_verifies(state_dir):
    _declare(state_dir)
    (row,) = [
        r for r in _connector_status(state_dir)["value"]["configured"] if r["connector"] == "cam-1"
    ]
    assert row["kind"] == "video" and row["family"] == "video"
    assert row["health"] == "unmounted" and "verify" in row["detail"]


def test_a_verified_source_is_ready(state_dir, fake_ffmpeg):
    _declare(state_dir)
    assert vc.verify("cam-1", state_dir=state_dir).ok
    (row,) = rows(state_dir)
    assert row["health"] == "ready" and "rtsp" in row["detail"]


def test_the_listing_holds_no_credential(state_dir, login, fake_ffmpeg):
    vc.connect(
        vc.VideoConnector(
            name="cam-1",
            credential_ref=login.ref,
            source={"kind": "rtsp", "url": "rtsp://h/unauthorized"},
        ),
        state_dir=state_dir,
    )
    vc.verify("cam-1", state_dir=state_dir)
    body = _connector_status(state_dir)
    assert login.password not in json.dumps(body)
    (row,) = [r for r in body["value"]["configured"] if r["connector"] == "cam-1"]
    assert row["health"] == "failed"


def test_no_video_source_no_rows(state_dir):
    assert rows(state_dir) == []


def test_what_a_person_types_lists_it(state_dir):
    """`axi connector` resolves to the Registry Fabric's CLI (a known noun collision);
    the video source must show there too, not only through the connector.status skill."""
    _declare(state_dir)
    done = subprocess.run(
        [PYTHON, "-m", "axiom", "connector", "--json", "status"],
        env=child_env(state_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    (row,) = [r for r in json.loads(done.stdout)["configured"] if r["connector"] == "cam-1"]
    assert row["kind"] == "video" and row["health"] == "unmounted"
