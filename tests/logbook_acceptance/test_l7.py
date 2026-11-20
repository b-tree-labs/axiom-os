# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit L7: change since the previous signed value, with nothing pre-filled.

Ticket: docs/working/logbook-units/L7.md in the product repo that holds the plan, built against
`docs/working/design-note-l7-change.md` (approved 2026-11-12: the change shows at confirmation
after entry and on the signed record; the default phrase shows only a move, with "same as last",
the previous value and its entry one step away; a unit mismatch shows only to the signer; and
"previous" reaches across intervals and names the interval). Rows A1 to A7 are Axiom's; A8 is
the web half, in axiom-appkit. No domain words: a "change log" with a quantity, a number and
readings.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from decimal import Decimal

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import changes, field_sources, registry, service  # noqa: E402
from axiom.extensions.builtins.attest.field_sources import Instrument  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import parse_logbook  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import person  # noqa: E402

SITE = "site-a"
LOG = "change_log"
ME = "@op1:site-a"
INSTRUMENTS = [
    Instrument(id="flow", label="Flow", unit="L/min"),
    Instrument(id="temp", label="Temperature", unit="degC"),
]


@pytest.fixture
def change_log(attest_db):
    field_sources.register("gauges", lambda site: INSTRUMENTS if site == SITE else [])
    registry.register(
        parse_logbook(
            {
                "logbook": {"id": LOG, "version": "1", "display": "Change Log"},
                "type": [
                    {
                        "id": "CHECK",
                        "meanings": ["performed"],
                        "roles": ["operator"],
                        "confirm": {"modalities_any": ["screen", "cli"]},
                        "fields": [
                            {"id": "power", "type": "quantity", "unit": "kW", "observe": True,
                             "label": "Power"},
                            {"id": "count", "type": "number", "label": "Count"},
                            {"id": "gauges", "type": "readings", "observe": True,
                             "from_site": "gauges"},
                            {"id": "note", "type": "text"},
                        ],
                    },
                    {
                        "id": "OTHER",
                        "meanings": ["performed"],
                        "roles": ["operator"],
                        "confirm": {"modalities_any": ["screen", "cli"]},
                        "fields": [{"id": "power", "type": "quantity", "unit": "kW"}],
                    },
                ],
            },
            source="test:change_log",
        )
    )  # fmt: skip
    yield registry.get(LOG)
    field_sources.unregister("gauges")


def draft(fields, entry_type="CHECK", site=SITE):
    return service.create_draft(
        site_id=site, logbook=LOG, entry_type=entry_type, meaning="performed",
        content={"title": "Check", "fields": fields}, origin="human", for_principal=ME,
    )  # fmt: skip


def sign(draft_id, signer):
    who = person()
    p = service.present(draft_id)
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="cli")
    return p, service.sign(p.presentation_id, signatory=who, signer=signer).record


def power(value, unit="kW"):
    return {"value": value, "unit": unit}


def correct(target, fields, signer):
    d = service.create_supplement_draft(
        site_id=SITE, target=target["attestation_id"], reason="correction",
        content={"fields": fields}, origin="human", for_principal=ME,
    )  # fmt: skip
    return sign(d, signer)[1]


def by_field(items):
    return {(c["field"], c.get("instrument")): c for c in items}


def strings_only(value):
    """Every leaf is a string, a bool, an int (a sequence number) or None; never a float."""
    if isinstance(value, dict):
        return all(strings_only(v) for v in value.values())
    if isinstance(value, list):
        return all(strings_only(v) for v in value)
    return not isinstance(value, float)


# -- A1: exact comparison ---------------------------------------------------------------------


@pytest.mark.acceptance("LB-L7-A1")
def test_lb_l7_a1_compare_is_exact_and_keeps_the_right_places():
    up = changes.compare({"value": "500", "unit": "kW"}, {"value": "950", "unit": "kW"})
    assert (up["kind"], up["delta"]) == ("up", "450")
    down = changes.compare({"value": "20.4", "unit": "degC"}, {"value": "20.1", "unit": "degC"})
    assert (down["kind"], down["delta"]) == ("down", "-0.3")
    same = changes.compare({"value": "950", "unit": "kW"}, {"value": "950.0", "unit": "kW"})
    assert (same["kind"], same["delta"]) == ("no_change", "0.0")
    places = changes.compare({"value": "20.4", "unit": "degC"}, {"value": "20.45", "unit": "degC"})
    assert places["delta"] == "0.05"
    big = changes.compare({"value": "1E+2", "unit": ""}, {"value": "250", "unit": ""})
    assert big["delta"] == "150"
    other = changes.compare({"value": "500", "unit": "kW"}, {"value": "0.9", "unit": "MW"})
    assert (other["kind"], other["delta"]) == ("unit_changed", None)
    with pytest.raises(TypeError):
        changes.compare({"value": 500.0, "unit": "kW"}, {"value": "950", "unit": "kW"})


