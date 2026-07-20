# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the principal-pin reconciliation check in `axi dr`.

When the user has set ``memory.default_principal`` but recent writes
landed under a different principal, that's silent dysfunction — the
fragment writes succeeded but a future ``axi memory show`` (which
defaults to the pin) won't surface them. The reconciliation check
flags drift before users wonder why their memory is "empty."
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    from axiom.extensions.builtins.settings import store as store_mod
    monkeypatch.setattr(
        store_mod, "_get_global_settings_path", lambda: tmp_path / "settings.toml",
    )
    monkeypatch.setattr(
        store_mod, "_PROJECT_SETTINGS_PATH", tmp_path / "project_settings.toml",
    )


@pytest.fixture
def isolated_composition(tmp_path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base = tmp_path / "memory_ledger"
    base.mkdir()
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


@pytest.fixture
def patched_doctor(monkeypatch, isolated_composition):
    """Make the doctor module use the isolated composition for queries."""
    from axiom.extensions.builtins.memory import cli as memory_cli
    monkeypatch.setattr(
        memory_cli, "_build_default_composition", lambda: isolated_composition,
    )
    return isolated_composition


def _record_under(composition, principal_id: str, tool: str = "claude-code"):
    from axiom.memory.session_capture import record_session_turn
    return record_session_turn(
        composition=composition,
        principal_id=principal_id,
        tool=tool,
        user_input="test", assistant_output="test",
    )


# ---------------------------------------------------------------------------
# OK / WARN / SKIPPED
# ---------------------------------------------------------------------------


def test_principal_reconcile_ok_when_recent_writes_match_pin(
    patched_doctor, isolated_settings,
):
    from axiom.cli.doctor import (
        CheckStatus,
        check_axiom_memory_principal_reconciliation,
    )
    from axiom.extensions.builtins.settings.store import SettingsStore

    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )
    for _ in range(3):
        _record_under(patched_doctor, "user@example.org")

    result = check_axiom_memory_principal_reconciliation()
    assert result.status == CheckStatus.OK


def test_principal_reconcile_warns_when_recent_writes_use_different_principal(
    patched_doctor, isolated_settings,
):
    from axiom.cli.doctor import (
        CheckStatus,
        check_axiom_memory_principal_reconciliation,
    )
    from axiom.extensions.builtins.settings.store import SettingsStore

    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )
    # Recent writes went to a different principal — drift.
    for _ in range(3):
        _record_under(patched_doctor, "personal@example.com")

    result = check_axiom_memory_principal_reconciliation()
    assert result.status == CheckStatus.WARNING
    assert "axi settings" in (result.fix_hint or "")


def test_principal_reconcile_skipped_when_no_pin(
    patched_doctor, isolated_settings,
):
    from axiom.cli.doctor import (
        CheckStatus,
        check_axiom_memory_principal_reconciliation,
    )

    # No pin set; nothing to reconcile against.
    result = check_axiom_memory_principal_reconciliation()
    assert result.status in (CheckStatus.SKIPPED, CheckStatus.WARNING)


def test_principal_reconcile_ok_when_no_writes_yet(
    patched_doctor, isolated_settings,
):
    from axiom.cli.doctor import (
        CheckStatus,
        check_axiom_memory_principal_reconciliation,
    )
    from axiom.extensions.builtins.settings.store import SettingsStore

    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )
    # Pinned but no writes — not drift; just empty.
    result = check_axiom_memory_principal_reconciliation()
    assert result.status in (CheckStatus.OK, CheckStatus.SKIPPED)


def test_doctor_default_checks_include_principal_reconciliation():
    from axiom.cli.doctor import default_checks

    names = [c.name for c in default_checks()]
    assert any("principal" in n.lower() for n in names)
