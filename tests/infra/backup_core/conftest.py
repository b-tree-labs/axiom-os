# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Sandbox for the backup-core and restore-drill tests.

Every test runs with the state dir, HOME, AXI_HOME, the file secrets backend and the action
ledger pointed into ``tmp_path``.  :func:`assert_sandboxed` runs FIRST (autouse) and fails the
test if any location the code under test resolves is outside the temp tree, is the real home,
or if a database URL is set; the keychain runner raises.  ``test_the_sandbox_guard_*`` in
``test_sandbox_guard.py`` prove the guard fires.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_REAL_HOME = Path(os.path.expanduser("~")).resolve()

#: Fabricated canaries: they exist only to be searched for in output.
CANARY_A = "CANARY-bc-alpha-3c91d2e07f0b84a6"
CANARY_B = "CANARY-bc-bravo-a9c4417de3502f80"
PASSPHRASE = "drill passphrase, protects nothing 77"


class SandboxEscape(AssertionError):
    pass


def assert_sandboxed(tmp: Path) -> None:
    """Fail unless every credential- or state-bearing location is inside ``tmp``."""
    from axiom.infra.paths import get_user_state_dir

    tmp = tmp.resolve()
    places = {
        "state dir": get_user_state_dir(),
        "HOME": Path(os.environ["HOME"]),
        "AXI_HOME": Path(os.environ["AXI_HOME"]),
        "file secrets": Path(os.environ["AXIOM_FILE_SECRETS_PATH"]),
    }
    for label, p in places.items():
        rp = p.resolve()
        if tmp != rp and tmp not in rp.parents:
            raise SandboxEscape(f"{label} resolves outside the sandbox: {rp}")
        if rp == _REAL_HOME:
            raise SandboxEscape(f"{label} is the real home: {rp}")
    if os.environ.get("AXIOM_FOREIGN_SECRETS_BACKEND") != "file":
        raise SandboxEscape("foreign secrets backend is not the file backend")
    if os.environ.get("AXIOM_ACTION_LEDGER_BACKEND") != "jsonl":
        raise SandboxEscape("action ledger backend is not jsonl")
    if os.environ.get("AXIOM_DB_URL"):
        raise SandboxEscape("AXIOM_DB_URL is set; a real database must never be reached")


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    home, state, axi_home = tmp_path / "home", tmp_path / "state", tmp_path / "axi_home"
    for d in (home, state, axi_home):
        d.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AXI_HOME", str(axi_home))
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.setenv("AXIOM_STATE_DIR", str(state))
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    monkeypatch.setenv("AXIOM_FILE_SECRETS_PATH", str(tmp_path / "file-secrets.json"))
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setenv("AXIOM_SNAPSHOT_BEFORE", "0")
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)

    from axiom.extensions.builtins.secrets.providers import keychain

    def _no_keychain(*a, **k):
        raise SandboxEscape("a test reached the macOS `security` binary (the real keychain)")

    monkeypatch.setattr(keychain, "_subprocess_runner", _no_keychain)
    assert_sandboxed(tmp_path)
    return tmp_path


@pytest.fixture
def state_dir(sandbox) -> Path:
    return sandbox / "state"


@pytest.fixture
def fast_kdf(monkeypatch):
    from axiom.infra.vault_backup import crypto

    monkeypatch.setattr(crypto, "DEFAULT_KDF", {"n": 2**10, "r": 8, "p": 1})
