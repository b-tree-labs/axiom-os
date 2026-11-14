# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""S17 A record's brief names its signer and carries its fields (Log views design 2026-11-13).

The appkit Log views design (``docs/design/proposed-2026-11-13/log-views/``, paper and list, build
changes 12, 13 and 17) shows who signed by initials on paper and by name in the list (never the
handle), and an entry's fields as one sentence after its type. The brief a list reads carried only
the signer's handle and the title, so a page could do neither without fetching every record. The
brief now carries ``signer_name`` (the name the signer signed as) and ``fields`` (the signed values).
"""

from __future__ import annotations

import pytest

from .conftest import ANN, LOG, SITE, as_, sign_cli


@pytest.mark.acceptance("LV-12-A1")
def test_lv_12_a1_a_brief_names_the_signer_as_they_signed(world, web):
    from axiom.extensions.builtins.attest import service

    rec = sign_cli(ANN, "NOTE", {"text": "Pump noisy at start-up"})
    brief = service.brief(rec)
    assert brief["signer"] == ANN.handle
    assert brief["signer_name"] == ANN.name
    listed = as_(web, ANN).get(f"/api/v1/attest/{LOG}/records").json()["records"]
    assert listed[0]["signer_name"] == ANN.name


@pytest.mark.acceptance("LV-13-A1")
def test_lv_13_a1_a_brief_carries_the_signed_field_values(world, web):
    rec = sign_cli(ANN, "NOTE", {"text": "Room warm after lunch"})
    listed = as_(web, ANN).get(f"/api/v1/attest/{LOG}/records", params={"site": SITE}).json()["records"]
    mine = [r for r in listed if r["attestation_id"] == rec["attestation_id"]][0]
    assert mine["fields"] == {"text": "Room warm after lunch"}
