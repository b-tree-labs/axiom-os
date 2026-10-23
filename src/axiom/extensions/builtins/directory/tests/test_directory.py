# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-103 — directory providers. Each test names the decision it pins.

No network: the entra adapter takes an injected Graph client, mirroring the
calendar vendors' injectable-client pattern.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.directory import (
    DirectoryCapability,
    GroupRef,
    GroupRoleMap,
    MembershipResolver,
    PrincipalRef,
    available_providers,
    get_provider,
)
from axiom.extensions.builtins.directory.providers import (
    EntraDirectory,
    LocalDirectory,
    OidcClaimsDirectory,
)

BEN = PrincipalRef(subject="oid-111", provider="entra", display="Ben")


# --------------------------------------------------------------------------
# Decision 1 — the port, with capability negotiation
# --------------------------------------------------------------------------


class TestCapabilityNegotiation:
    def test_a_provider_that_cannot_enumerate_is_first_class(self):
        """Decision 1: callers negotiate rather than assume."""
        claims = OidcClaimsDirectory(claims={"groups": ["g-ops"]})
        assert DirectoryCapability.LOOKUP in claims.capabilities
        assert DirectoryCapability.ENUMERATE not in claims.capabilities
        # ...and asking anyway is a clean refusal, not an AttributeError.
        with pytest.raises(NotImplementedError):
            list(claims.members_of(GroupRef(id="g-ops", provider="oidc_claims")))

    def test_local_provider_can_enumerate(self, tmp_path):
        f = tmp_path / "dir.json"
        f.write_text(json.dumps({"groups": {"g-ops": {"members": ["oid-111"]}}}))
        local = LocalDirectory(path=f)
        assert DirectoryCapability.ENUMERATE in local.capabilities
        members = list(local.members_of(GroupRef(id="g-ops", provider="local")))
        assert [m.subject for m in members] == ["oid-111"]


# --------------------------------------------------------------------------
# Decision 2 — day-one adapters, and `local` as the floor (ADR-022)
# --------------------------------------------------------------------------


class TestAdapters:
    def test_registry_exposes_the_three_day_one_adapters(self):
        assert {"entra", "oidc_claims", "local"} <= set(available_providers())

    def test_local_works_with_no_external_authority(self, tmp_path):
        """ADR-022: identity must work when no external authority is present."""
        f = tmp_path / "dir.json"
        f.write_text(json.dumps({"groups": {"g-ops": {"members": ["oid-111"]}}}))
        provider = get_provider("local", path=f)
        assert [g.id for g in provider.groups_for(BEN)] == ["g-ops"]

    def test_entra_reads_memberof_via_injected_client(self):
        calls = []

        class FakeGraph:
            def get(self, path):
                calls.append(path)
                return {"value": [{"id": "g-ops", "displayName": "Operators"}]}

        entra = EntraDirectory(client=FakeGraph())
        groups = entra.groups_for(BEN)
        assert [g.id for g in groups] == ["g-ops"]
        assert "/users/oid-111/memberOf" in calls[0]

    def test_entra_paginates(self):
        pages = [
            {"value": [{"id": "g1"}], "@odata.nextLink": "https://graph/next"},
            {"value": [{"id": "g2"}]},
        ]

        class FakeGraph:
            def get(self, path):
                return pages.pop(0)

        assert [g.id for g in EntraDirectory(client=FakeGraph()).groups_for(BEN)] == [
            "g1",
            "g2",
        ]


# --------------------------------------------------------------------------
# Decision 5 — claims-first, and the overage case that forces a call
# --------------------------------------------------------------------------


class TestClaimsFirst:
    def test_complete_claims_need_no_directory_call(self):
        """Decision 5: zero extra network calls when the IdP emits groups."""
        calls = []

        class CountingDirectory(LocalDirectory):
            def groups_for(self, principal):
                calls.append(principal)
                return []

        resolver = MembershipResolver(
            provider=CountingDirectory(groups={}), role_map=GroupRoleMap.permissive()
        )
        m = resolver.resolve(BEN, claims={"groups": ["g-ops"]})
        assert [g.id for g in m.groups] == ["g-ops"]
        assert calls == [], "directory must not be consulted when claims are complete"

    def test_claims_overage_forces_a_directory_call(self):
        """Decision 5 + Context: past the token size limit Entra sends a pointer."""
        provider = LocalDirectory(groups={"g-ops": ["oid-111"], "g-eng": ["oid-111"]})
        resolver = MembershipResolver(
            provider=provider, role_map=GroupRoleMap.permissive()
        )
        overage = {
            "_claim_names": {"groups": "src1"},
            "_claim_sources": {"src1": {"endpoint": "https://graph/…"}},
        }
        m = resolver.resolve(BEN, claims=overage)
        assert {g.id for g in m.groups} == {"g-ops", "g-eng"}
        assert m.source == "directory"

    def test_every_resolution_is_stamped(self):
        resolver = MembershipResolver(
            provider=LocalDirectory(groups={}), role_map=GroupRoleMap.permissive()
        )
        m = resolver.resolve(BEN, claims={"groups": []}, now=1000.0)
        assert m.as_of == 1000.0


# --------------------------------------------------------------------------
# Decision 12 — failure degrades toward LESS authority, never more
# --------------------------------------------------------------------------


