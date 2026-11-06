# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Presence (ADR-146): some acts must be signed where the thing was observed.

A device's class and location come from its enrolment, never from the caller.
A fixed device proves presence by being enrolled there; a portable one proves
it per signature with the location's rotating code, which is shown only on the
location's fixed display, so reading it proves the signer can see the room."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.attest import devices, grants, presence, registry, service
from axiom.extensions.builtins.attest.grants import Device, GrantRefused
from axiom.extensions.builtins.attest.logbooks import parse_logbook

from .test_grants import auth, keys, presented, sign_with
from .test_signing import SITE

SECRET = bytes(range(32))


@pytest.fixture
def room(attest_db):
    presence.set_secret_provider(lambda site, loc: SECRET if loc == "control_room" else None)
    registry.register(
        parse_logbook(
            {
                "logbook": {"id": "room_log", "version": "1", "display": "Room"},
                "type": [
                    {
                        "id": "CHECK",
                        "meanings": ["observed"],
                        "roles": ["operator"],
                        "presence": "control_room",
                        "confirm": {"modalities_any": ["screen"]},
                        "fields": [{"id": "value", "type": "text", "required": True}],
                    }
                ],
            },
            source="test:room",
        )
    )
    devices.enroll(site_id=SITE, device_id="console-a", device_class="kiosk",
                   location="control_room", mobility="fixed", by="@admin:site-a")  # fmt: skip
    devices.enroll(site_id=SITE, device_id="tablet-3", device_class="tablet",
                   location="control_room", mobility="portable", by="@admin:site-a")  # fmt: skip
    devices.enroll(site_id=SITE, device_id="office-pc", device_class="personal",
                   location="office", mobility="fixed", by="@admin:site-a")  # fmt: skip
    yield
    presence.reset_secret_provider()


def _check():
    return presented(
        service.create_draft(
            site_id=SITE, logbook="room_log", entry_type="CHECK", meaning="observed",
            content={"title": "check", "fields": {"value": "ok"}},
            origin="human", for_principal="@op1:site-a",
        )
    )  # fmt: skip


def _mint(pres, device_id, code=None, signer=None):
    return grants.mint(
        pres.presentation_id,
        auth=auth(),
        device=Device(device_id=device_id, device_class="personal"),
        presence_code=code,
        signer=signer,
    )


# -- the code ----------------------------------------------------------------------------


def test_code_is_six_digits_and_changes_each_window():
    t = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
    a = presence.code(SECRET, "control_room", t)
    b = presence.code(SECRET, "control_room", t + timedelta(seconds=presence.WINDOW_SECONDS))
    assert len(a) == 6 and a.isdigit() and a != b


def test_code_differs_per_location():
    t = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
    assert presence.code(SECRET, "control_room", t) != presence.code(SECRET, "lab", t)


def test_the_previous_window_is_accepted_and_older_is_not(room):
    t = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
    current = presence.code(SECRET, "control_room", t)
    w = timedelta(seconds=presence.WINDOW_SECONDS)
    assert presence.verify_code(SITE, "control_room", current, now=t + w)
    assert not presence.verify_code(SITE, "control_room", current, now=t + 2 * w)


def test_no_secret_means_no_code_can_prove_presence(room):
    assert not presence.verify_code(SITE, "lab", "123456")


# -- enrolment decides -----------------------------------------------------------------------


def test_a_fixed_device_at_the_location_proves_presence_by_enrolment(room, node_signer):
    pres = _check()
    token = _mint(pres, "console-a", signer=node_signer)
    claims = grants.read(token, keys(node_signer))
    assert claims["device_class"] == "kiosk"  # from enrolment, not the caller's "personal"
    assert claims["presence"] == {"location": "control_room", "proof": "enrolment"}
    rec = sign_with(pres, token, node_signer).record
    assert rec["assurance"]["presence"] == {"location": "control_room", "proof": "enrolment"}


def test_a_portable_device_needs_the_current_code(room, node_signer):
    pres = _check()
    with pytest.raises(GrantRefused, match="presence_required"):
        _mint(pres, "tablet-3", signer=node_signer)
    with pytest.raises(GrantRefused, match="presence_required"):
        _mint(pres, "tablet-3", code="000000", signer=node_signer)
    code = presence.code(SECRET, "control_room", datetime.now(UTC))
    token = _mint(pres, "tablet-3", code=code, signer=node_signer)
    assert grants.read(token, keys(node_signer))["presence"]["proof"] == "location_code"
    rec = sign_with(pres, token, node_signer).record
    assert rec["assurance"]["presence"] == {"location": "control_room", "proof": "location_code"}


