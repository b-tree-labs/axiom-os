# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The guard that keeps these tests off a real ``~/.axi``, keychain and database must itself fail."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from .conftest import SandboxEscape, assert_sandboxed

REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir  # not $HOME: the fixture has already moved that


def test_the_guard_passes_inside_the_sandbox(sandbox):
    assert_sandboxed(sandbox)


@pytest.mark.parametrize("var", ["HOME", "AXI_HOME", "AXI_STATE_DIR", "AXIOM_FILE_SECRETS_PATH"])
def test_the_guard_fires_when_a_location_escapes(sandbox, monkeypatch, var):
    monkeypatch.setenv(var, str(Path(REAL_HOME) / ".axi"))
    with pytest.raises(SandboxEscape):
        assert_sandboxed(sandbox)


def test_the_guard_fires_on_the_real_home(sandbox, monkeypatch):
    monkeypatch.setenv("HOME", REAL_HOME)
    with pytest.raises(SandboxEscape):
        assert_sandboxed(sandbox)


def test_the_guard_fires_on_a_database_url(sandbox, monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://localhost:5432/anything")
    with pytest.raises(SandboxEscape, match="real database"):
        assert_sandboxed(sandbox)


@pytest.mark.parametrize(("var", "bad"), [("AXIOM_FOREIGN_SECRETS_BACKEND", "keychain"),
                                           ("AXIOM_ACTION_LEDGER_BACKEND", "postgres")])
def test_the_guard_fires_on_a_real_backend(sandbox, monkeypatch, var, bad):
    monkeypatch.setenv(var, bad)
    with pytest.raises(SandboxEscape):
        assert_sandboxed(sandbox)


def test_the_keychain_runner_raises(sandbox):
    from axiom.extensions.builtins.secrets.providers import keychain

    with pytest.raises(SandboxEscape):
        keychain._subprocess_runner(["security", "list-keychains"])
