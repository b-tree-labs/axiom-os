# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`ext install` snapshots the harness state before it replaces anything.

Runs against a throwaway state dir; the suite-wide AXIOM_SNAPSHOT_BEFORE=0 is
lifted only for these tests, after AXI_STATE_DIR points at a temp tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.cli.ext.commands.install import install_extension
from axiom.infra import backup_core as bc
from axiom.infra import state_snapshots as ss
from tests.cli.ext.test_install import _publish_fresh, axiom_home  # noqa: F401


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    s = tmp_path / "state"
    s.mkdir()
    (s / "settings.toml").write_text("a = 1\n")
    monkeypatch.setenv("AXI_STATE_DIR", str(s))
    monkeypatch.setenv("AXIOM_STATE_DIR", str(s))
    monkeypatch.setenv("AXIOM_SNAPSHOT_BEFORE", "1")
    return s


def test_install_snapshots_first(scaffolded_extension, axiom_home, state):  # noqa: F811
    _publish_fresh(scaffolded_extension, "greeter")
    messages: list[str] = []
    install_extension("greeter", no_pip=True, announce=messages.append)
    store = ss.open_store(state)
    ids = store.ids()
    assert len(ids) == 1
    body = store.read_manifest(ids[0])["body"]
    assert body["reason"] == "ext install greeter 0.1.0" and body["trigger"] == "before"
    assert any("state snapshot before 'ext install greeter 0.1.0'" in m for m in messages)


def test_a_dry_run_install_takes_no_snapshot(scaffolded_extension, axiom_home, state):  # noqa: F811
    _publish_fresh(scaffolded_extension, "greeter")
    install_extension("greeter", no_pip=True, dry_run=True)
    assert ss.open_store(state).ids() == []


def test_a_failed_snapshot_does_not_block_the_install(scaffolded_extension, axiom_home, state, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bc, "free_bytes", lambda p: (1, 10**12))
    _publish_fresh(scaffolded_extension, "greeter")
    messages: list[str] = []
    rec = install_extension("greeter", no_pip=True, announce=messages.append)
    assert rec.name == "greeter"
    assert any("COULD NOT take a state snapshot" in m for m in messages)
