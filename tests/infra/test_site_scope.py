# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One platform, several sites, and one answer to who may see what.

A partner's deployment should behave as though it is their site and nothing else
exists. A fleet operator, researcher or regulator may see many and compare
across them. Same question, different answer — so it is asked once, here, and
every resource surface uses the result rather than growing its own filter.
"""

from __future__ import annotations

import pytest

from axiom.infra.site_scope import SiteOutOfScope, SiteScope, deployment_sites, resolve

ACU, VCU, TAMU, TRIGA = "acu-flowloop", "vcu-flowloop", "tamu-flowloop", "ut-triga"
ALL = [ACU, VCU, TAMU, TRIGA]


# --- the partner deployment: "as if it is the site" -------------------------

def test_a_partner_deployment_sees_only_its_own_site():
    scope = resolve(env={"AXIOM_SERVED_SITES": ACU})
    assert scope.permits(ACU)
    assert not scope.permits(VCU)
    assert scope.filter(ALL) == [ACU]


def test_a_listing_never_names_a_site_it_would_then_refuse():
    """Showing a name and refusing it discloses what the refusal withholds."""
    scope = resolve(env={"AXIOM_SERVED_SITES": ACU})
    listed = scope.filter(ALL)
    for site in listed:
        assert scope.permits(site)
    assert VCU not in listed


def test_out_of_scope_raises_rather_than_returning_empty():
    """An empty answer and a forbidden one must not look the same to a surface."""
    scope = resolve(env={"AXIOM_SERVED_SITES": ACU})
    with pytest.raises(SiteOutOfScope):
        scope.require(VCU)
    assert scope.require(ACU) == ACU


# --- fleet: many sites, compared across ------------------------------------

def test_a_fleet_grant_spans_several_sites():
    scope = resolve(granted=[ACU, VCU, TAMU],
                    env={"AXIOM_SERVED_SITES": f"{ACU},{VCU},{TAMU},{TRIGA}"})
    assert scope.filter(ALL) == [ACU, VCU, TAMU]
    assert not scope.permits(TRIGA)


def test_a_grant_cannot_widen_the_deployment_bound():
    """The node's bound is the outer limit by construction.

    A misconfigured grant — or a compromised one — must not reach a site this
    node does not serve at all.
    """
    scope = resolve(granted=[ACU, VCU], env={"AXIOM_SERVED_SITES": ACU})
    assert scope.permits(ACU)
    assert not scope.permits(VCU), "a grant widened the deployment bound"


def test_no_grant_yet_leaves_the_deployment_bound_standing():
    """The state before the authorization seam lands, safe in the right direction."""
    scope = resolve(granted=None, env={"AXIOM_SERVED_SITES": f"{ACU},{VCU}"})
    assert scope.filter(ALL) == [ACU, VCU]


# --- the unconfigured case --------------------------------------------------

def test_an_undeclared_deployment_is_unbounded_and_says_so():
    """A dev box with no config must work; the bound is where strictness lives."""
    scope = resolve(env={})
    assert scope.unbounded
    assert scope.filter(ALL) == ALL
    assert "no bound configured" in scope.describe()


def test_an_empty_setting_is_treated_as_undeclared_not_as_no_sites():
    """AXIOM_SERVED_SITES='' is a missing value, not an instruction to serve
    nothing — which would be an outage that reads like missing data."""
    assert deployment_sites({"AXIOM_SERVED_SITES": "   "}) is None


def test_whitespace_and_blanks_in_the_list_are_tolerated():
    assert deployment_sites({"AXIOM_SERVED_SITES": f" {ACU} , ,{VCU} "}) == frozenset(
        {ACU, VCU}
    )


# --- a grant of nothing -----------------------------------------------------

def test_a_grant_of_nothing_permits_nothing():
    """Distinct from 'no grant known'. An explicit empty grant is a real answer."""
    scope = resolve(granted=[], env={"AXIOM_SERVED_SITES": ACU})
    assert scope.filter(ALL) == []
    assert not scope.permits(ACU)
    assert scope.describe() == "no sites"


def test_scope_is_immutable():
    """A surface must not be able to widen its own scope mid-request."""
    scope = SiteScope(sites=frozenset({ACU}))
    with pytest.raises(AttributeError):
        scope.sites = frozenset({ACU, VCU})  # type: ignore[misc]
