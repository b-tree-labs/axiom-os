# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for #1803: a generic version history of a state derived from signed records.

A state is a declared key set (field ids) of one log at one site. Each signed entry that changes
the value of a declared key opens a version; each version names the entry that opened it and,
per key, its change since the version before, worded here (``no change``, ``from X``) so a
surface formats nothing. The comparison is L7's (``changes.compare``, exact, with Decimal) and
values are read after corrections by L7's rule (``changes.corrected``). Rows LG-1803-A1 to A5 are
Axiom's; A6 to A10 are the web half, in axiom-appkit (``DerivedVersions``). No domain words: a
"change log" with a quantity, a number and a text.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import versions  # noqa: E402
from axiom.governance import doors as g  # noqa: E402

from .test_l7 import LOG, SITE, change_log, correct, draft, power, sign  # noqa: E402, F401

pytestmark = pytest.mark.usefixtures("change_log")


def by_key(version):
    return {k["key"]: k for k in version["keys"]}


# -- A1: a version opens only when a declared key changes -----------------------------------


@pytest.mark.acceptance("LG-1803-A1")
def test_lg1803_a1_a_version_opens_only_when_a_declared_key_changes(node_signer):
    _, first = sign(draft({"power": power("500"), "count": "3", "note": "set up"}), node_signer)
    sign(draft({"power": power("500.0"), "count": "3", "note": "nothing moved"}), node_signer)
    _, third = sign(draft({"power": power("500"), "count": "4"}), node_signer)
    sign(draft({"note": "a key outside the set"}), node_signer)
    got = versions.derive(SITE, LOG, ["power", "count"])
    opened = [v["opened_by"]["attestation_id"] for v in got["versions"]]
    assert opened == [third["attestation_id"], first["attestation_id"]], "newest first"
    assert [v["number"] for v in got["versions"]] == [2, 1]
    assert got["complete"] is True
    assert [k["key"] for k in got["keys"]] == ["power", "count"]
    assert [k["label"] for k in got["keys"]] == ["Power", "Count"]


@pytest.mark.acceptance("LG-1803-A1")
def test_lg1803_a1_another_site_and_another_entry_type_are_other_states(node_signer):
    sign(draft({"power": power("500")}), node_signer)
    sign(draft({"power": power("700")}, entry_type="OTHER"), node_signer)
    assert len(versions.derive(SITE, LOG, ["power"], entry_type="CHECK")["versions"]) == 1
    assert len(versions.derive(SITE, LOG, ["power"])["versions"]) == 2
    assert versions.derive("site-b", LOG, ["power"])["versions"] == []


# -- A2: the words, from L7's exact comparison -------------------------------------------------


@pytest.mark.acceptance("LG-1803-A2")
def test_lg1803_a2_each_key_says_no_change_or_from_the_previous_value(node_signer):
    sign(draft({"power": power("500"), "count": "3", "note": "first"}), node_signer)
    sign(draft({"power": power("950"), "count": "3.0", "note": "second"}), node_signer)
    sign(draft({"power": power("0.95", "MW"), "count": "3", "note": "second"}), node_signer)
    newest, middle, oldest = versions.derive(SITE, LOG, ["power", "count", "note"])["versions"]
    first = by_key(oldest)
    assert {k: (v["value"], v["change"], v["kind"]) for k, v in first.items()} == {
        "power": ("500 kW", "", "first"),
        "count": ("3", "", "first"),
        "note": ("first", "", "first"),
    }, "a first value has no change words"
    mid = by_key(middle)
    assert (mid["power"]["change"], mid["power"]["kind"]) == ("from 500 kW", "changed")
    assert (mid["count"]["change"], mid["count"]["kind"]) == ("no change", "no_change"), "3 == 3.0"
    assert (mid["note"]["change"], mid["note"]["previous"]) == ("from first", "first")
    new = by_key(newest)
    assert (new["power"]["change"], new["power"]["value"]) == ("from 950 kW", "0.95 MW"), (
        "a unit change is a change"
    )
    assert new["note"]["change"] == "no change"


@pytest.mark.acceptance("LG-1803-A2")
def test_lg1803_a2_a_key_first_set_after_the_first_version_says_set(node_signer):
    sign(draft({"power": power("500")}), node_signer)
    sign(draft({"power": power("500"), "count": "3"}), node_signer)
    sign(draft({"power": power("600"), "count": "3"}), node_signer)
    newest, middle, oldest = versions.derive(SITE, LOG, ["power", "count"])["versions"]
    assert [k["key"] for k in oldest["keys"]] == ["power"]
    assert (by_key(oldest)["power"]["kind"], by_key(oldest)["power"]["change"]) == ("first", "")
    count = by_key(middle)["count"]
    assert (count["kind"], count["change"], count["previous"]) == ("set", "set", None)
    # Every key of every version after the first carries words.
    for v in (newest, middle):
        assert all(k["change"] for k in v["keys"]), v


