# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Only Docker can tell which port is free where Docker binds it.

`kit-up` used to probe for a free port itself and hand it to `docker run`. Under
Docker Desktop on Windows the probe runs in the Linux shell and the bind happens
on the Windows side, so a port that looked free failed with `Only one usage of
each socket address`, on every attempt. The sandbox now asks Docker to choose.
"""

from __future__ import annotations

import subprocess

from axiom.extensions.builtins.data_platform.kit import medallion


def test_the_sandbox_is_published_on_a_port_docker_chooses(monkeypatch, tmp_path):
    calls: list[tuple[str, ...]] = []

    def docker(*args, check=True):
        calls.append(args)
        out = "127.0.0.1:53697\n" if args[0] == "port" else ""
        return subprocess.CompletedProcess(["docker", *args], 0, out, "")

    class Project:
        state_dir = tmp_path / "state"
        tenant = "example"

    monkeypatch.setattr(medallion, "_docker", docker)
    monkeypatch.setattr(medallion, "_container", lambda _p: "sandbox")
    monkeypatch.setattr(medallion, "_state_file", lambda _p: tmp_path / "state" / "s.json")
    monkeypatch.setattr(medallion, "_wait", lambda _d: None)
    monkeypatch.setattr(medallion, "stamp", lambda _d: None)
    monkeypatch.setattr(medallion, "prepare", lambda _p, _d: [])
    monkeypatch.delenv("AXIOM_KIT_DSN", raising=False)

    out = medallion.up(Project())

    run = next(c for c in calls if c[0] == "run")
    assert run[run.index("-p") + 1] == "127.0.0.1::5432"
    assert ":53697/" in out["dsn"]


def test_the_published_port_survives_docker_listing_two_addresses(monkeypatch):
    listed = "127.0.0.1:53697\n[::1]:53697\n"
    monkeypatch.setattr(
        medallion,
        "_docker",
        lambda *a, check=True: subprocess.CompletedProcess(["docker", *a], 0, listed, ""),
    )

    assert medallion._published_port("sandbox") == 53697
