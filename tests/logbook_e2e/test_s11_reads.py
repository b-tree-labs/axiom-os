# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S11 Reads are recorded (#1818): across the run, each read of records wrote exactly one access
record naming the person, the surface and the range, and no content; metadata reads wrote none;
the access log reads newest first; the audit chain verifies."""

from __future__ import annotations

import json
import logging

import pytest

from .conftest import ANN, LOG, SITE, access_records, as_, check, open_session, sign_cli


@pytest.mark.acceptance("LB-E2E-A23")
def test_lb_e2e_a23_every_read_is_one_record_with_no_content(world, web, tmp_path):
    from axiom.extensions.builtins.attest import exports
    from axiom.extensions.builtins.attest.skills import bind_default
    from axiom.infra import audit_trail
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skills import SkillContext

    open_session(world)
    rec = check(world)
    sign_cli(ANN, "NOTE", {"text": "a secret-free note"})
    exports.set_storage(None, base_dir=tmp_path / "storage")
    c = as_(web, ANN)
    before = len(access_records(world.state_dir))
    assert c.get("/api/v1/attest/logbooks").status_code == 200  # metadata: not a read of records
    assert c.get(f"/api/v1/attest/logbooks/{LOG}").status_code == 200
    assert len(access_records(world.state_dir)) == before
    doors = [
        ("records", lambda: c.get(f"/api/v1/attest/{LOG}/records")),
        ("record", lambda: c.get(f"/api/v1/attest/{LOG}/records/{rec['attestation_id']}")),
        ("intervals", lambda: c.get(f"/api/v1/attest/{LOG}/intervals")),
        ("verify", lambda: c.post(f"/api/v1/attest/{LOG}/verify")),
        ("export", lambda: c.post(f"/api/v1/attest/{LOG}/exports", json={"formats": ["text"]})),
    ]
    try:
        for what, call in doors:
            n = len(access_records(world.state_dir))
            assert call().status_code in (200, 201), what
            new = access_records(world.state_dir)[n:]
            assert len(new) == 1, (what, new)
            assert new[0]["principal"] == ANN.handle and new[0]["access"]["surface"] == "web"
            assert new[0]["access"]["site"] == SITE and new[0]["access"]["logbook"] == LOG
            assert "readings" not in json.dumps(new[0]) and "secret-free" not in json.dumps(new[0])
    finally:
        exports.set_storage(None)
    ctx = SkillContext(registry=bind_default(), state_dir=world.state_dir, logger=logging.getLogger("e2e"),
                       principal=PrincipalContext(handle=ANN.handle, posture="sso", assured=True), surface="mcp")  # fmt: skip
    listed = ctx.registry.invoke("attest.access", {"site": SITE, "logbook": LOG}, ctx)
    assert listed.ok, listed.errors
    times = [r["at"] for r in listed.value["reads"]]
    assert times == sorted(times, reverse=True) and len(times) >= len(doors)
    assert audit_trail.verify_chain(audit_trail.audit_path(world.state_dir))


@pytest.mark.acceptance("LB-E2E-A21")
def test_lb_e2e_a21_a_consumer_label_reaches_every_record_list(world, web):
    # #1802 (server half): a consumer's label for a record rides the brief, so the records route
    # and the stream carry it; the page shows it without knowing what it means. Information tone
    # only: a label claiming success, or a label function that fails, gives no label.
    from axiom.extensions.builtins.attest import presenters, registry

    from .conftest import ANN, LOG, as_, open_session, sign_cli

    def label(brief):
        if brief.get("entry_type") == "OPEN":
            return {"text": "Opened", "tone": "info"}
        if brief.get("entry_type") == "NOTE":
            return {"text": "Done", "tone": "success"}
        raise RuntimeError("no label")

    registry.Inputs().record_label(LOG, label)
    try:
        open_session(world)
        sign_cli(ANN, "NOTE", {"text": "x"})
        sign_cli(ANN, "CLOSE", {"text": "done"})
        rows = as_(web, ANN).get(f"/api/v1/attest/{LOG}/records").json()["records"]
        got = {r["entry_type"]: r["label"] for r in rows}
        assert got == {"OPEN": {"text": "Opened", "tone": "info"}, "NOTE": None, "CLOSE": None}
    finally:
        presenters.unregister_label(LOG)