@pytest.mark.acceptance("LB-L7-A1")
def test_lb_l7_a1_the_words_follow_the_note(change_log, node_signer):
    _, first = sign(draft({"power": power("500"), "count": "7"}), node_signer)
    p = service.present(draft({"power": power("950"), "count": "7"}))
    got = by_field(p.changes)
    assert got[("power", None)]["text"] == "up 450 kW"
    assert got[("power", None)]["detail"].startswith(f"from 500 kW, #{first['seq']}, ")
    assert got[("count", None)]["kind"] == "no_change"
    assert got[("count", None)]["text"] == "", "an equal value shows nothing by default"
    assert got[("count", None)]["detail"].startswith(f"same as last, 7, #{first['seq']}")
    mismatch = service.present(draft({"power": power("0.9", "MW")}))
    m = by_field(mismatch.changes)[("power", None)]
    assert (m["kind"], m["delta"]) == ("unit_changed", None)
    assert m["text"] == "unit differs from the last entry (kW)", (
        "the signer is told, at confirmation"
    )


# -- A2: the first value ----------------------------------------------------------------------


@pytest.mark.acceptance("LB-L7-A2")
def test_lb_l7_a2_the_first_value_has_no_previous_and_no_note(change_log, node_signer):
    p = service.present(draft({"power": power("500")}))
    assert p.changes == []
    assert "Change since previous" not in p.forms["card"]
    # another type and another site never count as "previous"
    sign(draft({"power": power("500")}, entry_type="OTHER"), node_signer)
    assert service.present(draft({"power": power("600")})).changes == []


# -- A3: after a correction -------------------------------------------------------------------


@pytest.mark.acceptance("LB-L7-A3")
def test_lb_l7_a3_a_corrected_value_is_the_previous_value(change_log, node_signer):
    sign(draft({"power": power("500")}), node_signer)
    _, second = sign(draft({"power": power("950"), "gauges": {"flow": "12", "temp": "20.4"}}),
                     node_signer)  # fmt: skip
    fix = correct(second, {"power": power("900"), "gauges": {"temp": "20.5"}}, node_signer)
    p = service.present(draft({"power": power("1000"), "gauges": {"flow": "12", "temp": "20.1"}}))
    got = by_field(p.changes)
    assert got[("power", None)]["text"] == "up 100 kW"
    assert got[("power", None)]["previous_ref"]["attestation_id"] == second["attestation_id"]
    assert got[("power", None)]["previous_ref"]["corrected_by"] == fix["attestation_id"]
    assert got[("gauges", "temp")]["text"] == "down 0.4 degC", "per instrument, after the fix"
    assert got[("gauges", "flow")]["kind"] == "no_change", "an instrument the fix left alone"


# -- A4: nothing before entry -----------------------------------------------------------------


@pytest.mark.acceptance("LB-L7-A4")
def test_lb_l7_a4_nothing_is_pre_filled_or_offered(change_log, node_signer, client):
    from axiom.extensions.builtins.attest.tests.test_api import OP1, as_

    sign(draft({"power": power("512.5")}), node_signer)
    described = as_(client, OP1).get(f"/api/v1/attest/logbooks/{LOG}")
    assert described.status_code == 200, described.text
    assert "512.5" not in described.text and "changes" not in described.json()
    empty = service.draft(draft({"note": "nothing entered yet"}))
    assert "512.5" not in json.dumps(empty, default=str) and "changes" not in empty
    p = service.present(draft({"note": "nothing entered yet"}))
    assert p.changes == [], "a field with no value in the draft has no change"
    assert "512.5" not in json.dumps(p.forms)


@pytest.mark.acceptance("LB-L7-A4")
def test_lb_l7_a4_a_machine_supplied_observed_value_is_still_refused(change_log):
    with pytest.raises(service.AttestRefused, match="observe"):
        service.create_draft(
            site_id=SITE, logbook=LOG, entry_type="CHECK", meaning="performed",
            content={"title": "Check", "fields": {"power": power("500")}}, origin="agent",
            for_principal=ME,
        )  # fmt: skip


# -- A5: at confirmation, in the stored text, never in the digest -----------------------------


@pytest.mark.acceptance("LB-L7-A5")
def test_lb_l7_a5_the_change_is_in_the_stored_text_and_not_in_the_digest(change_log, node_signer):
    later = draft({"power": power("950")})
    before = service.present(later)
    assert before.changes == []
    sign(draft({"power": power("500")}), node_signer)
    after = service.present(later)
    assert after.digest == before.digest, "the statement never depends on earlier records"
    assert "Change since previous" in after.forms["card"] and "up 450 kW" in after.forms["card"]
    assert "up 450 kW" in after.forms["page"]
    assert "Change since previous" not in after.forms["line"]
    assert after.rendered == after.forms["card"]
    _, rec = sign(later, node_signer)
    shown = [p["text"] for p in rec["evidence"]["presented"]]
    assert any("up 450 kW" in t for t in shown), "the record keeps what the signer saw"


