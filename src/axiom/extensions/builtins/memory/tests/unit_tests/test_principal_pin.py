# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the default-principal pin and resolve_principal_id helper.

A user works under `user@example.org` but the harness exposes
`personal@example.com` as the user email. Without a pinned default,
every CLI / MCP call must pass `--principal` explicitly or risk silently
writing under the wrong identity. The pin closes that footgun.
"""

from __future__ import annotations


import pytest


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Force SettingsStore to use a tmp settings.toml."""
    from axiom.extensions.builtins.settings import store as store_mod

    monkeypatch.setattr(
        store_mod, "_get_global_settings_path", lambda: tmp_path / "settings.toml",
    )
    monkeypatch.setattr(
        store_mod, "_PROJECT_SETTINGS_PATH", tmp_path / "project_settings.toml",
    )


# ---------------------------------------------------------------------------
# Default schema
# ---------------------------------------------------------------------------


def test_default_schema_includes_memory_default_principal():
    from axiom.extensions.builtins.settings.store import _DEFAULTS

    assert "memory.default_principal" in _DEFAULTS
    assert _DEFAULTS["memory.default_principal"] == ""


def test_settings_set_and_get_default_principal_round_trip(isolated_settings):
    from axiom.extensions.builtins.settings.store import SettingsStore

    store = SettingsStore()
    store.set("memory.default_principal", "user@example.org", scope="global")

    fresh = SettingsStore()
    assert fresh.get("memory.default_principal") == "user@example.org"


# ---------------------------------------------------------------------------
# resolve_principal_id helper
# ---------------------------------------------------------------------------


def test_resolve_principal_id_returns_explicit_when_provided(isolated_settings):
    from axiom.memory.session_capture import resolve_principal_id

    assert resolve_principal_id("alice@example.org") == "alice@example.org"


def test_resolve_principal_id_uses_pinned_when_explicit_empty(isolated_settings):
    from axiom.extensions.builtins.settings.store import SettingsStore
    from axiom.memory.session_capture import resolve_principal_id

    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )

    assert resolve_principal_id("") == "user@example.org"
    assert resolve_principal_id(None) == "user@example.org"


def test_resolve_principal_id_explicit_overrides_pinned(isolated_settings):
    from axiom.extensions.builtins.settings.store import SettingsStore
    from axiom.memory.session_capture import resolve_principal_id

    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )

    assert resolve_principal_id("alice@example.org") == "alice@example.org"


def test_resolve_principal_id_raises_when_neither_set(isolated_settings):
    from axiom.memory.session_capture import resolve_principal_id

    with pytest.raises(ValueError) as excinfo:
        resolve_principal_id(None)
    msg = str(excinfo.value).lower()
    assert "principal" in msg
    # Error names the fix command so the user knows what to do.
    assert "axi settings" in msg or "default_principal" in msg


# ---------------------------------------------------------------------------
# CLI integration: axi memory record falls back to the pinned principal
# ---------------------------------------------------------------------------


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
def patched_cli(monkeypatch, isolated_composition):
    from axiom.extensions.builtins.memory import cli
    monkeypatch.setattr(
        cli, "_build_default_composition", lambda: isolated_composition,
    )
    return isolated_composition


def test_cli_record_uses_pinned_principal_when_flag_omitted(
    patched_cli, isolated_settings, capsys,
):
    from axiom.extensions.builtins.memory import cli
    from axiom.extensions.builtins.settings.store import SettingsStore
    from axiom.memory.session_summary import list_fragments_by_principal

    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )

    rc = cli.main([
        "record",
        "--tool", "claude-code",
        "--user-input", "from pinned principal",
        "--assistant-output", "ok",
    ])
    assert rc == 0

    frags = list_fragments_by_principal(
        patched_cli, "user@example.org", limit=5,
    )
    assert len(frags) == 1


def test_cli_record_principal_flag_overrides_pin(
    patched_cli, isolated_settings, capsys,
):
    from axiom.extensions.builtins.memory import cli
    from axiom.extensions.builtins.settings.store import SettingsStore
    from axiom.memory.session_summary import list_fragments_by_principal

    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )

    rc = cli.main([
        "record",
        "--principal", "alice@example.org",
        "--tool", "claude-code",
        "--user-input", "explicit",
        "--assistant-output", "ok",
    ])
    assert rc == 0

    # Wrote under alice, not bbooth.
    alice_frags = list_fragments_by_principal(
        patched_cli, "alice@example.org", limit=5,
    )
    bbooth_frags = list_fragments_by_principal(
        patched_cli, "user@example.org", limit=5,
    )
    assert len(alice_frags) == 1
    assert len(bbooth_frags) == 0


def test_cli_record_errors_clearly_when_no_principal_and_no_pin(
    patched_cli, isolated_settings, capsys,
):
    from axiom.extensions.builtins.memory import cli

    rc = cli.main([
        "record",
        "--tool", "claude-code",
        "--user-input", "x",
        "--assistant-output", "y",
    ])
    assert rc != 0
    err = capsys.readouterr().err.lower()
    assert "principal" in err


# ---------------------------------------------------------------------------
# MCP integration: axiom_memory_append falls back to pinned principal
# ---------------------------------------------------------------------------


def test_mcp_append_uses_pinned_when_principal_omitted(
    monkeypatch, isolated_composition, isolated_settings,
):
    from axiom.extensions.builtins.memory import mcp_server
    from axiom.extensions.builtins.settings.store import SettingsStore
    from axiom.memory.session_summary import list_fragments_by_principal

    monkeypatch.setattr(
        mcp_server, "_build_default_composition", lambda: isolated_composition,
    )
    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )

    result = mcp_server.append(
        tool="claude-code",
        user_input="no principal passed",
        assistant_output="ok",
    )
    assert result["principal_id"] == "user@example.org"

    frags = list_fragments_by_principal(
        isolated_composition, "user@example.org", limit=5,
    )
    assert len(frags) == 1


def test_mcp_show_uses_pinned_when_principal_omitted(
    monkeypatch, isolated_composition, isolated_settings,
):
    from axiom.extensions.builtins.memory import mcp_server
    from axiom.extensions.builtins.settings.store import SettingsStore

    monkeypatch.setattr(
        mcp_server, "_build_default_composition", lambda: isolated_composition,
    )
    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )

    # Seed a fragment under the pinned principal.
    mcp_server.append(
        principal_id="user@example.org",
        tool="claude-code",
        user_input="seeded",
        assistant_output="ok",
    )

    out = mcp_server.show()  # no principal_id arg
    assert out["principal"] == "user@example.org"
    assert out["fragment_count"] == 1
