# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-084 — ActorContext and the assurance ladder.

Resolution is *deterministic from verified claims*: no lookups, no I/O, no
clock. That is the property that lets it sit at the identity boundary and keeps
``decide()`` free of them.
"""

from __future__ import annotations

import pytest

from axiom.governance.actor import (
    ACR_BY_POSTURE,
    AAL_BY_POSTURE,
    ActorContext,
    Assurance,
    resolve_actor,
)
from axiom.infra.principal import PrincipalContext


class TestAssuranceLadder:
    def test_posture_maps_to_nist_aal(self):
        """The normative table ADR-084 asks for: posture ↔ AAL ↔ acr."""
        assert AAL_BY_POSTURE["open"] == 0
        assert AAL_BY_POSTURE["attested"] == 1
        assert AAL_BY_POSTURE["sso"] == 2
        assert AAL_BY_POSTURE["service"] == 2

    def test_every_posture_has_an_acr(self):
        from axiom.infra.principal import POSTURES

        assert set(ACR_BY_POSTURE) == set(POSTURES)

    def test_assurance_is_ordered_so_floors_can_be_compared(self):
        assert Assurance(posture="sso").meets("attested") is True
        assert Assurance(posture="attested").meets("sso") is False

    def test_open_posture_is_not_an_authenticated_claim(self):
        """`open` is a named absence of proof, not a weak proof."""
        assert Assurance(posture="open").aal == 0
        assert Assurance(posture="open").authenticated is False
        assert Assurance(posture="sso").authenticated is True


class TestResolveActor:
    CLAIMS = {
        "sub": "oid-1",
        "tid": "tenant-a",
        "roles": ["operator"],
        "acr": "urn:example:sso",
        "amr": ["pwd", "mfa"],
        "auth_time": 1700.0,
    }

    def test_resolves_from_verified_claims(self):
        actor = resolve_actor(self.CLAIMS, handle="@ben:netl", posture="sso")
        assert actor.handle == "@ben:netl"
        assert actor.tenant == "tenant-a"
        assert actor.roles == ("operator",)
        assert actor.assurance.posture == "sso"
        assert actor.assurance.amr == ("pwd", "mfa")
        assert actor.assurance.auth_time == 1700.0

    def test_claim_acr_wins_over_the_derived_default(self):
        """The IdP's own acr is more informative than our posture mapping."""
        actor = resolve_actor(self.CLAIMS, handle="@ben:netl", posture="sso")
        assert actor.assurance.acr == "urn:example:sso"

    def test_absent_acr_falls_back_to_the_posture_table(self):
        claims = {k: v for k, v in self.CLAIMS.items() if k != "acr"}
        actor = resolve_actor(claims, handle="@ben:netl", posture="sso")
        assert actor.assurance.acr == ACR_BY_POSTURE["sso"]

    def test_roles_may_be_supplied_out_of_band(self):
        """Group claims overflow, so roles can come from the directory seam."""
        actor = resolve_actor(
            {"sub": "oid-1"}, handle="@ben:netl", posture="sso", roles=("researcher",)
        )
        assert actor.roles == ("researcher",)

    def test_supplied_roles_win_over_claim_roles(self):
        """The directory is authoritative when consulted; claims were the fallback."""
        actor = resolve_actor(
            self.CLAIMS, handle="@ben:netl", posture="sso", roles=("student",)
        )
        assert actor.roles == ("student",)

    def test_is_deterministic(self):
        """No clock, no I/O — the same claims always yield the same actor."""
        a = resolve_actor(self.CLAIMS, handle="@ben:netl", posture="sso")
        b = resolve_actor(self.CLAIMS, handle="@ben:netl", posture="sso")
        assert a == b

    def test_unknown_posture_is_rejected_rather_than_silently_downgraded(self):
        with pytest.raises(ValueError):
            resolve_actor(self.CLAIMS, handle="@ben:netl", posture="superuser")


class TestPrincipalContextBridge:
    def test_round_trips_from_the_existing_principal_context(self):
        """ADR-084 folds the posture ladder in without breaking its API."""
        pc = PrincipalContext(handle="@ben:netl", posture="sso", assured=True, idp="entra")
        actor = ActorContext.from_principal_context(pc, tenant="netl", roles=("operator",))
        assert actor.handle == "@ben:netl"
        assert actor.assurance.posture == "sso"
        assert actor.roles == ("operator",)
        assert actor.attributes["idp"] == "entra"

    def test_projects_back_so_existing_consumers_keep_working(self):
        pc = PrincipalContext(handle="@ben:netl", posture="attested", assured=True)
        actor = ActorContext.from_principal_context(pc)
        back = actor.to_principal_context()
        assert back.handle == pc.handle
        assert back.posture == pc.posture
        assert back.meets("attested") is True


class TestEnvelopeCarriesTheActor:
    def test_envelope_accepts_an_actor_context_without_changing_actor(self):
        """Additive on purpose: `actor` stays the bare Principal so receipts and
        capability signatures are byte-identical. Promoting the field itself is a
        separate sweep of its consumers."""
        from axiom.governance.envelope import ActionEnvelope
        import dataclasses

        fields = {f.name for f in dataclasses.fields(ActionEnvelope)}
        assert "actor_context" in fields
        assert "actor" in fields
