# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Membership → SubjectContext, the identity-boundary binding.

``governance/subject.py`` states the contract this implements: *"Until
ActorContext lands, a resolver at the identity boundary populates it from
verified token claims."* This is that resolver.

The tests that matter are the two that stop a degraded projection from becoming
authority, and the one that pins prefix agreement with the reconciler — a
mismatch there would silently break every substrate check while every unit test
still passed.
"""

from __future__ import annotations

from axiom.extensions.builtins.directory import (
    GroupRef,
    GroupRoleMap,
    MembershipResolver,
    PrincipalRef,
    RevokedSet,
    subject_from_membership,
)
from axiom.extensions.builtins.directory.providers import LocalDirectory
from axiom.extensions.builtins.directory.reconcile import (
    GROUP_PREFIX,
    MEMBER_RELATION,
    USER_PREFIX,
)
from axiom.extensions.builtins.directory.resolution import Membership

BEN = PrincipalRef(subject="oid-1", provider="entra")


def _membership(**kw) -> Membership:
    base = dict(
        principal=BEN,
        groups=(GroupRef(id="g-ops", provider="entra"),),
        roles=("operator",),
        as_of=100.0,
        stale=False,
        source="directory",
    )
    base.update(kw)
    return Membership(**base)


class TestSubjectBinding:
    def test_fga_user_matches_the_reconciler_prefix(self):
        """A prefix mismatch between writer and reader breaks every check while
        every unit test still passes — so the agreement is pinned here."""
        subject = subject_from_membership(_membership())
        assert subject.fga_user == f"{USER_PREFIX}oid-1"

    def test_groups_ride_as_contextual_tuples(self):
        """Membership is usable at decision time before the store holds anything.

        Contextual tuples are asserted for one decision only, so authorization
        works from the moment resolution does — no dependency on the reconciler
        having run, and no OpenFGA server required to be useful.
        """
        subject = subject_from_membership(_membership())
        assert subject.contextual_tuples == (
            (f"{USER_PREFIX}oid-1", MEMBER_RELATION, f"{GROUP_PREFIX}g-ops"),
        )

    def test_roles_and_provenance_ride_as_attributes(self):
        subject = subject_from_membership(_membership())
        assert subject.attributes["roles"] == ("operator",)
        assert subject.attributes["as_of"] == 100.0
        assert subject.attributes["membership_source"] == "directory"
        assert subject.attributes["membership_stale"] is False

    def test_tenant_is_carried_through(self):
        subject = subject_from_membership(_membership(), tenant="netl")
        assert subject.tenant == "netl"

    # -- the two that stop degraded data becoming authority ----------------

    def test_a_stale_projection_contributes_no_grants(self):
        """Decision 12 applied to binding: a failure may only remove authority.

        Contextual tuples ARE grants. Emitting them from a projection nobody
        could re-confirm would let a directory outage silently extend access,
        which is the same failure the reconciler refuses with
        StaleProjectionError.
        """
        subject = subject_from_membership(_membership(stale=True, source="claims-degraded"))
        assert subject.contextual_tuples == ()
        assert subject.attributes["membership_stale"] is True

    def test_stale_roles_are_still_reported_for_policy_to_judge(self):
        """Withhold the grant, not the fact. Policy needs to see the degradation
        to apply a max-age rule; hiding it would make a degraded decision
        indistinguishable from a fresh one."""
        subject = subject_from_membership(_membership(stale=True))
        assert subject.attributes["roles"] == ("operator",)

    def test_empty_membership_yields_a_subject_that_abstains(self):
        """No groups must mean ABSTAIN at the substrate, never a deny."""
        subject = subject_from_membership(
            _membership(groups=(), roles=(), source="none")
        )
        assert subject.contextual_tuples == ()
        assert subject.fga_user == f"{USER_PREFIX}oid-1"

    def test_end_to_end_a_revoked_group_never_reaches_the_substrate(self):
        """The whole chain: revoked set → resolver → subject → substrate input."""
        revoked = RevokedSet()
        revoked.record("oid-1", "g-ops", now=0.0)
        resolver = MembershipResolver(
            provider=LocalDirectory(groups={}),
            role_map=GroupRoleMap.permissive(),
            revoked=revoked,
        )
        membership = resolver.resolve(
            BEN, claims={"groups": ["g-ops", "g-eng"]}, now=1.0
        )
        subject = subject_from_membership(membership)
        objects = [t[2] for t in subject.contextual_tuples]
        assert f"{GROUP_PREFIX}g-ops" not in objects
        assert f"{GROUP_PREFIX}g-eng" in objects


class TestReachesTheSubstrate:
    """The seam is only real if what it produces arrives at the Check."""

    def test_contextual_tuples_arrive_at_the_openfga_check(self):
        from axiom.extensions.builtins.authz.openfga import (
            FgaCheckSpec,
            OpenFgaSubstrate,
        )
        from axiom.extensions.builtins.authz.substrate import SubstrateDecision

        seen: dict = {}

        class RecordingClient:
            def check(self, *, user, relation, object, contextual_tuples=()):
                seen.update(
                    user=user, relation=relation, object=object,
                    contextual_tuples=contextual_tuples,
                )
                return relation == MEMBER_RELATION

        subject = subject_from_membership(_membership())
        substrate = OpenFgaSubstrate(
            client=RecordingClient(),
            mapper=lambda _env: FgaCheckSpec(
                user=subject.fga_user,
                object=f"{GROUP_PREFIX}g-ops",
                permit_relation=MEMBER_RELATION,
                contextual_tuples=subject.contextual_tuples,
            ),
        )
        decision = substrate.check(object())  # envelope unused by this mapper

        assert decision is SubstrateDecision.ALLOW
        assert seen["user"] == f"{USER_PREFIX}oid-1"
        assert seen["contextual_tuples"] == (
            (f"{USER_PREFIX}oid-1", MEMBER_RELATION, f"{GROUP_PREFIX}g-ops"),
        )