def test_a_device_enrolled_elsewhere_cannot_sign_here(room, node_signer):
    code = presence.code(SECRET, "control_room", datetime.now(UTC))
    with pytest.raises(GrantRefused, match="presence_required"):
        _mint(_check(), "office-pc", code=code, signer=node_signer)


def test_an_unenrolled_device_is_a_personal_session_with_no_location(room, node_signer):
    code = presence.code(SECRET, "control_room", datetime.now(UTC))
    with pytest.raises(GrantRefused, match="presence_required"):
        _mint(_check(), "some-laptop", code=code, signer=node_signer)


def test_a_retired_device_is_no_longer_enrolled(room, node_signer):
    devices.retire("console-a", by="@admin:site-a")
    with pytest.raises(GrantRefused, match="presence_required"):
        _mint(_check(), "console-a", signer=node_signer)


def test_a_phone_enrolled_as_a_phone_stays_a_phone(attest_db, node_signer):
    devices.enroll(site_id=SITE, device_id="p1", device_class="phone", location=None,
                   mobility="portable", by="@admin:site-a")  # fmt: skip
    pres = presented()
    with pytest.raises(GrantRefused, match="phone"):
        grants.mint(pres.presentation_id, auth=auth(),
                    device=Device(device_id="p1", device_class="personal"), signer=node_signer)  # fmt: skip


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"device_class": "toaster"}, "device class"),
        ({"mobility": "rolling"}, "mobility"),
        ({"location": "Control Room"}, "location"),
    ],
)
def test_enrolment_is_validated(attest_db, kw, message):
    base = dict(site_id=SITE, device_id="d", device_class="kiosk", location="control_room",
                mobility="fixed", by="@admin:site-a")  # fmt: skip
    with pytest.raises(ValueError, match=message):
        devices.enroll(**{**base, **kw})


# -- a browser proves it is an enrolled device --------------------------------------------


def test_a_device_claims_its_enrolment_once_with_a_code(attest_db, node_signer):
    devices.enroll(site_id=SITE, device_id="console-b", device_class="kiosk",
                   location="control_room", mobility="fixed", by="@admin:site-a")  # fmt: skip
    code = devices.issue_claim_code("console-b")
    token = devices.redeem_claim("console-b", code, signer=node_signer)
    assert devices.read_device_token(token, keys(node_signer)) == ("console-b", SITE)
    with pytest.raises(ValueError, match="claim"):
        devices.redeem_claim("console-b", code, signer=node_signer)


def test_a_wrong_or_expired_claim_code_is_refused(attest_db, node_signer, monkeypatch):
    devices.enroll(site_id=SITE, device_id="console-c", device_class="kiosk",
                   location="control_room", mobility="fixed", by="@admin:site-a")  # fmt: skip
    code = devices.issue_claim_code("console-c")
    with pytest.raises(ValueError, match="claim"):
        devices.redeem_claim("console-c", "WRONG-CODE", signer=node_signer)
    later = datetime.now(UTC) + devices.CLAIM_TTL + timedelta(seconds=1)
    monkeypatch.setattr(devices, "_now", lambda: later)
    with pytest.raises(ValueError, match="claim"):
        devices.redeem_claim("console-c", code, signer=node_signer)


def test_a_device_token_for_a_retired_device_is_not_enrolled(attest_db, node_signer):
    devices.enroll(site_id=SITE, device_id="console-d", device_class="kiosk",
                   location="control_room", mobility="fixed", by="@admin:site-a")  # fmt: skip
    token = devices.redeem_claim(
        "console-d", devices.issue_claim_code("console-d"), signer=node_signer
    )
    devices.retire("console-d", by="@admin:site-a")
    device_id, site = devices.read_device_token(token, keys(node_signer))
    assert devices.get(device_id, site) is None


def test_a_forged_device_token_is_refused(attest_db, node_signer):
    from axiom.attest.chain import Ed25519Signer
    from axiom.vega.identity.keypair import generate_keypair

    other = Ed25519Signer(node_signer.key_id, generate_keypair())
    devices.enroll(site_id=SITE, device_id="console-e", device_class="kiosk",
                   location="control_room", mobility="fixed", by="@admin:site-a")  # fmt: skip
    forged = devices.redeem_claim("console-e", devices.issue_claim_code("console-e"), signer=other)
    with pytest.raises(ValueError, match="signature"):
        devices.read_device_token(forged, keys(node_signer))
