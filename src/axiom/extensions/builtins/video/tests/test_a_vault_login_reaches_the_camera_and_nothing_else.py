# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The login path a person actually uses: ``vault set``, then ``--credential-ref foreign://…``.

The login is stored with the vault's own verb (value on stdin), named by
reference in the connector, accepted by a camera that demands Digest auth, and
found nowhere under the state directory except the vault's store.
"""

from __future__ import annotations

import base64
import os

import pytest

from .fake_rtsp import FakeCamera
from .harness import envelope, files_holding, run_cli, vault_set

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the stand-in tools are POSIX shell wrappers"
)

PASSWORD = "pw-from-the-vault-7f3a"


@pytest.fixture
def stored(state_dir, file_vault, fake_ffmpeg):
    vault_set(state_dir, "video-cam-1", f"viewer:{PASSWORD}")
    return "foreign://video-cam-1"


def test_a_vault_login_is_accepted_by_the_camera(state_dir, stored, fake_ffmpeg):
    with FakeCamera(users={"viewer": PASSWORD}, auth="digest") as cam:
        added = run_cli(
            [
                "add",
                "cam-1",
                "--kind",
                "rtsp",
                "--url",
                cam.url("/t"),
                "--credential-ref",
                stored,
                "--json",
            ],
            state_dir=state_dir,
        )
        assert added.returncode == 0, added.stdout + added.stderr
        ladder = envelope(run_cli(["diagnose", "cam-1", "--json"], state_dir=state_dir))
    steps = {s["step"]: s for s in ladder["value"]["steps"]}
    assert steps["credential"]["ok"] and stored in steps["credential"]["detail"]
    assert steps["auth-accepted"]["ok"], steps["auth-accepted"]
    assert not any(PASSWORD in " ".join(argv) for argv in fake_ffmpeg.argv()), "never in argv"
    assert any(PASSWORD in seen["url"] for seen in fake_ffmpeg.inputs()), "the login reached ffmpeg"


def test_the_login_is_nowhere_but_the_vault(state_dir, stored):
    with FakeCamera(users={"viewer": PASSWORD}, auth="digest") as cam:
        run_cli(
            ["add", "cam-1", "--kind", "rtsp", "--url", cam.url("/t"), "--credential-ref", stored],
            state_dir=state_dir,
        )
        status = run_cli(["status", "--json"], state_dir=state_dir)
        diagnose = run_cli(["diagnose", "cam-1", "--json"], state_dir=state_dir)
    assert PASSWORD not in status.stdout + status.stderr + diagnose.stdout + diagnose.stderr
    assert files_holding(state_dir, PASSWORD) == []
    # The control: the vault's dev store does hold it (base64, as that backend says: not encryption).
    stored_form = base64.b64encode(f"viewer:{PASSWORD}".encode()).decode()
    assert files_holding(state_dir, stored_form, skip=()), "the vault's own store holds it"


def test_a_vault_login_the_camera_refuses_names_vault_set(state_dir, stored):
    with FakeCamera(users={"viewer": "a-different-password"}, auth="digest") as cam:
        run_cli(
            ["add", "cam-1", "--kind", "rtsp", "--url", cam.url("/t"), "--credential-ref", stored],
            state_dir=state_dir,
        )
        ladder = envelope(run_cli(["diagnose", "cam-1", "--json"], state_dir=state_dir))
    failed = next(s for s in ladder["value"]["steps"] if s["ok"] is False)
    assert failed["step"] == "auth-accepted" and "vault set" in failed["fix"]
    assert PASSWORD not in repr(ladder)


def test_a_ref_to_a_name_nobody_stored_fails_at_the_credential_rung(
    state_dir, file_vault, fake_ffmpeg
):
    run_cli(
        [
            "add",
            "cam-1",
            "--kind",
            "rtsp",
            "--url",
            "rtsp://cam.example.org/ok",
            "--credential-ref",
            "foreign://never-set",
        ],
        state_dir=state_dir,
    )
    failed = next(
        s
        for s in envelope(run_cli(["diagnose", "cam-1", "--json"], state_dir=state_dir))["value"][
            "steps"
        ]
        if s["ok"] is False
    )
    assert failed["step"] == "credential" and "vault set" in failed["fix"]
