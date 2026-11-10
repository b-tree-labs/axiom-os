# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""When Docker refuses, the person reads Docker's own words, not a traceback.

A contributor's first `kit-up` ended in a Python traceback and `exit status 125`.
Exit 125 only says Docker itself failed; the reason was in stderr, captured and
then thrown away, so nobody could tell a blocked image pull from a stopped daemon.
"""

from __future__ import annotations

import subprocess

import pytest

from axiom.extensions.builtins.data_platform.kit import medallion
from axiom.extensions.builtins.data_platform.kit.medallion import KitError


def _refusing_run(stderr: str):
    def run(cmd, **kwargs):
        raise subprocess.CalledProcessError(125, cmd, output="", stderr=stderr)

    return run


@pytest.fixture(autouse=True)
def _docker_on_path(monkeypatch):
    monkeypatch.setattr(medallion.shutil, "which", lambda _name: "/usr/bin/docker")


def test_a_refused_docker_command_reports_what_docker_said(monkeypatch):
    said = "docker: Error response from daemon: Get https://registry-1.docker.io/v2/: net/http: TLS handshake timeout."
    monkeypatch.setattr(medallion.subprocess, "run", _refusing_run(said))

    with pytest.raises(KitError) as caught:
        medallion._docker("run", "-d", "--name", "x", "-e", "POSTGRES_PASSWORD=secret-value", "postgres:16")

    message = str(caught.value)
    assert "TLS handshake timeout" in message
    assert "exit status 125" in message or "125" in message


def test_the_message_offers_the_way_around_docker(monkeypatch):
    monkeypatch.setattr(medallion.subprocess, "run", _refusing_run("boom"))

    with pytest.raises(KitError) as caught:
        medallion._docker("run", "postgres:16")

    assert "AXIOM_KIT_DSN" in str(caught.value)


def test_the_message_does_not_repeat_the_command_line(monkeypatch):
    monkeypatch.setattr(medallion.subprocess, "run", _refusing_run("boom"))

    with pytest.raises(KitError) as caught:
        medallion._docker("run", "-e", "POSTGRES_PASSWORD=secret-value", "postgres:16")

    assert "secret-value" not in str(caught.value)


def test_a_silent_refusal_still_says_something_useful(monkeypatch):
    monkeypatch.setattr(medallion.subprocess, "run", _refusing_run(""))

    with pytest.raises(KitError) as caught:
        medallion._docker("run", "postgres:16")

    assert "docker info" in str(caught.value)


def test_check_false_still_returns_the_result(monkeypatch):
    done = subprocess.CompletedProcess(["docker", "rm"], 1, "", "no such container")
    monkeypatch.setattr(medallion.subprocess, "run", lambda cmd, **kwargs: done)

    assert medallion._docker("rm", "-f", "-v", "x", check=False) is done
