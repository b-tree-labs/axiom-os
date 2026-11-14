# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S1 Declare (#1818): the log loads, says what it defers, and resolves its site inputs."""

from __future__ import annotations

import hashlib

import pytest

from .conftest import ANN, E2E_LOG, LOG, OTHER, SITE, as_


def declaration_digest() -> str:
    return hashlib.sha256(E2E_LOG.read_bytes()).hexdigest()


@pytest.mark.acceptance("LB-E2E-A1")
def test_lb_e2e_a1_the_log_loads_defers_only_what_it_means_to_and_describes_itself(world, web):
    from axiom.extensions.builtins.attest import registry

    book = registry.get(LOG)
    assert book.handle == "desk"
    assert set(book.not_yet_enforced) == {"logbook.retention"}  # cosign.party: #1807
    r = as_(web, ANN).get(f"/api/v1/attest/logbooks/{LOG}")
    assert r.status_code == 200, r.text
    body = r.json()
    types = {t["id"]: t for t in body["types"]}
    assert set(types) == {"OPEN", "CLOSE", "CHECK", "NOTE", "CHANGE", "HANDOVER", "EXPLAIN"}
    assert types["CHECK"]["presence"] == "desk_room" and types["CHECK"]["posture"] == "sso"
    assert types["NOTE"]["posture"] == "attested" and types["CHANGE"]["roles"] == ["member"]
    assert [f["id"] for f in types["CHECK"]["fields"]] == ["readings"]
    assert body["exports"] == [{"id": "daily", "template": "templates/daily.txt"}]
    # The declaration's bytes are fixed for the run: S13 (A27) asserts they never changed.
    assert len(declaration_digest()) == 64


@pytest.mark.acceptance("LB-E2E-A2")
def test_lb_e2e_a2_the_site_inputs_resolve_for_the_site_and_not_the_other(world):
    from axiom.extensions.builtins.attest import field_sources

    assert [i.id for i in field_sources.resolve("instruments", SITE)] == ["flow", "level", "temp"]
    assert field_sources.resolve("instruments", OTHER) == []


@pytest.mark.acceptance("LB-E2E-A2")
def test_lb_e2e_a2_a_fresh_process_resolves_the_inputs_without_a_register_call(
    attest_db, monkeypatch
):
    # #1799: an extension declares ``kind = "logbook_inputs"``; the node calls it once, before the
    # first read of a log, so nobody (and no test) calls ``register`` by hand.
    from pathlib import Path

    from axiom.extensions.builtins.attest import field_sources, intervals, registry, site_settings

    root = Path(__file__).parent / "extension"
    monkeypatch.setattr(registry, "_logbooks", {})
    monkeypatch.setattr(registry, "_discovered", False)
    monkeypatch.setattr(registry, "_inputs_ran", set())
    monkeypatch.setattr(registry, "_extension_roots", lambda: [root])
    monkeypatch.setattr(field_sources, "_sources", {})
    monkeypatch.setattr(intervals, "_seeds", {})
    monkeypatch.setattr(site_settings, "_source", None)
    assert [i.id for i in field_sources.resolve("instruments", SITE)] == ["flow"]
    assert intervals.next_number(SITE, LOG, "session") == 7000
    assert site_settings.value(SITE, "desk.checks.interval_minutes", 30) == 20
    registry.discover([root])  # a second discovery does not call the extension again
    import sys

    assert sys.modules["axiom_logbook_inputs.desk_inputs"].CALLS == ["register"]
