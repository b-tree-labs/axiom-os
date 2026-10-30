# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``docker_available`` must never raise. It exists so tests can skip.

A skip guard that raises does the opposite of its job: instead of standing
the tests down when Docker cannot answer, it fails the run. That is what
happened on 2026-09-05 — the second probe was unguarded, its five-second
timeout lost to the load of the very suite it runs inside, and a pre-push
that should have skipped two tests blocked the push instead.

These run without Docker and without touching it: every probe is faked.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.federation_lifecycle import harness


def _fake_run(*, fail_on: str, exc: Exception):
    """Return a subprocess.run stand-in that raises for one command only."""

    def run(cmd, **kwargs):
        if fail_on in " ".join(cmd):
            raise exc
        return subprocess.CompletedProcess(cmd, 0, stdout="27.0.0\n", stderr="")

    return run


@pytest.mark.parametrize(
    "probe",
    ["docker version", "docker compose version"],
    ids=["daemon-probe", "compose-probe"],
)
@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(cmd="docker", timeout=5),
        OSError("docker vanished"),
    ],
    ids=["timeout", "oserror"],
)
def test_an_unresponsive_probe_returns_a_reason_rather_than_raising(
    monkeypatch, probe, failure
) -> None:
    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/local/bin/docker")
    monkeypatch.setattr(harness.subprocess, "run", _fake_run(fail_on=probe, exc=failure))
    ok, reason = harness.docker_available()
    assert ok is False
    assert reason, "a skip guard that stands tests down must say why"


def test_both_probes_answering_reports_available(monkeypatch) -> None:
    # The other half of the pin: the guard must not become "always skip".
    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/local/bin/docker")
    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="27.0.0\n", stderr=""),
    )
    assert harness.docker_available() == (True, "")


def test_a_missing_docker_cli_is_still_a_clean_skip(monkeypatch) -> None:
    monkeypatch.setattr(harness.shutil, "which", lambda _name: None)
    ok, reason = harness.docker_available()
    assert ok is False and "PATH" in reason
