# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for release planning service."""

from __future__ import annotations

import pytest

from axiom.vega.federation.release_plan import ReleasePlanService


@pytest.fixture
def svc(tmp_path):
    return ReleasePlanService(plan_path=tmp_path / "release-plan.yaml")


def test_add_milestone(svc):
    m = svc.add_milestone("0.5.0", codename="Canary", target_date="2026-05-01")
    assert m.version == "0.5.0"
    assert m.codename == "Canary"
    assert m.status == "planned"


def test_duplicate_milestone_raises(svc):
    svc.add_milestone("0.5.0")
    with pytest.raises(ValueError, match="already exists"):
        svc.add_milestone("0.5.0")


def test_stage_feature(svc):
    svc.add_milestone("0.5.0")
    f = svc.stage_feature("0.5.0", "Progressive CLI", "Tiered help", tier=0,
                          cli_commands=["axi model init"], test_count=5)
    assert f.name == "Progressive CLI"
    assert f.tier == 0
    assert f.test_count == 5


def test_stage_feature_missing_milestone(svc):
    with pytest.raises(ValueError, match="not found"):
        svc.stage_feature("9.9.9", "X", "Y")


def test_mark_shipped(svc):
    svc.add_milestone("0.5.0")
    svc.stage_feature("0.5.0", "Feat", "Desc")
    m = svc.mark_shipped("0.5.0")
    assert m.status == "tagged"
    assert all(f.status == "shipped" for f in m.features)


def test_mark_announced(svc):
    svc.add_milestone("0.5.0")
    svc.stage_feature("0.5.0", "Feat", "Desc")
    svc.mark_shipped("0.5.0")
    m = svc.mark_announced("0.5.0")
    assert m.status == "announced"
    assert all(f.status == "announced" for f in m.features)


def test_generate_notes(svc):
    svc.add_milestone("0.5.0", codename="Canary")
    svc.stage_feature("0.5.0", "CLI Tiers", "Progressive disclosure",
                      cli_commands=["axi model init"], test_count=10)
    notes = svc.generate_notes("0.5.0")
    assert "# Release 0.5.0" in notes
    assert "Canary" in notes
    assert "CLI Tiers" in notes
    assert "`axi model init`" in notes
    assert "10 tests" in notes


def test_list_milestones_filters_by_status(svc):
    svc.add_milestone("0.5.0")
    svc.add_milestone("0.6.0")
    svc.mark_shipped("0.5.0")
    planned = svc.list_milestones(status="planned")
    assert len(planned) == 1
    assert planned[0].version == "0.6.0"


def test_next_milestone(svc):
    svc.add_milestone("0.5.0")
    svc.add_milestone("0.6.0")
    svc.mark_shipped("0.5.0")
    nxt = svc.next_milestone()
    assert nxt is not None
    assert nxt.version == "0.6.0"


def test_round_trip_persistence(svc):
    svc.add_milestone("0.5.0", codename="Canary")
    svc.stage_feature("0.5.0", "Feat", "Desc", tier=2, test_count=7)
    # Reload via new service instance pointing to same file
    svc2 = ReleasePlanService(plan_path=svc._path)
    m = svc2.get_milestone("0.5.0")
    assert m is not None
    assert m.codename == "Canary"
    assert len(m.features) == 1
    assert m.features[0].test_count == 7
    assert m.features[0].tier == 2


def test_feature_test_count_tracked(svc):
    svc.add_milestone("0.5.0")
    svc.stage_feature("0.5.0", "A", "a", test_count=3)
    svc.stage_feature("0.5.0", "B", "b", test_count=7)
    m = svc.get_milestone("0.5.0")
    assert sum(f.test_count for f in m.features) == 10
