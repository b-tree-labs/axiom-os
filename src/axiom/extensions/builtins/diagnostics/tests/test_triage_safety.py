# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for TRIAGE's safety check sweep + extension-author registry pattern."""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.diagnostics import agent_cli, safety
from axiom.extensions.builtins.diagnostics.safety import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    Finding,
    run_check,
    sweep,
)

# Test-local check callables (the extension-author surface)


def _check_returns_nothing():
    return None


def _check_returns_one_finding():
    return Finding(
        check_name="test.one",
        severity=SEVERITY_WARNING,
        title="One thing",
        detail="A single warning finding",
    )


def _check_returns_iterable():
    return [
        Finding(check_name="test.iter", severity=SEVERITY_INFO, title="info-1"),
        Finding(check_name="test.iter", severity=SEVERITY_CRITICAL, title="critical-1"),
    ]


def _check_raises():
    raise RuntimeError("boom")


def _check_returns_garbage():
    return 42


def _check_no_severity_set():
    return Finding(check_name="test.nosev", severity="", title="missing severity")


# run_check — never raises; surfaces check failures as Findings


class TestRunCheck:
    def test_returns_none_means_no_findings(self):
        result = run_check(
            "test.none",
            "axiom.extensions.builtins.diagnostics.tests.test_triage_safety:_check_returns_nothing",
            SEVERITY_WARNING,
        )
        assert result == []

    def test_single_finding_passthrough(self):
        result = run_check(
            "test.one",
            "axiom.extensions.builtins.diagnostics.tests.test_triage_safety:_check_returns_one_finding",
            SEVERITY_WARNING,
        )
        assert len(result) == 1
        assert result[0].title == "One thing"

    def test_iterable_returns_all(self):
        result = run_check(
            "test.iter",
            "axiom.extensions.builtins.diagnostics.tests.test_triage_safety:_check_returns_iterable",
            SEVERITY_WARNING,
        )
        assert len(result) == 2
        assert {f.severity for f in result} == {SEVERITY_INFO, SEVERITY_CRITICAL}

    def test_check_that_raises_becomes_critical_finding(self):
        result = run_check(
            "test.raises",
            "axiom.extensions.builtins.diagnostics.tests.test_triage_safety:_check_raises",
            SEVERITY_WARNING,
        )
        assert len(result) == 1
        assert result[0].severity == SEVERITY_CRITICAL
        assert "raised an exception" in result[0].title
        assert "boom" in result[0].detail

    def test_check_that_returns_garbage_becomes_warning(self):
        result = run_check(
            "test.garbage",
            "axiom.extensions.builtins.diagnostics.tests.test_triage_safety:_check_returns_garbage",
            SEVERITY_WARNING,
        )
        assert len(result) == 1
        assert result[0].severity == SEVERITY_WARNING
        assert "neither Finding nor iterable" in result[0].title

    def test_unloadable_entry_becomes_critical_finding(self):
        result = run_check(
            "test.bad",
            "no.such.module:nope",
            SEVERITY_WARNING,
        )
        assert len(result) == 1
        assert result[0].severity == SEVERITY_CRITICAL
        assert "failed to load" in result[0].title

    def test_severity_default_applied_when_missing(self):
        result = run_check(
            "test.nosev",
            "axiom.extensions.builtins.diagnostics.tests.test_triage_safety:_check_no_severity_set",
            SEVERITY_INFO,
        )
        assert result[0].severity == SEVERITY_INFO


# Manifest schema — safety_check provider type is parsed


class TestSafetyCheckProviderParsing:
    def test_diagnostics_manifest_declares_safety_checks(self):
        from axiom.extensions.discovery import discover_extensions

        for ext in discover_extensions():
            if ext.name == "diagnostics":
                names = {sc.name for sc in ext.safety_checks}
                assert "diagnostics.state_dir_disk_space" in names
                assert "diagnostics.pending_patches_stale" in names
                return
        pytest.fail("diagnostics extension not discovered")

    def test_discover_safety_checks_returns_built_ins(self):
        checks = safety.discover_safety_checks()
        names = {name for name, _, _ in checks}
        assert "diagnostics.state_dir_disk_space" in names
        assert "diagnostics.pending_patches_stale" in names


# Built-in checks


