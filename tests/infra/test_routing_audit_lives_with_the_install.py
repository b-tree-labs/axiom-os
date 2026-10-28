# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The routing audit is one file per install, not one per working directory.

An MCP server spawned by a harness runs with a working directory of ``/`` and
``AXIOM_ROOT`` pointing at a temp dir; ``audit explain`` runs from a checkout.
While the log hung off the project root, the two never met: sixty-four
checkouts on one machine each had their own log, and the id a withhold handed
the client resolved nowhere. The log now lives in the platform state directory.
The old location is still read, so ids written by older code are still found.
"""

from __future__ import annotations

import json

from axiom.infra import routing_audit


def test_the_audit_path_follows_the_install_not_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", None)
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_ROOT", str(tmp_path / "elsewhere"))
    expected = tmp_path / "state" / "logs" / "routing_audit.jsonl"

    assert routing_audit.audit_path() == expected
    other = tmp_path / "somewhere" / "else"
    other.mkdir(parents=True)
    monkeypatch.chdir(other)
    assert routing_audit.audit_path() == expected, "the working directory must not move the log"


def test_a_write_lands_in_the_install_log_and_resolves(tmp_path, monkeypatch):
    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", None)
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    assert routing_audit.log_routing_decision(
        routing_event_id="rid-home", tier="export_controlled", classifier="label", reason="r",
        extra={"revision": "0.70.2@9286bcac1", "labels": ["classification=export_controlled"]},
    ) is True
    found = routing_audit.find_routing_decision("rid-home")
    assert found is not None and found["revision"] == "0.70.2@9286bcac1"
    assert found["labels"] == ["classification=export_controlled"]


def test_an_id_written_by_older_code_under_a_checkout_is_still_found(tmp_path, monkeypatch):
    legacy = tmp_path / "runtime" / "logs" / "routing_audit.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps({"routing_event_id": "rid-legacy", "tier": "export_controlled", "classifier": "keyword"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", tmp_path / "home.jsonl")
    monkeypatch.setattr(routing_audit, "_LEGACY_PATHS", [legacy])

    found = routing_audit.find_routing_decision("rid-legacy")
    assert found is not None and found["routing_event_id"] == "rid-legacy"
    assert routing_audit.find_routing_decision("rid-missing") is None


def test_the_install_log_wins_over_a_legacy_copy(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(json.dumps({"routing_event_id": "rid-both", "tier": "old"}) + "\n", encoding="utf-8")
    home = tmp_path / "home.jsonl"
    home.write_text(json.dumps({"routing_event_id": "rid-both", "tier": "new"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", home)
    monkeypatch.setattr(routing_audit, "_LEGACY_PATHS", [legacy])
    assert routing_audit.find_routing_decision("rid-both")["tier"] == "new"


def test_a_failed_write_returns_false_and_warns(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is needed", encoding="utf-8")
    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", blocker / "routing_audit.jsonl")
    with caplog.at_level("WARNING", logger="axiom.infra.routing_audit"):
        written = routing_audit.log_routing_decision(
            routing_event_id="rid-fail", tier="export_controlled", classifier="label", reason="r"
        )
    assert written is False
    assert any("rid-fail" in rec.getMessage() for rec in caplog.records)