@pytest.mark.acceptance("LG-1803-A2")
def test_lg1803_a2_the_comparison_is_l7s():
    assert versions.same({"value": "950", "unit": "kW"}, {"value": "950.0", "unit": "kW"})
    assert not versions.same({"value": "950", "unit": "kW"}, {"value": "0.95", "unit": "MW"})
    assert versions.same("a text", "a text") and not versions.same("a", "b")
    assert versions.same(["x", "y"], ["x", "y"]) and not versions.same(["x"], ["y"])
    import inspect

    assert "changes.compare" in inspect.getsource(versions.same), "one comparison, not a second"


# -- A3: after corrections ---------------------------------------------------------------------


@pytest.mark.acceptance("LG-1803-A3")
def test_lg1803_a3_a_correction_changes_its_version_and_opens_none(node_signer):
    sign(draft({"power": power("500")}), node_signer)
    _, second = sign(draft({"power": power("950")}), node_signer)
    fix = correct(second, {"power": power("900")}, node_signer)
    got = versions.derive(SITE, LOG, ["power"])["versions"]
    assert len(got) == 2, "a correction is not a version"
    newest = by_key(got[0])["power"]
    assert (newest["value"], newest["change"]) == ("900 kW", "from 500 kW")
    assert newest["corrected_by"] == fix["attestation_id"]
    assert got[0]["opened_by"]["attestation_id"] == second["attestation_id"]
    # A correction that restates the earlier value closes the version it opened.
    correct(second, {"power": power("500")}, node_signer)
    assert len(versions.derive(SITE, LOG, ["power"])["versions"]) == 1


# -- A4: the door ------------------------------------------------------------------------------


@pytest.mark.acceptance("LG-1803-A4")
def test_lg1803_a4_the_versions_door_is_a_registered_row():
    door = g.get("attest.http.versions")
    assert door.route == ("GET", "/api/v1/attest/{logbook}/versions")
    assert door.shape == "target" and set(door.params) >= {"entry_type", "keys"}
    assert door.surface == "api" and door.serves == "versions"


@pytest.mark.acceptance("LG-1803-A4")
def test_lg1803_a4_served_newest_first_with_one_read_record(node_signer, client):
    from axiom.extensions.builtins.attest.tests.test_api import OP1, as_

    sign(draft({"power": power("500")}), node_signer)
    _, second = sign(draft({"power": power("950")}), node_signer)
    c = as_(client, OP1)
    got = c.get(f"/api/v1/attest/{LOG}/versions?site={SITE}&keys=power,count&entry_type=CHECK")
    assert got.status_code == 200, got.text
    body = got.json()
    assert (body["site_id"], body["logbook"], body["entry_type"]) == (SITE, LOG, "CHECK")
    assert body["versions"][0]["opened_by"]["attestation_id"] == second["attestation_id"]
    assert by_key(body["versions"][0])["power"]["change"] == "from 500 kW"
    empty = c.get(f"/api/v1/attest/{LOG}/versions?site={SITE}")
    assert empty.status_code == 200 and empty.json()["versions"] == [], "no keys, no versions"
    none = c.get(f"/api/v1/attest/{LOG}/versions?site={SITE}&keys=note")
    assert none.status_code == 200 and none.json()["versions"] == []
    unknown = c.get(f"/api/v1/attest/{LOG}/versions?site={SITE}&keys=power&entry_type=NOPE")
    assert unknown.status_code == 404


# -- A5: each version names the entry that opened it -------------------------------------------


@pytest.mark.acceptance("LG-1803-A5")
def test_lg1803_a5_each_version_names_its_signed_entry(node_signer):
    _, rec = sign(draft({"power": power("500")}), node_signer)
    opened = versions.derive(SITE, LOG, ["power"])["versions"][0]["opened_by"]
    assert opened == {
        "attestation_id": rec["attestation_id"],
        "seq": rec["seq"],
        "occurred_at": rec["occurred_at"],
        "signer": (rec.get("signer") or {}).get("display") or rec["signer"]["principal"],
        "entry_type": "CHECK",
        "logbook": LOG,
    }