# -- A6: the record reads the same ------------------------------------------------------------


@pytest.mark.acceptance("LB-L7-A6")
def test_lb_l7_a6_the_signed_record_reads_as_it_was_confirmed(change_log, node_signer, client,
                                                              tmp_path):  # fmt: skip
    from axiom.extensions.builtins.attest import api
    from axiom.extensions.builtins.attest.skills import read as read_skills
    from axiom.extensions.builtins.attest.tests.test_api import OP1, as_

    _, first = sign(draft({"power": power("500"), "count": "3"}), node_signer)
    shown, second = sign(draft({"power": power("950"), "count": "3"}), node_signer)
    at_confirm = [{k: c[k] for k in ("field", "instrument", "kind", "delta", "text")}
                  for c in shown.changes]  # fmt: skip
    on_record = changes.for_record(second)
    assert [{k: c[k] for k in ("field", "instrument", "kind", "delta", "text")}
            for c in on_record] == at_confirm  # fmt: skip
    assert api._presentation_json(shown)["changes"] == shown.changes
    got = as_(client, OP1).get(f"/api/v1/attest/{LOG}/records/{second['attestation_id']}")
    assert got.status_code == 200, got.text
    assert [c["text"] for c in got.json()["changes"]] == [c["text"] for c in on_record]
    r = read_skills.show({"attestation_id": second["attestation_id"]}, None)
    assert r.ok and r.value["changes"] == on_record
    # A correction of the earlier entry, signed later, is what the record view is for.
    correct(first, {"power": power("600")}, node_signer)
    assert by_field(changes.for_record(second))[("power", None)]["text"] == "up 350 kW"
    # The CLI prints the changes under the record.
    from axiom.extensions.builtins.attest import log_cli
    from axiom.infra.skills import SkillResult

    out = io.StringIO()
    with redirect_stdout(out):
        log_cli._print_text(SkillResult(ok=True, value={"record": second,
                                                        "changes": changes.for_record(second)}))  # fmt: skip
    printed = out.getvalue()
    assert "Change since previous" in printed and "Power: up 350 kW" in printed
    assert "Count: same as last" in printed


@pytest.mark.acceptance("LB-L7-A6")
def test_lb_l7_a6_a_unit_mismatch_on_a_record_shows_no_warning():
    prev = {"value": "500", "unit": "kW", "attestation_id": "a", "seq": 4,
            "occurred_at": "2026-11-12T06:58:00+00:00", "interval": None}  # fmt: skip
    item = changes.describe("power", None, "Power", prev, {"value": "0.9", "unit": "MW"},
                            signer=False)  # fmt: skip
    assert item["kind"] == "unit_changed" and item["text"] == ""
    assert item["detail"] == "from 500 kW, #4, 06:58; now 0.9 MW"


@pytest.mark.acceptance("LB-L7-A6")
def test_lb_l7_a6_previous_across_intervals_names_the_interval():
    prev = {"value": "500", "unit": "kW", "attestation_id": "a", "seq": 412,
            "occurred_at": "2026-11-12T06:58:00+00:00", "interval": {"kind": "run", "number": 41}}  # fmt: skip
    item = changes.describe("power", None, "Power", prev, {"value": "950", "unit": "kW"},
                            current_interval={"kind": "run", "number": 42})  # fmt: skip
    assert item["detail"] == "from 500 kW, #412, 06:58, in run 41"
    same = changes.describe("power", None, "Power", prev, {"value": "950", "unit": "kW"},
                            current_interval={"kind": "run", "number": 41})  # fmt: skip
    assert same["detail"] == "from 500 kW, #412, 06:58"


# -- A7: no float -----------------------------------------------------------------------------


@pytest.mark.acceptance("LB-L7-A7")
def test_lb_l7_a7_no_float_anywhere(change_log, node_signer):
    sign(draft({"power": power("500.25"), "count": "2", "gauges": {"flow": "1.5"}}), node_signer)
    p = service.present(draft({"power": power("950"), "count": "3", "gauges": {"flow": "1.75"}}))
    assert p.changes and strings_only(p.changes)
    for c in p.changes:
        assert isinstance(c["delta"], str) and Decimal(c["delta"]) == Decimal(c["delta"])
        assert isinstance(c["previous"]["value"], str)
    assert by_field(p.changes)[("power", None)]["delta"] == "449.75"
