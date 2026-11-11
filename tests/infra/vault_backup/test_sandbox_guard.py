# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The guard comes first: a test that could reach a real credential store fails."""

from __future__ import annotations

import pytest

from .conftest import SandboxEscape, assert_sandboxed


def test_the_sandbox_is_inside_tmp(sandbox):
    assert_sandboxed(sandbox)


def test_the_sandbox_guard_fires_when_state_escapes(sandbox, monkeypatch):
    outside = sandbox.parent / f"{sandbox.name}-outside"
    monkeypatch.setenv("AXI_STATE_DIR", str(outside))
    try:
        with pytest.raises(SandboxEscape):
            assert_sandboxed(sandbox)
    finally:
        outside.rmdir() if outside.exists() else None


def test_the_sandbox_guard_fires_when_the_backend_is_not_the_file_backend(sandbox, monkeypatch):
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "keychain")
    with pytest.raises(SandboxEscape):
        assert_sandboxed(sandbox)


def test_the_sandbox_guard_fires_when_a_database_url_is_set(sandbox, monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://localhost:5432/x")
    with pytest.raises(SandboxEscape):
        assert_sandboxed(sandbox)


def test_the_real_keychain_runner_cannot_be_called(sandbox):
    from axiom.extensions.builtins.secrets.providers import keychain

    with pytest.raises(SandboxEscape):
        keychain._subprocess_runner(["find-generic-password"])
