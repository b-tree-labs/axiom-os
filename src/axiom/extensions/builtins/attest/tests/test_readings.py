# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Readings whose instruments come from the site (U2).

A logbook says a field's instruments come from the site (``from_site``); the
extension that owns the site's configuration registers where. Each reading
is stored as the person entered it, with the instrument's unit and its
declared uncertainty, or an explicit "unquantified" — never a bare number,
never zero for unknown."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.attest import field_sources, registry, service
from axiom.extensions.builtins.attest.field_sources import Instrument
from axiom.extensions.builtins.attest.logbooks import LogbookError, parse_logbook
from axiom.extensions.builtins.attest.service import AttestRefused

from .test_signing import SITE, person

INSTRUMENTS = [
    Instrument(id="power", label="Power", unit="kW"),
    Instrument(id="pool_temp", label="Pool temperature", unit="degC",
               uncertainty={"kind": "magnitude_only", "u": "0.5"}),
]  # fmt: skip


@pytest.fixture
def checks(attest_db):
    field_sources.register("instruments", lambda site: INSTRUMENTS if site == SITE else [])
    registry.register(
        parse_logbook(
            {
                "logbook": {"id": "check_log", "version": "1", "display": "Checks"},
                "type": [
                    {
                        "id": "CHECK",
                        "meanings": ["performed"],
                        "roles": ["operator"],
                        "confirm": {"modalities_any": ["screen", "cli"]},
                        "fields": [
                            {"id": "readings", "type": "readings", "observe": True,
                             "required": True, "from_site": "instruments"},
                        ],
                    }
                ],
            },
            source="test:checks",
        )
    )  # fmt: skip
    yield
    field_sources.unregister("instruments")


def draft(readings):
    return service.create_draft(
        site_id=SITE, logbook="check_log", entry_type="CHECK", meaning="performed",
        content={"title": "Check", "fields": {"readings": readings}},
        origin="human", for_principal="@op1:site-a",
    )  # fmt: skip


def test_from_site_is_now_enforced_not_deferred():
    logbook = parse_logbook(
        {
            "logbook": {"id": "b", "version": "1"},
            "type": [{"id": "T", "meanings": ["performed"], "roles": ["r"],
                      "fields": [{"id": "r", "type": "readings", "from_site": "instruments"}]}],
        }
    )  # fmt: skip
    assert logbook.type("T").field("r").from_site == "instruments"
    assert not [k for k in logbook.not_yet_enforced if k.endswith("from_site")]


def test_from_site_belongs_on_a_readings_field():
    with pytest.raises(LogbookError, match="readings"):
        parse_logbook(
            {
                "logbook": {"id": "b", "version": "1"},
                "type": [{"id": "T", "meanings": ["performed"], "roles": ["r"],
                          "fields": [{"id": "x", "type": "text", "from_site": "instruments"}]}],
            }
        )  # fmt: skip


def test_a_reading_is_stored_with_its_unit_and_uncertainty(checks):
    d = draft({"power": "950", "pool_temp": {"value": "31.2", "unit": "degC"}})
    stored = service.draft(d)["content"]["fields"]["readings"]
    assert stored["power"] == {
        "value": "950",
        "unit": "kW",
        "uncertainty": {"kind": "unquantified"},
    }
    assert stored["pool_temp"] == {
        "value": "31.2",
        "unit": "degC",
        "uncertainty": {"kind": "magnitude_only", "u": "0.5"},
    }


def test_every_instrument_must_be_read_before_it_can_be_presented(checks):
    d = draft({"power": "950"})
    with pytest.raises(AttestRefused, match="pool_temp"):
        service.present(d)


@pytest.mark.parametrize(
    ("readings", "message"),
    [
        ({"power": {"value": "950", "unit": "W"}}, "unit"),
        ({"flux": "1e12"}, "flux"),
        ({"power": "lots"}, "decimal"),
        ({"power": 950.0}, "decimal"),
        ("950", "instrument"),
    ],
)
def test_a_bad_reading_is_refused(checks, readings, message):
    with pytest.raises(AttestRefused, match=message):
        draft(readings)


def test_the_presentation_shows_and_says_each_reading(checks):
    p = service.present(draft({"power": "950", "pool_temp": "31.2"}))
    assert "Power: 950 kW" in p.forms["card"]
    assert "Pool temperature: 31.2 degC" in p.forms["card"]
    assert "<digits>950</digits>" in p.speakable


def test_a_signed_check_keeps_the_readings_as_entered(checks, node_signer):
    d = draft({"power": "950", "pool_temp": "31.20"})
    pres = service.present(d)
    service.respond(pres.presentation_id, principal=person().principal, answer="sign", via="cli")
    rec = service.sign(pres.presentation_id, signatory=person(), signer=node_signer).record
    assert rec["content"]["fields"]["readings"]["pool_temp"]["value"] == "31.20"


def test_a_logbook_whose_source_is_not_registered_says_so(attest_db):
    registry.register(
        parse_logbook(
            {
                "logbook": {"id": "orphan_log", "version": "1"},
                "type": [{"id": "T", "meanings": ["performed"], "roles": ["operator"],
                          "fields": [{"id": "r", "type": "readings", "from_site": "nowhere"}]}],
            },
            source="test:orphan",
        )
    )  # fmt: skip
    with pytest.raises(AttestRefused, match="nowhere"):
        service.create_draft(
            site_id=SITE, logbook="orphan_log", entry_type="T", meaning="performed",
            content={"title": "x", "fields": {"r": {"a": "1"}}},
            origin="human", for_principal="@op1:site-a",
        )  # fmt: skip


def test_instruments_are_resolved_per_site(checks):
    assert [i.id for i in field_sources.resolve("instruments", SITE)] == ["power", "pool_temp"]
    assert field_sources.resolve("instruments", "site-b") == []
