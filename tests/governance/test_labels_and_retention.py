# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The one label model and the retention floors (MVP-22, MVP-08-A3).

Test names carry the acceptance row they prove (``test_mvp_22_a1_…``), which is
how the acceptance harness finds them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.governance import classification as c
from axiom.governance import retention as r


# --------------------------------------------------------------- labels


def test_mvp_22_a1_an_unconfirmed_label_is_no_label_for_export_or_retrieval():
    proposed = c.propose_label("clear")
    assert proposed.classification is c.Classification.INTERNAL and not proposed.confirmed
    for act in ("exported", "retrieved"):
        with pytest.raises(c.Unlabelled, match=act):
            c.require_label(proposed, act=act)
        with pytest.raises(c.Unlabelled):
            c.require_label(None, act=act)
    ok = c.require_label(proposed.confirm("@alice:alpha"), act="exported")
    assert ok.confirmed and ok.by == "@alice:alpha"


def test_mvp_22_a1_every_detector_verdict_maps_onto_the_one_vocabulary():
    # the library EC hook (clear/sensitive/held) and the ingest screen (none/sensitive/controlled)
    assert c.propose_label("none").classification is c.Classification.INTERNAL
    assert c.propose_label("sensitive").classification is c.Classification.REGULATED
    assert c.propose_label("held").classification is c.Classification.CONTROLLED
    assert c.propose_label("controlled").classification is c.Classification.CONTROLLED
    # an unknown verdict proposes the strictest label, never the loosest
    assert c.propose_label("???").classification is c.Classification.CONTROLLED


def test_a_label_round_trips_and_a_confirmation_names_who():
    label = c.confirmed_label("regulated", by="@bob:alpha", basis="distribution marking")
    again = c.ExportControlLabel.from_json(label.to_json())
    assert again == label
    assert c.ExportControlLabel.from_json({}) is None
    assert c.ExportControlLabel.from_json({"value": "nonsense"}) is None
    with pytest.raises(ValueError):
        c.confirmed_label("internal", by="")


def test_mvp_06_a1_clearance_fails_closed_and_compares_with_the_label():
    try:
        assert c.clearance_for(None) is c.Classification.PUBLIC
        assert c.clearance_for("@alice:alpha") is c.DEFAULT_CLEARANCE
        c.set_clearance_resolver(lambda p: "controlled" if p == "@eve:alpha" else None)
        assert c.clearance_for("@eve:alpha") is c.Classification.CONTROLLED
        assert c.clearance_for("@alice:alpha") is c.DEFAULT_CLEARANCE

        def boom(_p):
            raise RuntimeError("directory down")

        c.set_clearance_resolver(boom)
        assert c.clearance_for("@eve:alpha") is c.DEFAULT_CLEARANCE  # no elevation on a fault
    finally:
        c.set_clearance_resolver(None)
    assert c.permits(c.Classification.INTERNAL, c.Classification.INTERNAL)
    assert not c.permits(c.Classification.INTERNAL, c.propose_label("held"))


# --------------------------------------------------------------- retention

NOW = datetime(2026, 10, 30, tzinfo=UTC)


def test_mvp_22_a2_deleting_before_the_floor_is_refused_with_the_date_named():
    created = NOW - timedelta(days=30)
    with pytest.raises(r.RetentionRefused) as e:
        r.check_deletion(r.RetentionClass.OPERATIONAL, created, now=NOW)
    assert "operational" in str(e.value) and "5 years" in str(e.value)
    assert e.value.until is not None and e.value.until > NOW
    # after the floor it is allowed
    r.check_deletion(r.RetentionClass.OPERATIONAL, NOW - timedelta(days=6 * 365), now=NOW)
    r.check_deletion(r.RetentionClass.SYSTEM, NOW - timedelta(days=400), now=NOW)


def test_mvp_22_a2_lifetime_legal_hold_and_holds_are_never_deleted_by_age():
    old = NOW - timedelta(days=50 * 365)
    for cls in (r.RetentionClass.LIFETIME, r.RetentionClass.LEGAL_HOLD):
        with pytest.raises(r.RetentionRefused):
            r.check_deletion(cls, old, now=NOW)
    with pytest.raises(r.RetentionRefused, match="export-control hold"):
        r.check_deletion(r.RetentionClass.SYSTEM, old, now=NOW, holds=["an export-control hold"])


def test_mvp_22_a2_a_deletion_is_recorded_in_the_tamper_evident_chain(tmp_path):
    from axiom.infra.audit_trail import audit_path, read_chain, verify_chain

    link = r.record_deletion(kind="library.item", subject="item-1", principal="@alice:alpha", site="alpha",
                             retention_class=r.RetentionClass.SYSTEM, forever=True,
                             detail={"sha256": "ab" * 32}, state_dir=tmp_path)
    chain = read_chain(audit_path(tmp_path))
    assert [rec["skill"] for rec in chain] == ["retention.delete.library.item"]
    assert chain[0]["outcome"] == "deleted-forever" and chain[0]["hash"] == link["hash"]
    assert verify_chain(audit_path(tmp_path))


def test_mvp_08_a3_a_site_may_only_lengthen_and_a_shorter_value_names_the_floor():
    longer = r.site_policy({"operational": 10 * 365})
    assert longer.floor(r.RetentionClass.OPERATIONAL) == timedelta(days=3650)
    with pytest.raises(r.RetentionRefused) as e:
        r.site_policy({"operational": 365})
    assert "platform floor of 5 years" in str(e.value) and "only lengthen" in str(e.value)
    with pytest.raises(r.RetentionRefused, match="no time floor"):
        r.site_policy({"legal_hold": 10})
    with pytest.raises(r.RetentionRefused):
        r.site_policy({"system": "a while"})


def test_a_policy_provider_cannot_shorten_a_floor():
    try:
        r.set_policy_provider(lambda site: r.SitePolicy(floors={r.RetentionClass.SYSTEM: timedelta(days=1)}))
        assert r.policy_for("alpha").floor(r.RetentionClass.SYSTEM) == r.PLATFORM_FLOORS[r.RetentionClass.SYSTEM]
    finally:
        r.set_policy_provider(None)


def test_a_class_change_may_lengthen_never_shorten_and_a_hold_ends_by_release():
    r.check_reclassify(r.RetentionClass.SYSTEM, r.RetentionClass.OPERATIONAL)
    r.check_reclassify(r.RetentionClass.OPERATIONAL, r.RetentionClass.LEGAL_HOLD)
    with pytest.raises(r.RetentionRefused, match="only lengthen"):
        r.check_reclassify(r.RetentionClass.OPERATIONAL, r.RetentionClass.SYSTEM)
    with pytest.raises(r.RetentionRefused, match="released"):
        r.check_reclassify(r.RetentionClass.LEGAL_HOLD, r.RetentionClass.OPERATIONAL)
    r.check_reclassify(r.RetentionClass.LEGAL_HOLD, r.RetentionClass.OPERATIONAL, release_hold=True)