class TestDegradation:
    def _boom(self):
        class Boom(LocalDirectory):
            def groups_for(self, principal):
                raise ConnectionError("directory unreachable")

        return Boom(groups={})

    def test_within_ttl_the_cached_projection_is_used(self):
        provider = LocalDirectory(groups={"g-ops": ["oid-111"]})
        resolver = MembershipResolver(
            provider=provider, role_map=GroupRoleMap.permissive(), ttl=100.0
        )
        first = resolver.resolve(BEN, claims={}, now=0.0)
        assert [g.id for g in first.groups] == ["g-ops"]
        resolver._provider = self._boom()  # directory dies
        second = resolver.resolve(BEN, claims={}, now=50.0)
        assert [g.id for g in second.groups] == ["g-ops"]
        assert second.stale is False

    def test_past_ttl_and_unreachable_does_not_serve_stale_authority(self):
        """Decision 12: NOT 'last known good, indefinitely'.

        The cache holds two groups. Past TTL with the directory dead, the
        resolver must fall back to what the *token* carries — here nothing —
        rather than keep serving authority nobody can re-confirm.
        """
        provider = LocalDirectory(groups={"g-ops": ["oid-111"], "g-admin": ["oid-111"]})
        resolver = MembershipResolver(
            provider=provider, role_map=GroupRoleMap.permissive(), ttl=10.0
        )
        warm = resolver.resolve(BEN, claims={}, now=0.0)
        assert {g.id for g in warm.groups} == {"g-ops", "g-admin"}

        resolver._provider = self._boom()
        m = resolver.resolve(BEN, claims={}, now=999.0)
        assert m.groups == (), "expired cache must not keep conferring authority"
        assert m.stale is True
        assert m.source == "claims-degraded"

    def test_a_directory_failure_can_only_remove_roles_never_add(self):
        """Overage forces a directory call; when it fails, nothing is invented."""
        overage = {
            "_claim_names": {"groups": "src1"},
            "_claim_sources": {"src1": {"endpoint": "https://graph/…"}},
        }
        resolver = MembershipResolver(
            provider=self._boom(), role_map=GroupRoleMap.permissive(), ttl=10.0
        )
        m = resolver.resolve(BEN, claims=overage, now=0.0)
        assert m.groups == () and m.roles == ()
        assert m.stale is True

    def test_no_cache_no_claims_unreachable_yields_nothing(self):
        """Fail toward zero authority, not toward a default."""
        resolver = MembershipResolver(
            provider=self._boom(), role_map=GroupRoleMap.permissive()
        )
        m = resolver.resolve(BEN, claims={}, now=0.0)
        assert m.groups == () and m.roles == ()


# --------------------------------------------------------------------------
# Decision 8 — group→role mapping is external config, first match wins
# --------------------------------------------------------------------------


class TestGroupRoleMap:
    RULES = [
        {"group": "ENGR-*-Operators", "role": "operator"},
        {"group": "ENGR-*", "role": "researcher"},
    ]

    def test_first_match_wins(self):
        m = GroupRoleMap(rules=self.RULES, default=())
        assert m.roles_for(["ENGR-UT-Operators"]) == ("operator",)

    def test_falls_through_to_the_later_rule(self):
        m = GroupRoleMap(rules=self.RULES, default=())
        assert m.roles_for(["ENGR-UT-Students"]) == ("researcher",)

    def test_unmatched_group_gets_the_conservative_default(self):
        m = GroupRoleMap(rules=self.RULES, default=())
        assert m.roles_for(["SOME-OTHER-GROUP"]) == ()

    def test_default_is_empty_not_permissive(self):
        """A group we do not recognise must not confer authority."""
        assert GroupRoleMap(rules=[], default=()).roles_for(["anything"]) == ()

    def test_roles_are_deduped_and_ordered(self):
        m = GroupRoleMap(rules=self.RULES, default=())
        assert m.roles_for(["ENGR-A-Operators", "ENGR-B-Operators", "ENGR-C"]) == (
            "operator",
            "researcher",
        )

    def test_loads_from_json_config(self, tmp_path):
        f = tmp_path / "roles.json"
        f.write_text(json.dumps({"default": [], "rules": self.RULES}))
        assert GroupRoleMap.from_file(f).roles_for(["ENGR-UT-Operators"]) == (
            "operator",
        )


# --------------------------------------------------------------------------
# Decision 11 — identity keyed on the immutable subject, never email
# --------------------------------------------------------------------------


class TestSubjectKeying:
    def test_principal_identity_is_the_subject_not_the_display(self):
        a = PrincipalRef(subject="oid-111", provider="entra", display="Ben Booth")
        b = PrincipalRef(subject="oid-111", provider="entra", display="B. Booth")
        assert a == b and hash(a) == hash(b)

    def test_cache_key_survives_a_display_name_change(self):
        provider = LocalDirectory(groups={"g-ops": ["oid-111"]})
        resolver = MembershipResolver(
            provider=provider, role_map=GroupRoleMap.permissive(), ttl=100.0
        )
        resolver.resolve(PrincipalRef("oid-111", "entra", "Ben Booth"), claims={}, now=0.0)
        resolver._provider = None  # any lookup would explode
        m = resolver.resolve(
            PrincipalRef("oid-111", "entra", "Benjamin Booth"), claims={}, now=1.0
        )
        assert [g.id for g in m.groups] == ["g-ops"]
