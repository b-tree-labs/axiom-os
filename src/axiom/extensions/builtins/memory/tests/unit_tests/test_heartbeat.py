# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the heartbeat fragment write + freshness check.

The heartbeat is a low-cost periodic write that confirms the memory
write path is healthy end-to-end. `axi dr` checks freshness — if no
heartbeat in the last 2h, something is wrong (cron stopped, disk full,
ledger broken, daemon crashed, …).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


# ---------------------------------------------------------------------------
# record_heartbeat
# ---------------------------------------------------------------------------


def test_record_heartbeat_writes_episodic_fragment_with_heartbeat_kind(
    isolated_composition,
):
    from axiom.memory.session_capture import record_heartbeat

    frag = record_heartbeat(
        composition=isolated_composition,
        principal_id="user@example.org",
    )
    assert frag.cognitive_type.value == "episodic"
    assert frag.content["fact_kind"] == "heartbeat"
    assert frag.content["source"] == "axi-monitor"
    assert "axi-monitor" in frag.provenance.agents
    assert frag.provenance.principal_id == "user@example.org"


def test_record_heartbeat_event_time_is_now_utc(isolated_composition):
    from axiom.memory.session_capture import record_heartbeat

    before = datetime.now(timezone.utc)
    frag = record_heartbeat(
        composition=isolated_composition,
        principal_id="user@example.org",
    )
    after = datetime.now(timezone.utc)
    event_time = datetime.fromisoformat(frag.content["event_time"])
    assert before <= event_time <= after


# ---------------------------------------------------------------------------
# Freshness check
# ---------------------------------------------------------------------------


def _seed_heartbeat_at(composition, principal_id: str, when: datetime):
    """Helper: write a heartbeat with a specific event_time + provenance ts."""
    from axiom.memory.session_capture import record_heartbeat
    return record_heartbeat(
        composition=composition,
        principal_id=principal_id,
        event_time=when.isoformat(),
    )


def test_heartbeat_freshness_ok_when_recent(
    isolated_composition, isolated_settings,
):
    from axiom.extensions.builtins.settings.store import SettingsStore
    from axiom.memory.session_capture import heartbeat_freshness

    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )
    _seed_heartbeat_at(
        isolated_composition,
        "user@example.org",
        datetime.now(timezone.utc) - timedelta(minutes=10),
    )

    status = heartbeat_freshness(composition=isolated_composition)
    assert status["state"] == "ok"
    assert status["age_seconds"] is not None
    assert status["age_seconds"] < 1800  # <30min


def test_heartbeat_freshness_warn_between_1_and_2_hours(
    isolated_composition, isolated_settings,
):
    from axiom.extensions.builtins.settings.store import SettingsStore
    from axiom.memory.session_capture import heartbeat_freshness

    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )
    _seed_heartbeat_at(
        isolated_composition,
        "user@example.org",
        datetime.now(timezone.utc) - timedelta(minutes=90),
    )

    status = heartbeat_freshness(composition=isolated_composition)
    assert status["state"] == "warn"


def test_heartbeat_freshness_error_over_2_hours(
    isolated_composition, isolated_settings,
):
    from axiom.extensions.builtins.settings.store import SettingsStore
    from axiom.memory.session_capture import heartbeat_freshness

    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )
    _seed_heartbeat_at(
        isolated_composition,
        "user@example.org",
        datetime.now(timezone.utc) - timedelta(hours=3),
    )

    status = heartbeat_freshness(composition=isolated_composition)
    assert status["state"] == "error"


def test_heartbeat_freshness_error_when_no_heartbeat_ever(
    isolated_composition, isolated_settings,
):
    from axiom.extensions.builtins.settings.store import SettingsStore
    from axiom.memory.session_capture import heartbeat_freshness

    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )

    status = heartbeat_freshness(composition=isolated_composition)
    assert status["state"] == "error"
    assert status["age_seconds"] is None
    assert "no_heartbeat" in status.get("reason", "")


