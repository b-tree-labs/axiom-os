# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S2 Draft and propose (#1818): an agent proposes over HTTP with its key, only the person sees
it, no reading rides along, and the journal holds a reference, never the content."""

from __future__ import annotations

import json

import pytest

from .conftest import ANN, BO, LOG, as_, as_helper, open_session

BODY = {"logbook": LOG, "entry_type": "CHECK", "meaning": "performed", "title": "Check",
        "fields": {}, "for_principal": ANN.handle}  # fmt: skip


@pytest.mark.acceptance("LB-E2E-A3")
def test_lb_e2e_a3_a_proposal_reaches_only_its_person_with_no_reading(world, web, helper, live):
    from axiom.extensions.builtins.journal import rights
    from axiom.extensions.builtins.journal import service as journal

    open_session(world)
    r = web.post("/api/v1/attest/proposals", json=BODY, headers=as_helper(web))
    assert r.status_code == 201, r.text
    draft_id = r.json()["draft_id"]
    assert [d["draft_id"] for d in as_(web, ANN).get("/api/v1/attest/drafts").json()["drafts"]] == [
        draft_id
    ]
    assert as_(web, BO).get("/api/v1/attest/drafts").json()["drafts"] == []
    refused = web.post("/api/v1/attest/proposals", headers=as_helper(web),
                       json={**BODY, "fields": {"readings": {"flow": "1"}}})  # fmt: skip
    assert refused.status_code == 422 and "observe" in refused.json()["detail"]
    rows = journal.query(rights.Caller(handle=ANN.handle, sites=None), lanes=["attestations"])[
        "rows"
    ]
    created = [x for x in rows if x["subject"].endswith("draft_created")]
    assert len(created) == 1 and draft_id in json.dumps(created[0]["ref"])
