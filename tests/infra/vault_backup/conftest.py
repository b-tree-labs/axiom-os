# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Sandbox for the vault-backup tests: credentials are involved, so the
sandbox is enforced, not hoped for.

Every test runs with the state dir, ``AXI_HOME``, ``HOME`` and the file
secrets backend pointed into ``tmp_path``.  Two guards make an escape FAIL
rather than quietly touch a real credential store:

* the macOS keychain runner raises, so nothing can reach the real keychain;
* :func:`assert_sandboxed` is called by the fixtures and checks every
  location the code under test resolves lies under the temp tree.

``test_the_sandbox_guard_*`` prove the guards fire.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: Canary values.  Fabricated; they exist only to be searched for.
CANARY_A = "CANARY-vb-alpha-7f3c91d2e0b84a65"
CANARY_B = "CANARY-vb-bravo-02a9c4417de35f80"
CANARY_FILE = "CANARY-vb-keyfile-5b6e1a93c70d2f48"
PASSPHRASE = "correct horse battery staple 94"

_REAL_HOME = Path(os.path.expanduser("~")).resolve()


class SandboxEscape(AssertionError):
    pass


def assert_sandboxed(tmp: Path) -> None:
    """Fail unless every credential-bearing location is inside ``tmp``."""
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
        if rp == _REAL_HOME or _REAL_HOME in rp.parents and tmp not in rp.parents:
            raise SandboxEscape(f"{label} is under the real home: {rp}")
    if os.environ.get("AXIOM_FOREIGN_SECRETS_BACKEND") != "file":
        raise SandboxEscape("foreign secrets backend is not the file backend")
    if os.environ.get("AXIOM_ACTION_LEDGER_BACKEND") != "jsonl":
        raise SandboxEscape("action ledger backend is not jsonl")
    if os.environ.get("AXIOM_DB_URL"):
        raise SandboxEscape("AXIOM_DB_URL is set; the default database must never be reached")


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = tmp_path / "state"
    axi_home = tmp_path / "axi_home"
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

    # Nothing may reach the real keychain.
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
def home_dir(sandbox) -> Path:
    return sandbox / "home"


@pytest.fixture
def vault(state_dir):
    """A real ForeignCredentialStore over the file backend, with canaries."""
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    store = ForeignCredentialStore(state_dir)
    store.set("alpha-token", CANARY_A.encode(), provider="guided", notes="first")
    store.set("bravo-key", CANARY_B.encode(), issuer_url="https://issuer.example/x")
    return store


@pytest.fixture
def fast_kdf(monkeypatch):
    """Cheap scrypt for engine tests; production strength is asserted in test_crypto."""
    from axiom.infra.vault_backup import crypto

    monkeypatch.setattr(crypto, "DEFAULT_KDF", {"n": 2**10, "r": 8, "p": 1})


class Keys:
    """The recovery code lives only here, in the test, as a person would hold it."""

    def __init__(self, code: str):
        self.code = code


@pytest.fixture
def keys(state_dir, fast_kdf) -> Keys:
    from axiom.infra import vault_backup as vb

    ki = vb.init_key(state_dir)
    assert ki.recovery_code
    return Keys(ki.recovery_code)


@pytest.fixture
def root(state_dir) -> Path:
    from axiom.infra.vault_backup.policy import default_root

    return default_root(state_dir)


def damage(path: Path, how: str) -> None:
    data = path.read_bytes()
    if how == "truncate":
        path.write_bytes(data[: len(data) // 2])
    elif how == "flip":
        b = bytearray(data)
        b[len(b) // 2] ^= 0xFF
        path.write_bytes(bytes(b))
    elif how == "zero":
        path.write_bytes(b"")
    elif how == "delete":
        path.unlink()
    elif how == "garbage":
        path.write_bytes(b"\x00" * len(data))
    else:  # pragma: no cover
        raise ValueError(how)