# ---------------------------------------------------------------------------
# CLI: axi memory heartbeat
# ---------------------------------------------------------------------------


def test_cli_heartbeat_writes_fragment(
    monkeypatch, isolated_composition, isolated_settings, capsys,
):
    from axiom.extensions.builtins.memory import cli
    from axiom.extensions.builtins.settings.store import SettingsStore
    from axiom.memory.session_summary import list_fragments_by_principal

    monkeypatch.setattr(
        cli, "_build_default_composition", lambda: isolated_composition,
    )
    SettingsStore().set(
        "memory.default_principal", "user@example.org", scope="global",
    )

    rc = cli.main(["heartbeat", "--json"])
    assert rc == 0

    frags = list_fragments_by_principal(
        isolated_composition, "user@example.org", limit=5,
    )
    heartbeat_frags = [
        f for f in frags if f.content.get("fact_kind") == "heartbeat"
    ]
    assert len(heartbeat_frags) == 1


# ---------------------------------------------------------------------------
# Doctor check integration
# ---------------------------------------------------------------------------


def test_doctor_default_checks_include_heartbeat_freshness():
    from axiom.cli.doctor import default_checks

    names = [c.name for c in default_checks()]
    assert any("heartbeat" in n.lower() for n in names)


# ---------------------------------------------------------------------------
# launchd plist generator (macOS) — drives the periodic heartbeat
# ---------------------------------------------------------------------------


def test_render_heartbeat_plist_includes_axi_binary_and_interval():
    from axiom.extensions.builtins.memory.heartbeat_install import (
        render_heartbeat_plist,
    )

    plist_xml = render_heartbeat_plist(
        axi_binary="/Users/example/Projects/workspace/.venv/bin/axi",
        interval_seconds=3600,
        log_dir="/tmp/axiom-heartbeat",
    )
    assert "com.axiom.memory.heartbeat" in plist_xml
    assert "/Users/example/Projects/workspace/.venv/bin/axi" in plist_xml
    assert "memory" in plist_xml
    assert "heartbeat" in plist_xml
    assert "<integer>3600</integer>" in plist_xml
    # Standard plist preamble
    assert "<?xml" in plist_xml
    assert "DOCTYPE plist" in plist_xml


def test_install_heartbeat_plist_writes_to_target_path(tmp_path, monkeypatch):
    from axiom.extensions.builtins.memory.heartbeat_install import (
        install_heartbeat_plist,
    )

    target = tmp_path / "com.axiom.memory.heartbeat.plist"
    log_dir = tmp_path / "logs"

    result = install_heartbeat_plist(
        axi_binary="/path/to/axi",
        plist_path=target,
        log_dir=log_dir,
        interval_seconds=3600,
        load=False,
    )

    assert target.exists()
    text = target.read_text()
    assert "/path/to/axi" in text
    assert result["plist_path"] == str(target)
    assert result["loaded"] is False  # we asked load=False


def test_uninstall_heartbeat_plist_removes_file(tmp_path):
    from axiom.extensions.builtins.memory.heartbeat_install import (
        install_heartbeat_plist,
        uninstall_heartbeat_plist,
    )

    target = tmp_path / "com.axiom.memory.heartbeat.plist"
    install_heartbeat_plist(
        axi_binary="/path/to/axi",
        plist_path=target,
        log_dir=tmp_path / "logs",
        load=False,
    )
    assert target.exists()

    result = uninstall_heartbeat_plist(plist_path=target, unload=False)
    assert not target.exists()
    assert result["removed"] is True


def test_uninstall_when_not_installed_is_noop(tmp_path):
    from axiom.extensions.builtins.memory.heartbeat_install import (
        uninstall_heartbeat_plist,
    )

    target = tmp_path / "absent.plist"
    result = uninstall_heartbeat_plist(plist_path=target, unload=False)
    assert result["removed"] is False
