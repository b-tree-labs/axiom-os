# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F16: a cohort's list is imported in bulk, kept in the record (not the flag) and
counted; a cohort can follow an external group through a pluggable source and refresh
from it on a declared schedule run by the platform scheduler."""

from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.features import cohort_sources
from axiom.extensions.builtins.flags import cli
from axiom.extensions.builtins.features.tests.conftest import mcp, run, svc
from axiom.infra import audit_trail

MANIFEST = Path(cohort_sources.__file__).parent / "axiom-extension.toml"


def _members(n: int) -> list[str]:
    return [f"agent-{i:05d}" for i in range(n)]


@pytest.mark.acceptance("LOOP-F16")
def test_a_list_is_imported_from_a_file_previewed_then_applied_and_counted(modes, tmp_path, capsys):
    path = tmp_path / "fleet.csv"
    path.write_text("principal\n" + "\n".join(_members(5000)) + "\n")
    assert cli.main(["cohort", "import", "fleet", "--file", str(path), "--reason", "the fleet"]) == 0
    out = capsys.readouterr().out
    assert "preview" in out and "5000" in out
    assert modes["targeting"].cohort("fleet") is None
    assert cli.main(["cohort", "import", "fleet", "--file", str(path), "--reason", "the fleet", "--apply"]) == 0
    stored = modes["targeting"].cohort("fleet")
    assert stored["size"] == 5000 and len(stored["principals"]) == 5000
    modes["targeting"].put_rollout("beta_editor", "development", rules=[{"cohort": "fleet", "value": "on"}],
                                   reason="r", by="@t")
    assert svc().resolve_flag("beta_editor", user="agent-04999") == "on"
    assert svc().resolve_flag("beta_editor", user="stranger") == "off"
    # The flag names the cohort; it never carries the list.
    assert modes["targeting"].rollout("beta_editor", "development")["rules"] == [{"cohort": "fleet", "value": "on"}]


@pytest.mark.acceptance("LOOP-F16")
def test_an_import_adds_or_replaces_and_reports_the_difference(modes, tmp_path):
    ts = modes["targeting"]
    ts.put_cohort("fleet", principals=_members(10), by="@t")
    out = run("flags.cohort_import", {"key": "fleet", "members": _members(15)[5:], "mode": "add",
                                         "reason": "five more"}, modes["state"])
    assert out.ok, out.errors
    assert out.value["after"]["size"] == 15 and out.value["added"] == 5 and out.value["removed"] == 0
    out = run("flags.cohort_import", {"key": "fleet", "members": ["solo"], "reason": "reset"}, modes["state"])
    assert out.ok and out.value["after"]["size"] == 1 and out.value["removed"] == 15


@pytest.mark.acceptance("LOOP-F16")
def test_an_import_from_an_ai_client_waits_on_a_person(modes):
    out = mcp("axiom_flags__cohort_import", {"key": "fleet", "members": _members(3), "reason": "r"},
              modes["state"])
    assert not out["ok"] and out["value"]["plan_hash"]
    assert modes["targeting"].cohort("fleet") is None


@pytest.mark.acceptance("LOOP-F16")
def test_a_large_write_is_audited_by_count_not_by_listing_every_member(modes):
    out = run("flags.cohort_import", {"key": "fleet", "members": _members(2000), "reason": "r"}, modes["state"])
    assert out.ok
    rec = [r for r in audit_trail.read_chain(audit_trail.audit_path(modes["state"]))
           if r["skill"] == "flags.cohort_import"][-1]
    new = rec["change"]["new"]
    assert new["principals"]["count"] == 2000 and len(new["principals"]["sample"]) <= 10
    assert new["principals"]["sha256"]


def _directory(tmp_path, monkeypatch, members):
    path = tmp_path / "directory.json"
    path.write_text(json.dumps({"groups": {"g-ops": {"members": members, "display": "Operators"}}}))
    monkeypatch.setenv("AXIOM_DIRECTORY_PROVIDER", "local")
    monkeypatch.setenv("AXIOM_DIRECTORY_LOCAL_FILE", str(path))
    return path


@pytest.mark.acceptance("LOOP-F16")
def test_a_cohort_follows_a_directory_group_and_refreshes_on_its_schedule(modes, tmp_path, monkeypatch):
    path = _directory(tmp_path, monkeypatch, ["oid-1", "oid-2"])
    out = run("flags.cohort_set", {"key": "ops", "source": {"kind": "directory", "group": "g-ops"},
                                      "refresh_every": "1h", "reason": "follow the ops group"}, modes["state"])
    assert out.ok, out.errors
    stored = modes["targeting"].cohort("ops")
    assert stored["principals"] == ["oid-1", "oid-2"] and stored["source"]["group"] == "g-ops"
    assert stored["refresh_every_s"] == 3600 and stored["refreshed_at"]

    path.write_text(json.dumps({"groups": {"g-ops": {"members": ["oid-2", "oid-3", "oid-4"]}}}))
    soon = datetime.now(UTC) + timedelta(minutes=10)
    assert run("flags.rollout_tick", {"now": soon.isoformat()}, modes["state"]).ok
    assert modes["targeting"].cohort("ops")["principals"] == ["oid-1", "oid-2"]  # not due yet
    later = datetime.now(UTC) + timedelta(hours=2)
    tick = run("flags.rollout_tick", {"now": later.isoformat()}, modes["state"])
    assert tick.ok and tick.value["refreshed"] == ["ops"]
    assert modes["targeting"].cohort("ops")["principals"] == ["oid-2", "oid-3", "oid-4"]


@pytest.mark.acceptance("LOOP-F16")
def test_a_source_that_fails_keeps_the_last_members_and_says_why(modes, tmp_path, monkeypatch):
    path = _directory(tmp_path, monkeypatch, ["oid-1"])
    assert run("flags.cohort_set", {"key": "ops", "source": {"kind": "directory", "group": "g-ops"},
                                       "refresh_every": "1h", "reason": "r"}, modes["state"]).ok
    path.write_text("not json")
    out = run("flags.cohort_refresh", {"key": "ops"}, modes["state"])
    assert not out.ok
    stored = modes["targeting"].cohort("ops")
    assert stored["principals"] == ["oid-1"] and stored["source_error"]


@pytest.mark.acceptance("LOOP-F16")
def test_a_source_is_pluggable_and_an_unknown_one_is_refused(modes):
    out = run("flags.cohort_set", {"key": "x", "source": {"kind": "nowhere", "group": "g"}, "reason": "r"},
              modes["state"])
    assert not out.ok and "nowhere" in out.errors[0]
    cohort_sources.register_source("static", lambda spec: list(spec["members"]))
    out = run("flags.cohort_set", {"key": "x", "source": {"kind": "static", "members": ["p1", "p2"]},
                                      "reason": "r"}, modes["state"])
    assert out.ok, out.errors
    assert modes["targeting"].cohort("x")["size"] == 2


@pytest.mark.acceptance("LOOP-F16")
def test_refreshing_runs_on_the_platform_scheduler(modes):
    manifest = tomllib.loads(MANIFEST.read_text())
    blocks = [b for b in manifest["extension"]["schedule"] if b["action"] == "flags.rollout_tick"]
    assert len(blocks) == 1 and blocks[0]["hosted"] is True
    from axiom.extensions.builtins.schedule.manifest import parse_manifest_block

    parsed = parse_manifest_block(blocks[0])
    assert parsed.cadence.kind == "interval"


@pytest.mark.acceptance("LOOP-F16")
def test_the_record_grows_only_by_adding(modes):
    """Membership, sources, stage plans, schedules and approvals live in the record; the
    migration that holds them only adds (ADR-182)."""
    from axiom.infra.deploy.migration_guard import contracting_operations

    path = Path(cli.__file__).parents[1] / "flags" / "migrations" / "versions" / "0003_rollout_modes.py"
    text = path.read_text()
    assert contracting_operations(text) == []
    for table in ("stage_plans", "changes"):
        assert f'"{table}"' in text
