# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The browser path end to end (ADR-146): a gate session and a device cookie
become a signing grant, and the grant signs. Real webgate router, real
session tokens, real Postgres."""

from __future__ import annotations

import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from axiom.extensions.builtins.attest import devices, registry, service, signing  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import parse_logbook  # noqa: E402
from axiom.extensions.builtins.webgate.api.routers import build_webgate_router  # noqa: E402
from axiom.extensions.builtins.webgate.api.signing import DEVICE_COOKIE  # noqa: E402
from axiom.webauth import SESSION_COOKIE  # noqa: E402
from axiom.webauth.keys import reset_key_store_for_tests  # noqa: E402
from axiom.webauth.session import issue_session_token  # noqa: E402
from axiom.webauth.users import InMemoryUserStore, User  # noqa: E402

from .test_grants import keys  # noqa: E402
from .test_signing import SITE, _round_check, person  # noqa: E402

BASE = "http://gate.example"
OP1 = User(user_id="op1", email="op1@example.org", name="Op One", roles=("user",), site=SITE,
           attributes={"idp": "entra"})  # fmt: skip


@pytest.fixture
def gate(attest_db, node_signer):
    reset_key_store_for_tests()
    signing.set_provider(lambda: node_signer)
    app = fastapi.FastAPI()
    app.include_router(build_webgate_router(InMemoryUserStore(), secure_cookies=False))
    client = TestClient(app, base_url=BASE, follow_redirects=False)
    yield client
    signing.reset_provider()
    reset_key_store_for_tests()


def signed_in(client, *, auth_age=0, amr=("pwd",)):
    token = issue_session_token(OP1, issuer=BASE, amr=amr, auth_time=int(time.time()) - auth_age)
    client.cookies.set(SESSION_COOKIE, token)
    return client


def answered(draft_id=None):
    pres = service.present(draft_id or _round_check(), modality="screen")
    service.respond(pres.presentation_id, principal=person().principal, answer="sign", via="screen")
    return pres


def test_a_session_gets_a_grant_and_the_grant_signs(gate, node_signer):
    pres = answered()
    r = signed_in(gate).post("/gate/grants", json={"presentation_id": pres.presentation_id,
                                                   "console_id": "console-1"})  # fmt: skip
    assert r.status_code == 200, r.text
    rec = service.sign(
        pres.presentation_id,
        signatory=person(),
        signer=node_signer,
        grant=r.json()["grant"],
        grant_keys=keys(node_signer),
    ).record
    a = rec["assurance"]
    assert (a["posture"], a["idp"], a["amr"]) == ("sso", "entra", ["pwd"])
    assert a["device_class"] == "personal" and a["device_id"] == "session:op1"
    assert a["console_id"] == "console-1"


def test_no_session_no_grant(gate):
    pres = answered()
    r = gate.post("/gate/grants", json={"presentation_id": pres.presentation_id})
    assert r.status_code == 401 and r.json()["code"] == "no_session"


def test_someone_elses_draft_gets_no_grant(gate):
    other = User(user_id="op2", email="op2@example.org", name="Op Two", roles=("user",), site=SITE)
    gate.cookies.set(SESSION_COOKIE, issue_session_token(other, issuer=BASE))
    r = gate.post("/gate/grants", json={"presentation_id": answered().presentation_id})
    assert r.status_code == 403 and r.json()["code"] == "not_for_you"


def test_an_old_sign_in_is_told_how_fresh_to_sign_in_again(gate):
    registry.register(
        parse_logbook(
            {
                "logbook": {"id": "fresh_log", "version": "1", "display": "Fresh"},
                "type": [
                    {
                        "id": "RELEASE",
                        "meanings": ["approved"],
                        "roles": ["operator"],
                        "assurance": {"posture": "sso", "fresh_within": "5m"},
                        "confirm": {"modalities_any": ["screen"]},
                    }
                ],
            },
            source="test:fresh",
        )
    )
    d = service.create_draft(
        site_id=SITE, logbook="fresh_log", entry_type="RELEASE", meaning="approved",
        content={"title": "Release", "fields": {}}, origin="human", for_principal="@op1:site-a",
    )  # fmt: skip
    pres = answered(d)
    r = signed_in(gate, auth_age=3600).post(
        "/gate/grants", json={"presentation_id": pres.presentation_id}
    )
    assert r.status_code == 403
    assert r.json()["code"] == "reauth_required" and r.json()["max_age"] == 300
    r = signed_in(gate, auth_age=10).post(
        "/gate/grants", json={"presentation_id": pres.presentation_id}
    )
    assert r.status_code == 200, r.text


def test_a_claimed_device_signs_as_what_it_was_enrolled_as(gate, node_signer):
    devices.enroll(site_id=SITE, device_id="console-a", device_class="kiosk",
                   location="control_room", mobility="fixed", by="@admin:site-a")  # fmt: skip
    code = devices.issue_claim_code("console-a")
    r = gate.post("/gate/devices/claim", json={"device_id": "console-a", "code": code})
    assert r.status_code == 200, r.text
    assert DEVICE_COOKIE in gate.cookies
    pres = answered()
    g = signed_in(gate).post("/gate/grants", json={"presentation_id": pres.presentation_id})
    assert g.status_code == 200, g.text
    from axiom.extensions.builtins.attest import grants

    claims = grants.read(g.json()["grant"], keys(node_signer))
    assert (claims["device_id"], claims["device_class"]) == ("console-a", "kiosk")


def test_a_wrong_claim_code_sets_no_device_cookie(gate):
    devices.enroll(site_id=SITE, device_id="console-z", device_class="kiosk",
                   location="control_room", mobility="fixed", by="@admin:site-a")  # fmt: skip
    devices.issue_claim_code("console-z")
    r = gate.post("/gate/devices/claim", json={"device_id": "console-z", "code": "nope"})
    assert r.status_code == 403 and r.json()["code"] == "invalid_claim"
    assert DEVICE_COOKIE not in gate.cookies


def test_a_garbage_device_cookie_is_a_personal_session(gate, node_signer):
    gate.cookies.set(DEVICE_COOKIE, "not.a-token")
    g = signed_in(gate).post("/gate/grants", json={"presentation_id": answered().presentation_id})
    assert g.status_code == 200
    from axiom.extensions.builtins.attest import grants

    assert grants.read(g.json()["grant"], keys(node_signer))["device_class"] == "personal"
