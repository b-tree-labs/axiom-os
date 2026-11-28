# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A throwaway TimescaleDB starts on a port Docker chooses, and a refusal says why.

On 2026-11-20 every test in one module errored at setup on CI: ``docker run``
exited 125 and the error carried no reason, because the helper captured
Docker's output and dropped it. Two causes fit and the log could tell neither.

- **The port.** The helper bound port 0 to learn a free one, closed the
  socket, then handed the number to ``docker run``. Under ``pytest -n auto``
  anything else on the runner may take the port in between. Only Docker can
  choose a port that is free where Docker binds it (the kit sandbox learned the
  same, ``test_kit_up_lets_docker_choose_the_port``).
- **The image.** That module was the first on the runner to need the image, so
  the run also pulled it, anonymously; a registry that refuses for a moment
  fails the same way, in the same fraction of a second.

So the helper never picks a port, says what Docker said, and tries a refused
start again before giving up.

Acceptance rows (no spec owns this helper; the rows are named here):

- **TSDB-1** the container is published on ``127.0.0.1::5432`` and the DSN
  uses the port ``docker port`` reports.
- **TSDB-2** a refused ``docker run`` fails naming Docker's own words.
- **TSDB-3** a refused ``docker run`` is tried again, a bounded number of
  times; each refused container is removed, and a later success is used.
"""

from __future__ import annotations

import subprocess

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402

from axiom.extensions.builtins.data_platform.tests import timescale_container  # noqa: E402

REFUSED = "docker: Error response from daemon: toomanyrequests: You have reached your pull rate limit."


class _Docker:
    """Plays ``docker``: each ``run`` takes the next scripted (returncode, stderr)."""

    def __init__(self, runs: list[tuple[int, str]]):
        self.runs = list(runs)
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, check=False, capture_output=False, text=False, **_kw):
        argv = list(argv)
        self.calls.append(argv)
        out, err, rc = "", "", 0
        if argv[1] == "run":
            rc, err = self.runs.pop(0)
        elif argv[1] == "port":
            out = "127.0.0.1:53697\n[::1]:53697\n"
        if not text:
            out, err = out.encode(), err.encode()
        if check and rc:
            raise subprocess.CalledProcessError(rc, argv, out, err)
        return subprocess.CompletedProcess(argv, rc, out, err)

    def of(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if c[1] == verb]


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _sql):
        return None


@pytest.fixture
def docker(monkeypatch):
    def install(runs):
        fake = _Docker(runs)
        monkeypatch.setattr(timescale_container, "docker_available", lambda: True)
        monkeypatch.setattr(timescale_container.subprocess, "run", fake)
        monkeypatch.setattr(timescale_container.time, "sleep", lambda _s: None)
        monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _Conn())
        return fake

    return install


def test_tsdb_1_the_container_is_published_on_a_port_docker_chooses(docker):
    fake = docker([(0, "")])

    name, url = timescale_container.start()

    (run,) = fake.of("run")
    assert run[run.index("-p") + 1] == "127.0.0.1::5432"
    assert url == f"postgresql://postgres:{timescale_container.PASSWORD}@127.0.0.1:53697/postgres"
    assert fake.of("port") == [["docker", "port", name, "5432/tcp"]]


def test_tsdb_2_a_refused_start_fails_naming_what_docker_said(docker):
    docker([(125, REFUSED)] * 10)

    with pytest.raises(Exception) as caught:
        timescale_container.start()

    assert "toomanyrequests" in str(caught.value)


def test_tsdb_3_a_refused_start_is_tried_again_and_its_container_removed(docker):
    fake = docker([(125, REFUSED), (0, "")])

    name, url = timescale_container.start()

    refused, used = (r[r.index("--name") + 1] for r in fake.of("run"))
    assert used == name and refused != name
    assert ["docker", "rm", "-f", "-v", refused] in fake.calls
    assert url.endswith(":53697/postgres")


def test_tsdb_3_the_retries_are_bounded_and_leave_no_container(docker):
    fake = docker([(125, REFUSED)] * 10)

    with pytest.raises(Exception):
        timescale_container.start()

    names = [r[r.index("--name") + 1] for r in fake.of("run")]
    assert 1 < len(names) <= 5
    removed = {c[-1] for c in fake.of("rm")}
    assert set(names) <= removed
