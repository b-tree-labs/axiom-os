# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""IDENT-1 / AEOS-ID-1: ctx.principal is always populated; default is the `open`
posture (unproven, OS-derived) — today's free-wheeling, now explicit."""

from __future__ import annotations

import logging
from pathlib import Path

from axiom.infra.principal import (
    PrincipalContext,
    effective_floor,
    node_posture,
    open_principal,
    principal_provenance,
)
from axiom.infra.skills import SkillContext


def test_open_principal_is_unproven_and_os_derived():
    p = open_principal()
    assert p.posture == "open"
    assert p.assured is False
    assert p.public_bytes is None
    assert p.handle.startswith("@") and p.handle.endswith(":local")


def test_posture_assurance_ladder():
    open_p = PrincipalContext("@x:local", "open")
    sso_p = PrincipalContext("@x:example-org", "sso", assured=True)
    assert open_p.meets("open")
    assert not open_p.meets("attested")          # open can't satisfy a higher floor
    assert sso_p.meets("attested") and sso_p.meets("sso")


def test_skillcontext_always_has_a_principal_defaulting_open():
    # Constructed the way existing code does (no principal passed) — must not break.
    ctx = SkillContext(registry=None, state_dir=Path("/tmp"), logger=logging.getLogger("t"))
    assert ctx.principal.posture == "open"
    assert ctx.principal.assured is False


def test_node_posture_defaults_open_and_env_overrides(monkeypatch):
    monkeypatch.delenv("AXIOM_IDENTITY_POSTURE", raising=False)
    assert node_posture() == "open"                          # default free-wheeling
    monkeypatch.setenv("AXIOM_IDENTITY_POSTURE", "sso")
    assert node_posture() == "sso"                           # an institution's deploy
    monkeypatch.setenv("AXIOM_IDENTITY_POSTURE", "bogus")
    assert node_posture() == "open"                          # invalid -> safe default


def test_effective_floor_is_the_max_of_node_resource_and_federation():
    assert effective_floor("open", None) == "open"
    assert effective_floor("open", "attested") == "attested"       # a real cred lifts an open node
    assert effective_floor("sso", "attested") == "sso"             # node already higher
    assert effective_floor("open", None, "sso") == "sso"           # a cohort floor lifts it
    assert effective_floor("attested", "open", "sso") == "sso"     # federation wins the max


def test_principal_provenance_labels_assurance():
    prov = principal_provenance(PrincipalContext("@ben:local", "open"))
    assert prov == {"principal": "@ben:local", "posture": "open", "assured": False}


def test_federation_policy_enforces_posture_and_required_idp():
    from axiom.infra.principal import FederationPolicy

    ut = FederationPolicy(min_posture="sso", allowed_idps=("entra",))
    below = PrincipalContext("@x:local", "open")
    wrong_idp = PrincipalContext("@x:google", "sso", assured=True, idp="google")
    ut_user = PrincipalContext("@ben:example-org", "sso", assured=True, idp="entra")

    assert ut.admits(below)[0] is False                      # below the floor
    assert "below the cohort floor" in ut.admits(below)[1]
    assert ut.admits(wrong_idp)[0] is False                  # right posture, wrong IdP
    assert "identity provider" in ut.admits(wrong_idp)[1]
    assert ut.admits(ut_user) == (True, None)                # right posture + IdP

    open_cohort = FederationPolicy()                         # any posture, any IdP
    assert open_cohort.admits(below) == (True, None)
