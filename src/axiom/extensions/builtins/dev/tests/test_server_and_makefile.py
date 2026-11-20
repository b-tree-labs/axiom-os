# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The Makefile runs THIS checkout; the shared dev Postgres is started, found and never leaked."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from axiom.extensions.builtins.dev import server
from axiom.extensions.builtins.lane.registry import UNMANAGED, Registry

from .conftest import FakeVault

ROOT = Path(__file__).resolve().parents[6]


def test_make_axi_defaults_to_this_checkouts_source():
    out = subprocess.run(
        ["make", "-n", "dev-status"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    assert f"PYTHONPATH={ROOT}/src" in out and "-m axiom.axiom_cli dev status" in out
    # ...and that command line really imports this checkout's package, not an installed one.
    got = subprocess.run(
        [sys.executable, "-c", "import axiom; print(axiom.__file__)"],
        cwd="/",
        capture_output=True,
        text=True,
        env={"PYTHONPATH": f"{ROOT}/src", "PATH": ""},
    ).stdout.strip()
    assert got.startswith(str(ROOT / "src"))


class FakeDocker:
    """Scripted docker: records argv and env, answers inspect from a small state."""

    def __init__(self, container="absent", volume=False):
        self.container, self.volume, self.calls = container, volume, []

    def __call__(self, args, *, env=None, timeout=120):
        self.calls.append((list(args), dict(env or {})))
        if args[0] == "inspect":
            if self.container == "absent":
                return 1, "Error: No such object: " + server.NAME
            return 0, "true" if self.container == "running" else "false"
        if args[0] == "volume":
            return (0, "") if self.volume else (1, "No such volume")
        if args[0] == "run":
            self.container = "running"
        if args[0] == "start":
            self.container = "running"
        if args[0] == "stop":
            self.container = "stopped"
        return 0, ""


@pytest.fixture
def docker(monkeypatch):
    def make(**kw):
        fake = FakeDocker(**kw)
        monkeypatch.setattr(server, "docker", fake)
        monkeypatch.setattr(server, "docker_path", lambda: "/usr/bin/docker")
        monkeypatch.setattr(server, "state", lambda: _state(fake))
        monkeypatch.setattr(server, "_wait", lambda vault, port, timeout: None)
        return fake

    def _state(fake):
        return {"absent": "absent", "running": "running", "stopped": "stopped"}[fake.container]

    return make


@pytest.fixture
def reg(tmp_path):
    return Registry(tmp_path / "lanes.json")


def test_the_port_is_reserved_through_the_lane_registry_once(reg):
    port = server.reserved_port(reg)
    held = reg.get(server.LANE)
    assert held.front == port and held.dsn_var == UNMANAGED
    assert server.reserved_port(reg) == port and len(reg.all()) == 1


def test_up_creates_one_container_with_the_password_only_in_the_vault(docker, reg):
    fake, vault = docker(), FakeVault()
    vault.get = lambda name: _Secret(vault.values[name])
    info = server.up(vault, reg)
    assert info["created"] and info["state"] == "running"
    run = next(c for c in fake.calls if c[0][0] == "run")
    argv, env = run
    pw = vault.values[server.SECRET].decode()
    assert pw and pw not in " ".join(argv)  # never on a command line
    assert env["POSTGRES_PASSWORD"] == pw and "POSTGRES_PASSWORD" in argv
    assert f"{server.VOLUME}:/var/lib/postgresql/data" in argv and server.NAME in argv
    assert f"127.0.0.1:{reg.get(server.LANE).front}:5432" in argv
    assert pw not in repr(server.status(vault, reg)) and pw not in repr(info)


class _Secret:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def as_str(self):
        return self.raw.decode()


def test_up_is_idempotent_and_down_keeps_the_data(docker, reg):
    fake, vault = docker(), FakeVault()
    vault.get = lambda name: _Secret(vault.values[name])
    server.up(vault, reg)
    again = server.up(vault, reg)
    assert again == {
        "state": "running",
        "port": reg.get(server.LANE).front,
        "started": False,
        "created": False,
    }
    assert [c[0][0] for c in fake.calls].count("run") == 1
    assert server.down()["stopped"]
    assert not any(c[0][0] in ("rm", "volume") and "rm" in c[0] for c in fake.calls)
    assert server.up(vault, reg)["started"]  # a stopped container is started, not recreated
    assert [c[0][0] for c in fake.calls].count("run") == 1


def test_a_volume_without_its_password_is_refused_with_the_fix(docker, reg):
    docker(volume=True)
    with pytest.raises(server.ServerError, match="docker volume rm"):
        server.up(FakeVault(), reg)


def test_no_docker_says_how_to_get_a_server(monkeypatch, reg):
    monkeypatch.setattr(server, "state", lambda: "unavailable")
    with pytest.raises(server.ServerError, match="--db-url"):
        server.up(FakeVault(), reg)
    url, fixes = server.acquire(FakeVault(), reg)
    assert url == "" and len(fixes) == 1 and "Docker" in fixes[0] and "--db-url" in fixes[0]


def test_discovery_prefers_an_explicit_test_server_then_the_running_one(docker, reg, monkeypatch):
    monkeypatch.setenv("AXIOM_PG_TEST_URL", "postgresql://u:p@h:1/postgres")
    monkeypatch.setattr(server, "reachable", lambda u, timeout=3.0: True)
    assert server.acquire(FakeVault(), reg) == ("postgresql://u:p@h:1/postgres", [])
    monkeypatch.delenv("AXIOM_PG_TEST_URL")
    docker(container="running")
    vault = FakeVault()
    vault.values[server.SECRET] = b"pw"
    vault.get = lambda name: _Secret(vault.values[name])
    url, fixes = server.acquire(vault, reg)
    assert (
        fixes == []
        and url == f"postgresql://postgres:pw@127.0.0.1:{reg.get(server.LANE).front}/postgres"
    )


def test_discovery_starts_the_server_when_docker_is_there_and_says_so(docker, reg):
    fake, vault, said = docker(), FakeVault(), []
    vault.get = lambda name: _Secret(vault.values[name])
    url, fixes = server.acquire(vault, reg, say=said.append)
    assert fixes == [] and url.startswith("postgresql://postgres:") and "@127.0.0.1:" in url
    assert any("starting the dev database server" in s for s in said)
    assert any(c[0][0] == "run" for c in fake.calls)


def test_the_cli_has_the_db_server_verbs():
    from axiom.extensions.builtins.dev.cli import build_parser

    args = build_parser().parse_args(["db", "server", "status", "--json"])
    assert (args.verb, args.db_verb, args.server_verb, args.json_after) == (
        "db",
        "server",
        "status",
        True,
    )
    for verb in ("up", "down"):
        assert build_parser().parse_args(["db", "server", verb]).server_verb == verb
