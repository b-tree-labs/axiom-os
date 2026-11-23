# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""doctor's Docker line proves the capability, not just that a binary exists.

Contributors hit "Docker is installed" while the daemon was off, or while a
container could not publish a port on loopback. The check starts a throwaway
container, asks where Docker published the port, and removes only what it made.
"""

from __future__ import annotations

import subprocess

from axiom.cli.doctor import CheckStatus, check_docker_capability, default_checks


class FakeDocker:
    """Scripted ``subprocess.run`` keyed on the docker verb."""

    def __init__(self, replies: dict[str, subprocess.CompletedProcess | Exception]):
        self.replies = replies
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        assert kw.get("timeout"), "every docker call must carry a timeout"
        reply = self.replies.get(cmd[1], subprocess.CompletedProcess(cmd, 0, "", ""))
        if isinstance(reply, Exception):
            raise reply
        return reply


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def _which(found=True):
    return lambda name: "/usr/bin/docker" if found and name == "docker" else None


def test_missing_docker_is_skipped_with_the_fix():
    r = check_docker_capability(which=_which(False), run=FakeDocker({}))
    assert r.status is CheckStatus.SKIPPED
    assert "not installed" in r.summary
    assert "AXIOM_KIT_DSN" in r.fix_hint and "Docker Desktop" in r.fix_hint


def test_daemon_down_reports_dockers_words_and_the_fix():
    run = FakeDocker({"info": _cp(1, "", "Cannot connect to the Docker daemon")})
    r = check_docker_capability(which=_which(), run=run)
    assert r.status is CheckStatus.WARNING
    assert "Cannot connect to the Docker daemon" in r.summary
    assert "Start Docker Desktop" in r.fix_hint
    assert not any(c[1] == "run" for c in run.calls)


def test_a_hung_daemon_does_not_hang_doctor():
    run = FakeDocker({"info": subprocess.TimeoutExpired("docker", 1)})
    r = check_docker_capability(which=_which(), run=run)
    assert r.status is CheckStatus.WARNING
    assert "did not answer" in r.summary


def test_ok_when_a_container_publishes_a_loopback_port_and_is_removed():
    run = FakeDocker({"run": _cp(0, "abc123\n"), "port": _cp(0, "127.0.0.1:49153\n")})
    r = check_docker_capability(which=_which(), run=run)
    assert r.status is CheckStatus.OK
    run_cmd = next(c for c in run.calls if c[1] == "run")
    assert "127.0.0.1::5432" in run_cmd and "--pull" in run_cmd and "never" in run_cmd
    rm = next(c for c in run.calls if c[1] == "rm")
    name = run_cmd[run_cmd.index("--name") + 1]
    assert rm[-1] == name  # removes exactly the container it named


def test_image_not_present_is_ok_but_does_not_pull():
    run = FakeDocker({"run": _cp(125, "", "Unable to find image 'postgres:16' locally")})
    r = check_docker_capability(which=_which(), run=run)
    assert r.status is CheckStatus.OK
    assert "image" in r.summary


def test_publish_failure_is_a_warning_with_dockers_words_and_cleans_up():
    run = FakeDocker({"run": _cp(125, "", "port is already allocated")})
    r = check_docker_capability(which=_which(), run=run)
    assert r.status is CheckStatus.WARNING
    assert "port is already allocated" in r.summary
    assert any(c[1] == "rm" for c in run.calls)


def test_published_somewhere_other_than_loopback_is_a_warning():
    run = FakeDocker({"run": _cp(0, "abc\n"), "port": _cp(0, "0.0.0.0:49153\n")})
    r = check_docker_capability(which=_which(), run=run)
    assert r.status is CheckStatus.WARNING


def test_it_is_registered_in_the_default_checks():
    assert any(c.fn is check_docker_capability for c in default_checks())
