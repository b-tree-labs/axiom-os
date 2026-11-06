# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Intervals: a signed entry opens one, another closes it (U6, spec Logbook
declaration). An entry that belongs inside an interval cannot be signed
outside one, and every such record says which interval it belongs to."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.attest import intervals, registry, service
from axiom.extensions.builtins.attest.logbooks import LogbookError, parse_logbook
from axiom.extensions.builtins.attest.service import AttestRefused

from .test_signing import SITE, person

LOGBOOK = {
    "logbook": {"id": "run_log", "version": "1"},
    "interval": {"run": {"opens": ["START"], "closes": ["STOP"], "number": {"format": "integer"}}},
    "type": [
        {"id": "START", "meanings": ["authored"], "roles": ["operator"], "requires_interval": "none_open"},
        {"id": "CHECK", "meanings": ["performed"], "roles": ["operator"], "requires_interval": "run"},
        {"id": "STOP", "meanings": ["authored"], "roles": ["operator"], "requires_interval": "run"},
        {"id": "NOTE", "meanings": ["authored"], "roles": ["operator"]},
    ],
}  # fmt: skip


@pytest.fixture
def runs(attest_db):
    registry.register(parse_logbook(LOGBOOK, source="test:runs"))
    yield
    intervals.reset_seeds()


def sign(entry_type, signer, meaning=None):
    m = meaning or {"START": "authored", "STOP": "authored", "NOTE": "authored"}.get(
        entry_type, "performed"
    )
    d = service.create_draft(
        site_id=SITE, logbook="run_log", entry_type=entry_type, meaning=m,
        content={"title": entry_type.lower(), "fields": {}}, origin="human", for_principal="@op1:site-a",
    )  # fmt: skip
    p = service.present(d)
    service.respond(p.presentation_id, principal=person().principal, answer="sign", via="cli")
    return service.sign(p.presentation_id, signatory=person(), signer=signer).record


def test_intervals_and_their_rules_are_enforced_now():
    b = parse_logbook(LOGBOOK)
    assert b.intervals["run"].opens == ("START",) and b.intervals["run"].closes == ("STOP",)
    assert b.type("CHECK").requires_interval == "run"
    assert not [k for k in b.not_yet_enforced if "interval" in k]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"interval": {"run": {"opens": ["NOPE"], "closes": ["STOP"]}}}, "NOPE"),
        ({"interval": {"run": {"opens": ["START"], "closes": []}}}, "closes"),
    ],
)
def test_an_interval_must_name_real_types(change, message):
    with pytest.raises(LogbookError, match=message):
        parse_logbook({**LOGBOOK, **change})


def test_requires_interval_must_name_a_declared_interval():
    extra = {
        "id": "X",
        "meanings": ["performed"],
        "roles": ["operator"],
        "requires_interval": "shift",
    }
    bad = {**LOGBOOK, "type": [*LOGBOOK["type"], extra]}
    with pytest.raises(LogbookError, match="shift"):
        parse_logbook(bad)


def test_a_check_outside_a_run_is_refused(runs, node_signer):
    with pytest.raises(AttestRefused, match="no run is open"):
        sign("CHECK", node_signer)


def test_start_opens_a_numbered_run_that_checks_and_stop_belong_to(runs, node_signer):
    start = sign("START", node_signer)
    check = sign("CHECK", node_signer)
    stop = sign("STOP", node_signer)
    assert start["interval"] == {"kind": "run", "number": 1, "event": "opened"}
    assert check["interval"] == {"kind": "run", "number": 1}
    assert stop["interval"] == {"kind": "run", "number": 1, "event": "closed"}
    assert intervals.open_interval(SITE, "run_log", "run") is None


def test_a_second_start_while_a_run_is_open_is_refused(runs, node_signer):
    sign("START", node_signer)
    with pytest.raises(AttestRefused, match="already open"):
        sign("START", node_signer)


def test_runs_number_on_from_the_sites_seed(runs, node_signer):
    intervals.register_seed("run_log", "run", lambda site: 5401)
    sign("START", node_signer)
    sign("STOP", node_signer)
    second = sign("START", node_signer)
    assert second["interval"]["number"] == 5402


def test_an_entry_with_no_interval_rule_signs_any_time(runs, node_signer):
    assert "interval" not in sign("NOTE", node_signer)
    sign("START", node_signer)
    assert "interval" not in sign("NOTE", node_signer)


def test_the_run_is_part_of_the_signed_record(runs, node_signer):
    from axiom.attest.canonical import digest

    rec = sign("START", node_signer)
    tampered = {**rec, "interval": {"kind": "run", "number": 2, "event": "opened"}}
    assert digest(tampered) != rec["digest"]
