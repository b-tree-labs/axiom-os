# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S12 Denial at every door (#1818): each caller who may not read gets the same status and the
same reason shape at every read door, with no partial content and no count; no signing door
accepts software or administration."""

from __future__ import annotations

import pytest

from .conftest import (
    ANN,
    BO,
    CY,
    DEE,
    LOG,
    SITE,
    TOKEN,
    as_,
    check,
    draft,
    open_session,
    sign_cli,
    signatory,
)


def _doors(rec_id: str):
    q = f"?site={SITE}"
    return [
        ("GET", f"/api/v1/attest/{LOG}/records{q}"),
        ("GET", f"/api/v1/attest/{LOG}/records/{rec_id}{q}"),
        ("GET", f"/api/v1/attest/{LOG}/intervals{q}"),
        ("GET", f"/api/v1/attest/{LOG}/obligations{q}"),
        ("POST", f"/api/v1/attest/{LOG}/verify{q}"),
        ("GET", f"/api/v1/attest/{LOG}/stream{q}&once=1"),
        ("GET", f"/api/v1/attest/{LOG}/exports{q}"),
        ("POST", f"/api/v1/attest/{LOG}/exports{q}"),
    ]


def _call(client, method, url, headers=None):
    if method == "POST":
        return client.post(url, json={"formats": ["text"]}, headers=headers or {})
    return client.get(url, headers=headers or {})


def _answers(world, web, helper):
    open_session(world)
    rec = check(world)
    callers = {
        "another site's person": (lambda: as_(web, CY), None),
        "no one signed in": (lambda: as_(web, None), None),
        "a key without the read scope": (
            lambda: as_(web, None),
            {"Authorization": f"Bearer {TOKEN}"},
        ),
    }
    out = {}
    for name, (sign_in, headers) in callers.items():
        sign_in()
        out[name] = [
            (url, _call(web, method, url, headers)) for method, url in _doors(rec["attestation_id"])
        ]
    return out


@pytest.mark.acceptance("LB-E2E-A24")
def test_lb_e2e_a24_every_read_door_refuses_with_a_reason_and_no_content(world, web, helper):
    for name, answers in _answers(world, web, helper).items():
        for url, r in answers:
            body = r.json()
            assert r.status_code in (401, 403, 404), (name, url, r.status_code)
            assert set(body) == {"detail", "code"}, (name, url, body)


@pytest.mark.acceptance("LB-E2E-A24")
def test_lb_e2e_a24_each_caller_is_refused_the_same_way_at_every_door(world, web, helper):
    # The owner's rule (#1886): a session that belongs to another site gets 403 "site" at every
    # door; 404 "not_found" only for a log the person may not read at their own site.
    for name, answers in _answers(world, web, helper).items():
        shapes = {(r.status_code, r.json()["code"]) for _url, r in answers}
        assert len(shapes) == 1, (name, sorted(shapes))
        if name == "another site's person":
            assert shapes == {(403, "site")}


@pytest.mark.acceptance("LB-E2E-A24")
def test_lb_e2e_a24_a_refused_read_is_recorded_once(world, web):
    # #1885: each refused read is one record at its door: who, which door, why; no content.
    from axiom.extensions.builtins.attest import reads

    from .conftest import access_records

    note = sign_cli(ANN, "NOTE", {"text": "x"})
    as_(web, CY).get(f"/api/v1/attest/{LOG}/records?site={SITE}")
    denials = [r for r in access_records(world.state_dir) if r.get("outcome") == "denied"]
    assert len(denials) == 1 and denials[0]["principal"] == CY.handle
    a = denials[0]["access"]
    assert (a["what"], a["refused"], a["ids"], a["count"]) == ("records", "site", [], 0)
    for method, url in _doors(note["attestation_id"])[1:]:
        _call(as_(web, CY), method, url)
    doors = [r["access"]["what"] for r in access_records(world.state_dir)
             if r.get("outcome") == "denied"]  # fmt: skip
    assert doors == ["records", "record", "intervals", "obligations", "verify", "stream",
                     "export", "export"]  # fmt: skip
    listed = reads.access_records(world.state_dir, logbook=LOG)
    assert {(r["outcome"], r["refused"]) for r in listed} == {("denied", "site")}


@pytest.mark.acceptance("LB-E2E-A24")
@pytest.mark.xfail(
    strict=True,
    reason="needs #1758 (LD-1): the door list read from its registry and the matrix generated",
)
def test_lb_e2e_a24_the_door_matrix_is_generated_from_the_registry():
    from axiom.extensions.builtins.attest import doors  # noqa: F401


@pytest.mark.acceptance("LB-E2E-A25")
def test_lb_e2e_a25_no_signing_door_accepts_software_or_administration(world):
    from axiom.extensions.builtins.attest import service, signing
    from axiom.extensions.builtins.attest.skills import SPECS

    note = sign_cli(ANN, "NOTE", {"text": "to act on"})
    for make in (
        lambda: draft(DEE, "NOTE", {"text": "an admin's note"}),
        lambda: service.create_ack_draft(site_id=SITE, target=note["attestation_id"], origin="human",
                                         for_principal=DEE.handle),
        lambda: service.create_seal_draft(site_id=SITE, logbook=LOG, summary="x", origin="human",
                                          for_principal=DEE.handle, from_seq=1, to_seq=note["seq"]),
    ):  # fmt: skip
        with pytest.raises(service.AttestRefused):
            d = make()
            pres = service.present(d)
            service.respond(
                pres.presentation_id, principal=signatory(DEE).principal, answer="sign", via="cli"
            )
            service.sign(pres.presentation_id, signatory=signatory(DEE), signer=signing.signer())
    signing_skills = {
        "attest.new",
        "attest.sign",
        "attest.supplement",
        "attest.ack",
        "attest.cosign",
        "attest.seal",
    }
    by = {s.name: s for s in SPECS}
    for name in signing_skills:
        assert by[name].surfaces == ("cli",), name
    assert BO.roles == ("lead",)