class TestBuiltinChecks:
    def test_disk_space_returns_empty_when_plenty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "axiom.extensions.builtins.diagnostics.safety.get_user_state_dir",
            lambda: tmp_path,
        )
        result = list(safety.check_state_dir_disk_space())
        # If the test runner has <5GB free we'd get a finding — assert shape
        for f in result:
            assert f.severity in (SEVERITY_WARNING, SEVERITY_CRITICAL)
            assert "State-dir partition" in f.title

    def test_pending_patches_empty_dir_returns_no_findings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "axiom.extensions.builtins.diagnostics.safety.get_user_state_dir",
            lambda: tmp_path,
        )
        result = list(safety.check_pending_patches_stale())
        assert result == []

    def test_pending_patches_stale_file_warns(self, tmp_path, monkeypatch):
        import os
        import time

        pending = tmp_path / "agents" / "triage" / "patches" / "pending"
        pending.mkdir(parents=True)
        old_patch = pending / "stale.json"
        old_patch.write_text("{}")
        old_ts = time.time() - (48 * 3600)
        os.utime(old_patch, (old_ts, old_ts))

        monkeypatch.setattr(
            "axiom.extensions.builtins.diagnostics.safety.get_user_state_dir",
            lambda: tmp_path,
        )
        result = list(safety.check_pending_patches_stale(max_age_hours=24.0))
        assert len(result) == 1
        assert result[0].severity == SEVERITY_WARNING
        assert "stale.json" in result[0].detail

    def test_pending_patches_fresh_file_no_warning(self, tmp_path, monkeypatch):
        pending = tmp_path / "agents" / "triage" / "patches" / "pending"
        pending.mkdir(parents=True)
        (pending / "fresh.json").write_text("{}")

        monkeypatch.setattr(
            "axiom.extensions.builtins.diagnostics.safety.get_user_state_dir",
            lambda: tmp_path,
        )
        result = list(safety.check_pending_patches_stale(max_age_hours=24.0))
        assert result == []


# sweep() — aggregates across all registered checks


class TestSweep:
    def test_sweep_aggregates_findings(self, monkeypatch):
        monkeypatch.setattr(
            safety,
            "discover_safety_checks",
            lambda: [
                (
                    "test.iter",
                    "axiom.extensions.builtins.diagnostics.tests.test_triage_safety:_check_returns_iterable",
                    SEVERITY_WARNING,
                ),
                (
                    "test.one",
                    "axiom.extensions.builtins.diagnostics.tests.test_triage_safety:_check_returns_one_finding",
                    SEVERITY_WARNING,
                ),
            ],
        )
        result = sweep()
        assert result["agent"] == "triage"
        assert result["checks_run"] == 2
        assert result["findings_total"] == 3
        assert result["findings_by_severity"][SEVERITY_CRITICAL] == 1
        assert result["findings_by_severity"][SEVERITY_INFO] == 1
        assert result["findings_by_severity"][SEVERITY_WARNING] == 1


# CLI — heartbeat / sweep / checks subcommands


class TestAgentCLI:
    def test_heartbeat_subcommand_in_parser(self):
        parser = agent_cli.build_parser()
        args = parser.parse_args(["heartbeat"])
        assert args.action == "heartbeat"

    def test_sweep_and_checks_subcommands_in_parser(self):
        parser = agent_cli.build_parser()
        for cmd in ("sweep", "checks"):
            args = parser.parse_args([cmd])
            assert args.action == cmd

    def test_heartbeat_writes_jsonl_signal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "axiom.extensions.builtins.diagnostics.agent_cli.get_user_state_dir",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            safety,
            "discover_safety_checks",
            lambda: [
                (
                    "test.one",
                    "axiom.extensions.builtins.diagnostics.tests.test_triage_safety:_check_returns_one_finding",
                    SEVERITY_WARNING,
                ),
            ],
        )
        rc = agent_cli.main(["heartbeat"])
        assert rc == 0

        log = tmp_path / "agents" / "triage" / "sweep.jsonl"
        assert log.exists()
        entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        assert len(entries) == 1
        assert entries[0]["agent"] == "triage"
        assert entries[0]["checks_run"] == 1
        assert entries[0]["findings_total"] == 1

    def test_heartbeat_returns_2_on_critical(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "axiom.extensions.builtins.diagnostics.agent_cli.get_user_state_dir",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            safety,
            "discover_safety_checks",
            lambda: [
                (
                    "test.iter",
                    "axiom.extensions.builtins.diagnostics.tests.test_triage_safety:_check_returns_iterable",
                    SEVERITY_WARNING,
                ),
            ],
        )
        rc = agent_cli.main(["heartbeat"])
        assert rc == 2
