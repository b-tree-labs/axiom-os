# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F22: each flag declares its kind (release, operational, permission, experiment,
kill switch) in its manifest, and the kind sets its defaults: a release flag must have a
removal date; a kill switch defaults on and is exempt from staleness."""

from __future__ import annotations

from datetime import date

import pytest

from axiom.extensions.builtins.features.flag_registry import FLAG_KINDS, FlagDef, parse_manifest_flags
from axiom.extensions.builtins.features.tests.conftest import mcp, svc


@pytest.mark.acceptance("LOOP-F22")
def test_the_five_kinds(modes):
    assert FLAG_KINDS == ("release", "operational", "permission", "experiment", "kill_switch")
    with pytest.raises(ValueError, match="kind"):
        FlagDef.from_mapping({"key": "x", "kind": "temporary"})


@pytest.mark.acceptance("LOOP-F22")
def test_a_release_flag_needs_a_removal_date(caplog):
    with pytest.raises(ValueError, match="remove_by"):
        FlagDef.from_mapping({"key": "new_nav", "kind": "release"})
    flag = FlagDef.from_mapping({"key": "new_nav", "kind": "release", "remove_by": date(2027, 3, 1),
                                 "owner": "@operator"})
    assert flag.remove_by == date(2027, 3, 1) and not flag.stale_exempt
    assert FlagDef.from_mapping({"key": "n", "kind": "release", "remove_by": "2027-03-01",
                                 "owner": "@operator"}).remove_by == date(2027, 3, 1)
    # From a manifest a malformed declaration is skipped and said, never half-loaded.
    assert parse_manifest_flags({"flags": [{"key": "new_nav", "kind": "release"}]}, owner="ext") == []
    assert "remove_by" in caplog.text


@pytest.mark.acceptance("LOOP-F22")
def test_a_kill_switch_defaults_on_and_is_exempt_from_staleness(modes):
    flag = svc().get_registry()["ops_brake"]
    assert flag.kind == "kill_switch" and flag.default == "on" and flag.stale_exempt
    assert svc().resolve_flag("ops_brake", user="ana") == "on"
    assert FlagDef.from_mapping({"key": "k", "kind": "kill_switch", "default": "off"}).default == "off"


@pytest.mark.acceptance("LOOP-F22")
def test_an_undeclared_kind_is_listed_as_unclassified_and_resolves_as_before(modes):
    flag = svc().get_registry()["beta_editor"]
    assert flag.kind == "" and flag.default == "off"
    rows = {r["name"]: r for r in mcp("axiom_flags__list", {}, modes["state"])["value"]["rows"]}
    assert rows["ops_brake"]["details"]["flag_kind"] == "kill_switch"
    assert rows["beta_editor"]["details"]["flag_kind"] is None
    assert svc().explain("ops_brake", user="ana")["kind"] == "kill_switch"


@pytest.mark.acceptance("LOOP-F22")
def test_the_stale_report_has_its_seam(modes):
    """The kind supplies what LOOP-F11's stale report reads (flags/lifecycle.py)."""
    release = FlagDef.from_mapping({"key": "n", "kind": "release", "remove_by": "2026-01-01", "owner": "@operator"})
    assert release.removal_due(date(2026, 6, 1)) and not release.removal_due(date(2025, 6, 1))
    brake = svc().get_registry()["ops_brake"]
    assert not brake.removal_due(date(2099, 1, 1))
