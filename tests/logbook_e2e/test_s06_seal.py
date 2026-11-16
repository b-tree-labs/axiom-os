# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S6 Seal (#1818): a lead closes out the session with what the run left open on purpose, the
node anchors it, the journal hears of it, and a seal locks nothing."""

from __future__ import annotations

from datetime import timedelta

import pytest

from .conftest import (
    ANN,
    BO,
    LOG,
    SITE,
    auth,
    check,
    draft,
    keys,
    open_session,
    sign_cli,
    sign_web,
    signatory,
)


@pytest.mark.acceptance("LB-E2E-A14")
def test_lb_e2e_a14_a_session_is_sealed_with_its_open_items_and_anchored(world, live):
    from axiom.extensions.builtins.attest import grants, obligations, service, signing
    from axiom.extensions.builtins.journal import rights
    from axiom.extensions.builtins.journal import service as journal

    t0 = world.clock["t"]
    open_session(world)
    first = check(world)
    sign_cli(ANN, "NOTE", {"text": "left unacknowledged on purpose"})
    sign_web(ANN, draft(ANN, "CHANGE", {"text": "left for a co-signature"}), device="desk-1")
    draft(ANN, "NOTE", {"text": "left open on purpose"})
    obligations.tick(SITE, LOG, now=t0 + timedelta(minutes=45), state_dir=world.state_dir)
    world.clock["t"] = t0 + timedelta(minutes=46)
    closed = sign_cli(ANN, "CLOSE", {"text": "done"})

    preview = service.seal_preview(SITE, LOG, boundary="session")
    assert preview["covers"]["head_seq"] == closed["seq"] and preview["covers"]["head_digest"]
    kinds = {i["kind"] for i in preview["open_items"]}
    assert {"unacknowledged", "pending_cosign", "open_draft", "missed_obligation"} <= kinds
    d = service.create_seal_draft(site_id=SITE, logbook=LOG, summary="Quiet session, four things carried.",
                                  origin="human", for_principal=BO.handle, boundary="session")  # fmt: skip
    pres = service.present(d, modality="screen")
    service.respond(
        pres.presentation_id, principal=signatory(BO).principal, answer="sign", via="screen"
    )
    token = grants.mint(pres.presentation_id, auth=auth(BO), device=grants.Device("desk-1", "personal"),
                        signer=signing.signer())  # fmt: skip
    result = service.sign(pres.presentation_id, signatory=signatory(BO), signer=signing.signer(),
                          source="screen", grant=token, grant_keys=keys())  # fmt: skip
    seal = result.record
    assert seal["kind"] == "seal" and result.anchor is not None
    assert {i["kind"] for i in seal["content"]["seal"]["open_items"]} == kinds
    rows = journal.query(rights.Caller(handle=BO.handle, sites=None), lanes=["attestations"])[
        "rows"
    ]
    assert [
        r["ref"]["attestation_id"]
        for r in rows
        if r["ref"].get("attestation_id") == seal["attestation_id"]
    ] == [seal["attestation_id"]], "the seal is one reference in the attestations lane"
    # A seal locks nothing: a supplement to an entry it covers still signs.
    d = service.create_supplement_draft(site_id=SITE, target=first["attestation_id"], reason="addition",
                                        content={"fields": {"readings": {"temp": "21.4"}}},
                                        origin="human", for_principal=ANN.handle)  # fmt: skip
    assert sign_web(ANN, d, device="desk-1")["kind"] == "supplement"


def _site_zone(zone="Etc/GMT+6"):
    from axiom.extensions.builtins.attest import site_settings

    site_settings.register(lambda site: {"site": {"timezone": zone}} if site == SITE else {})


def _at(world, day, hh, mm=0):
    """Set the node's clock to a time on the site's own clock (six hours behind UTC here)."""
    from datetime import UTC, datetime

    world.clock["t"] = datetime(2026, 11, day, hh, mm, tzinfo=UTC) + timedelta(hours=6)


@pytest.mark.acceptance("LB-E2E-A15")
def test_lb_e2e_a15_a_clock_period_is_sealed_after_it_ends(world):
    # #1806, decided by the owner: a seal covers the LAST period that ended; start times are read
    # in the site's time zone; preview before sealing.
    from axiom.extensions.builtins.attest import grants, service, signing, site_settings

    _site_zone()
    try:
        _at(world, 10, 7)
        first = sign_cli(ANN, "NOTE", {"text": "in the day period"})
        _at(world, 10, 17, 59)
        last = sign_cli(ANN, "NOTE", {"text": "just before it ends"})
        _at(world, 10, 18, 5)
        after = sign_cli(ANN, "NOTE", {"text": "in the next one"})
        preview = service.seal_preview(SITE, LOG, boundary="day")
        assert preview["boundary"] == "day"
        covers = preview["covers"]
        assert (covers["from_seq"], covers["to_seq"]) == (first["seq"], last["seq"])
        assert covers["period"] == {"name": "day", "start": "2026-11-10T06:00:00-06:00",
                                    "end": "2026-11-10T18:00:00-06:00"}  # fmt: skip
        assert after["seq"] > covers["to_seq"]
        d = service.create_seal_draft(site_id=SITE, logbook=LOG, summary="Day closed.",
                                      origin="human", for_principal=BO.handle, boundary="day")  # fmt: skip
        pres = service.present(d, modality="screen")
        service.respond(pres.presentation_id, principal=signatory(BO).principal, answer="sign",
                        via="screen")  # fmt: skip
        token = grants.mint(pres.presentation_id, auth=auth(BO), device=grants.Device("desk-1", "personal"),
                            signer=signing.signer())  # fmt: skip
        seal = service.sign(pres.presentation_id, signatory=signatory(BO), signer=signing.signer(),
                            source="screen", grant=token, grant_keys=keys()).record  # fmt: skip
        assert seal["content"]["seal"]["covers"]["period"]["name"] == "day"
        with pytest.raises(service.AttestRefused, match="already sealed|nothing"):
            service.seal_preview(SITE, LOG, boundary="day")
    finally:
        site_settings.unregister()


@pytest.mark.acceptance("LB-E2E-A15")
def test_lb_e2e_a15_an_ended_unsealed_period_is_not_closed_only_for_who_may_close_it(world, web):
    from axiom.extensions.builtins.attest import service, site_settings

    _site_zone()
    try:
        _at(world, 10, 9)
        sign_cli(ANN, "NOTE", {"text": "a day's entry"})
        _at(world, 10, 18, 10)  # inside the 30-minute grace window: nothing yet
        assert service.unsealed_periods(SITE, LOG, viewer=BO.handle, state_dir=world.state_dir) == []
        _at(world, 10, 18, 45)
        (due,) = service.unsealed_periods(SITE, LOG, viewer=BO.handle, state_dir=world.state_dir)
        assert due["state"] == "not_closed" and due["period"]["name"] == "day"
        assert due["action"] == {"seal": {"boundary": "day"}}
        assert service.unsealed_periods(SITE, LOG, viewer=ANN.handle, state_dir=world.state_dir) == []
        from .conftest import as_

        assert [p["state"] for p in as_(web, BO).get(f"/api/v1/attest/{LOG}/seal/due").json()["periods"]] == [
            "not_closed"
        ]
        assert as_(web, ANN).get(f"/api/v1/attest/{LOG}/seal/due").json()["periods"] == []
    finally:
        site_settings.unregister()


@pytest.mark.acceptance("LB-E2E-A15")
def test_lb_e2e_a15_a_site_with_no_time_zone_cannot_seal_a_clock_period(world):
    from axiom.extensions.builtins.attest import service

    sign_cli(ANN, "NOTE", {"text": "x"})
    with pytest.raises(service.AttestRefused, match="time zone"):
        service.seal_preview(SITE, LOG, boundary="day")
