# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Fakes for `dev up` tests that must not start a process or open a real vault."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from axiom.extensions.builtins.dev import node, schema
from axiom.extensions.builtins.dev.skills import up
from axiom.extensions.builtins.lane.registry import Registry


class FakeVault:
    """An in-memory vault, so no test touches the person's keychain."""

    def __init__(self, *, fail_writes: str = "") -> None:
        self.values: dict[str, bytes] = {}
        self.meta: dict[str, dict[str, Any]] = {}
        self.rows: list[dict[str, Any]] = []
        self.fail_writes = fail_writes

    def list(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def exists(self, name: str) -> bool:
        return name in self.values

    def get(self, name: str):
        raw = self.values[name]

        class _S:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return None

            def as_str(self):
                return raw.decode()

        return _S()

    def set(self, name: str, value: bytes, **meta: Any) -> dict[str, Any]:
        if self.fail_writes:
            raise RuntimeError(self.fail_writes)
        self.values[name] = value
        self.meta[name] = dict(meta)
        return {"name": name}


class Ctx:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """`up.run` with everything that would start a process or open a vault replaced."""
    root = tmp_path / "checkout"
    root.mkdir()
    started: list[node.Plan] = []
    vault = FakeVault()

    monkeypatch.setattr(
        up,
        "resolve_lane",
        lambda params: (
            {
                "name": "lane1",
                "front": 8898,
                "api": 8899,
                "database": "axiom_dev_lane1",
                "dsn_var": "AXIOM_DB_URL",
                "isolated": True,
                "root": str(root),
            },
            [],
        ),
    )
    monkeypatch.setattr(
        schema,
        "sync",
        lambda *a, **k: {"created": False, "commit": "abc1234", "migrated": [], "extensions": {}},
    )
    monkeypatch.setattr(up, "open_vault", lambda ctx: vault)
    # No inherited sign-in settings, and no real binding: a test that wants a
    # bound site puts a fake one behind `signin.from_bound_site`.
    import os

    for key in [k for k in os.environ if k.startswith("AXIOM_GATE_OIDC_")]:
        monkeypatch.delenv(key)
    from axiom.extensions.builtins.dev import signin

    monkeypatch.setattr(signin, "from_bound_site", lambda **kw: (None, []))
    monkeypatch.setattr(signin, "bound_site_name", lambda: "")
    monkeypatch.setattr(node, "read_record", lambda home: None)
    # The "Running:" block reads the serving mode; never this machine's real binding.
    from axiom.infra import serving_mode

    monkeypatch.setattr(serving_mode, "resolve_mode", lambda: serving_mode.ServingMode("local"))

    def fake_start(plan: node.Plan, **kw: Any) -> int:
        started.append(plan)
        return 4242

    monkeypatch.setattr(node, "start", fake_start)
    monkeypatch.setattr(node, "wait_ready", lambda plan, pid, timeout=90.0: (True, ""))
    monkeypatch.setattr(node, "served_url", lambda url, timeout=5.0: url + "app/")
    monkeypatch.setattr(node, "remember_url", lambda home, url: None)
    return {"root": root, "started": started, "vault": vault, "ctx": Ctx(tmp_path / "state")}


def _git_checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


@pytest.fixture
def lanes(tmp_path, monkeypatch):
    f = tmp_path / "lanes.json"
    monkeypatch.setenv("AXIOM_LANES_FILE", str(f))
    return Registry(f)


@pytest.fixture
def stubbed_node(monkeypatch):
    started = []
    monkeypatch.setattr(up, "open_vault", lambda ctx: FakeVault())
    monkeypatch.setattr(node, "start", lambda plan, **kw: started.append(plan) or 4242)
    monkeypatch.setattr(node, "wait_ready", lambda plan, pid, timeout=90.0: (True, ""))
    monkeypatch.setattr(node, "served_url", lambda url, timeout=5.0: url)
    monkeypatch.setattr(node, "stop", lambda home: {"stopped": False, "reason": "stub"})
    monkeypatch.setattr(
        schema,
        "sync",
        lambda *a, **k: {"created": True, "commit": "abc", "migrated": [], "extensions": {}},
    )
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u:p@db.example:5432/shared")
    return started


@pytest.fixture(autouse=True)
def no_real_database_server(monkeypatch):
    """No unit test starts Docker or finds a server by itself."""
    from axiom.extensions.builtins.dev import server

    monkeypatch.delenv("AXIOM_PG_TEST_URL", raising=False)
    monkeypatch.setattr(server, "docker_path", lambda: None)
    monkeypatch.setattr(server, "state", lambda: "unavailable")


@pytest.fixture(autouse=True)
def no_real_sign_in_provider(monkeypatch):
    """`dev up` asks whether the sign-in provider answers (#1842); a test never
    reaches a network to find out. A test that needs "does not answer" passes
    its own ``fetch`` or patches this again."""
    from axiom.extensions.builtins.dev import signin

    monkeypatch.setattr(signin, "_fetch", lambda url: None)
