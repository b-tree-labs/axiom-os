# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Fakes for `dev up` tests that must not start a process or open a real vault."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from axiom.extensions.builtins.dev import node
from axiom.extensions.builtins.dev.skills import up


class FakeVault:
    """An in-memory vault, so no test touches the person's keychain."""

    def __init__(self, *, fail_writes: str = "") -> None:
        self.values: dict[str, bytes] = {}
        self.fail_writes = fail_writes

    def list(self) -> list[dict[str, Any]]:
        return []

    def exists(self, name: str) -> bool:
        return name in self.values

    def set(self, name: str, value: bytes, **meta: Any) -> dict[str, Any]:
        if self.fail_writes:
            raise RuntimeError(self.fail_writes)
        self.values[name] = value
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
        up, "resolve_lane", lambda params: ({"name": "lane1", "front": 8899, "root": str(root)}, [])
    )
    monkeypatch.setattr(up, "open_vault", lambda ctx: vault)
    monkeypatch.setattr(node, "read_record", lambda home: None)

    def fake_start(plan: node.Plan, **kw: Any) -> int:
        started.append(plan)
        return 4242

    monkeypatch.setattr(node, "start", fake_start)
    monkeypatch.setattr(node, "wait_ready", lambda plan, pid, timeout=90.0: (True, ""))
    monkeypatch.setattr(node, "served_url", lambda url, timeout=5.0: url + "app/")
    monkeypatch.setattr(node, "remember_url", lambda home, url: None)
    return {"root": root, "started": started, "vault": vault, "ctx": Ctx(tmp_path / "state")}
