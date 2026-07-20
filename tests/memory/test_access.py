# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for bipartite access graphs + retrospective access check.

Per Rezazadeh et al. 2025 (arXiv 2505.18279) §3.1/§3.3:
- G_UA(t) ⊆ U×A: users × agents, time-varying.
- G_AR(t) ⊆ A×R: agents × resources, time-varying.
- Visible(u, a, t) := { m | A(m) ⊆ A(u,t) ∧ R(m) ⊆ R(a,t) }

Revocation = edge removal. Fragments never mutate.
"""

from __future__ import annotations


# The graph-mechanics tests below query as user "u1" against fragments owned by
# a DISTINCT peer, so "u1" is a NON-OWNER: their visibility is purely
# graph-derived. This is exactly the cross-principal population the bipartite
# access check governs — and the population the ADR-026 ownership base case
# leaves untouched (it fires only when the requester owns the fragment; see
# TestOwnershipBaseCase). Using "u1" as both contributor and querier would let
# the base case short-circuit and mask the graph mechanics under test.
_CONTRIBUTOR = "@peer:contrib"


def _fragment(agents, resources, principal_id=_CONTRIBUTOR):
    """Build a MemoryFragment with given contributing agents + resources."""
    from axiom.memory.fragment import create_fragment

    return create_fragment(
        content={"fact": "x"},
        cognitive_type="semantic",
        principal_id=principal_id,
        agents=agents,
        resources=resources,
    )


class TestEmptyGraphs:
    def test_empty_graphs_deny_all_classified(self):
        from axiom.memory.access import AccessGraphs, is_visible

        g = AccessGraphs()
        frag = _fragment({"a1"}, {"r1"})
        assert is_visible(g, user="u1", agent="a1", fragment=frag) is False


class TestVisibleBasic:
    def test_all_edges_present_fragment_visible(self):
        from axiom.memory.access import (
            AccessGraphs,
            add_agent_resource_edge,
            add_user_agent_edge,
            is_visible,
        )

        g = AccessGraphs()
        g = add_user_agent_edge(g, "u1", "a1")
        g = add_agent_resource_edge(g, "a1", "r1")

        frag = _fragment({"a1"}, {"r1"})
        assert is_visible(g, "u1", "a1", frag) is True

    def test_missing_user_agent_edge_hides(self):
        from axiom.memory.access import AccessGraphs, add_agent_resource_edge, is_visible

        g = AccessGraphs()
        g = add_agent_resource_edge(g, "a1", "r1")
        # User has no edge to a1 → can't use a1 at all
        frag = _fragment({"a1"}, {"r1"})
        assert is_visible(g, "u1", "a1", frag) is False

    def test_missing_agent_resource_edge_hides(self):
        from axiom.memory.access import AccessGraphs, add_user_agent_edge, is_visible

        g = AccessGraphs()
        g = add_user_agent_edge(g, "u1", "a1")
        # Agent has no edge to r1 → fragment not visible
        frag = _fragment({"a1"}, {"r1"})
        assert is_visible(g, "u1", "a1", frag) is False


class TestMultipleContributors:
    def test_all_contributing_agents_must_be_accessible(self):
        """If fragment contributed by {a1, a2}, user must have access to BOTH."""
        from axiom.memory.access import (
            AccessGraphs,
            add_agent_resource_edge,
            add_user_agent_edge,
            is_visible,
        )

        g = AccessGraphs()
        g = add_user_agent_edge(g, "u1", "a1")
        g = add_agent_resource_edge(g, "a1", "r1")
        g = add_agent_resource_edge(g, "a2", "r1")
        # u1 can't reach a2
        frag = _fragment({"a1", "a2"}, {"r1"})
        assert is_visible(g, "u1", "a1", frag) is False

        # Add the missing edge
        g = add_user_agent_edge(g, "u1", "a2")
        assert is_visible(g, "u1", "a1", frag) is True

    def test_all_resources_must_be_accessible(self):
        from axiom.memory.access import (
            AccessGraphs,
            add_agent_resource_edge,
            add_user_agent_edge,
            is_visible,
        )

        g = AccessGraphs()
        g = add_user_agent_edge(g, "u1", "a1")
        g = add_agent_resource_edge(g, "a1", "r1")
        # Missing a1→r2
        frag = _fragment({"a1"}, {"r1", "r2"})
        assert is_visible(g, "u1", "a1", frag) is False

        g = add_agent_resource_edge(g, "a1", "r2")
        assert is_visible(g, "u1", "a1", frag) is True


class TestRevocation:
    """Edge removal hides previously-visible fragments WITHOUT mutating them."""

    def test_removing_user_agent_edge_hides_fragment(self):
        from axiom.memory.access import (
            AccessGraphs,
            add_agent_resource_edge,
            add_user_agent_edge,
            is_visible,
            remove_user_agent_edge,
        )

        g = AccessGraphs()
        g = add_user_agent_edge(g, "u1", "a1")
        g = add_agent_resource_edge(g, "a1", "r1")

        frag = _fragment({"a1"}, {"r1"})
        assert is_visible(g, "u1", "a1", frag) is True

        # Revoke user's access to a1
        g = remove_user_agent_edge(g, "u1", "a1")
        assert is_visible(g, "u1", "a1", frag) is False

    def test_fragment_unchanged_across_revocation(self):
        from axiom.memory.access import (
            AccessGraphs,
            add_agent_resource_edge,
            add_user_agent_edge,
            remove_user_agent_edge,
        )

        g = AccessGraphs()
        g = add_user_agent_edge(g, "u1", "a1")
        g = add_agent_resource_edge(g, "a1", "r1")

        frag = _fragment({"a1"}, {"r1"})
        original_id = frag.id
        original_prov = frag.provenance

        g = remove_user_agent_edge(g, "u1", "a1")
        # Fragment dataclass is frozen; revocation doesn't touch it
        assert frag.id == original_id
        assert frag.provenance == original_prov


class TestVisibleFragments:
    """Bulk filter by visibility."""

    def test_visible_fragments_filters_set(self):
        from axiom.memory.access import (
            AccessGraphs,
            add_agent_resource_edge,
            add_user_agent_edge,
            visible_fragments,
        )

        g = AccessGraphs()
        g = add_user_agent_edge(g, "u1", "a1")
        g = add_agent_resource_edge(g, "a1", "r1")

        frag_yes = _fragment({"a1"}, {"r1"})
        frag_no = _fragment({"a1"}, {"r2"})  # r2 not accessible

        result = visible_fragments(g, "u1", "a1", [frag_yes, frag_no])
        assert len(result) == 1
        assert result[0].id == frag_yes.id


class TestAccessibilitySets:
    """Helper views for introspection / UI / debugging."""

    def test_agents_for_user(self):
        from axiom.memory.access import (
            AccessGraphs,
            add_user_agent_edge,
            agents_for_user,
        )

        g = AccessGraphs()
        g = add_user_agent_edge(g, "u1", "a1")
        g = add_user_agent_edge(g, "u1", "a2")
        g = add_user_agent_edge(g, "u2", "a3")

        assert agents_for_user(g, "u1") == frozenset({"a1", "a2"})
        assert agents_for_user(g, "u2") == frozenset({"a3"})

    def test_resources_for_agent(self):
        from axiom.memory.access import (
            AccessGraphs,
            add_agent_resource_edge,
            resources_for_agent,
        )

        g = AccessGraphs()
        g = add_agent_resource_edge(g, "a1", "r1")
        g = add_agent_resource_edge(g, "a1", "r2")

        assert resources_for_agent(g, "a1") == frozenset({"r1", "r2"})


class TestOwnershipBaseCase:
    """ADR-026 base case: the master reads their OWN memory regardless of the
    access graph. The base case grants the READ right only — it opens no
    cross-principal path (a different principal falls through to the graph
    check) and never overrides the serving gate (vault etc. deny downstream).
    """

    def _owned(self, contributor, *, ctype="semantic", master=None):
        """A fragment authored by ``contributor``; ownership master set to
        ``master`` when given (else left unset / anonymously-owned)."""
        import dataclasses

        from axiom.memory.fragment import create_fragment
        from axiom.memory.ownership import new_ownership

        content = {"secret": "APIKEY hunter2"} if ctype == "vault" else {"fact": "x"}
        frag = create_fragment(
            content=content, cognitive_type=ctype,
            principal_id=contributor, agents={"axi"}, resources=set(),
        )
        if master is not None:
            frag = dataclasses.replace(frag, ownership=new_ownership(master=master))
        return frag

    def test_owner_reads_own_with_empty_graph(self):
        from axiom.memory.access import AccessGraphs, is_visible

        frag = self._owned("@alice:home", master="@alice:home")
        assert is_visible(
            AccessGraphs(), user="@alice:home", agent="axi", fragment=frag
        ) is True

    def test_owner_reads_own_anonymously_owned_fragment(self):
        # No ownership record → the contributing principal is the de-facto
        # owner (ADR-026 migration note); read still granted under empty graph.
        from axiom.memory.access import AccessGraphs, is_visible

        frag = self._owned("@alice:home")  # ownership left unset
        assert frag.ownership is None
        assert is_visible(
            AccessGraphs(), user="@alice:home", agent="axi", fragment=frag
        ) is True

    def test_non_owner_gets_nothing_from_base_case(self):
        # A DIFFERENT principal is NOT helped by the base case → empty graph
        # denies. The ownership base case opens no cross-principal path.
        from axiom.memory.access import AccessGraphs, is_visible

        frag = self._owned("@alice:home", master="@alice:home")
        assert is_visible(
            AccessGraphs(), user="@mallory:evil", agent="axi", fragment=frag
        ) is False

    def test_non_owner_still_governed_by_graph(self):
        # The §3.3 graph path is unchanged for non-owners: with the right edge
        # a peer CAN read; without it they cannot.
        from axiom.memory.access import (
            AccessGraphs,
            add_user_agent_edge,
            is_visible,
        )

        frag = self._owned("@alice:home", master="@alice:home")
        assert is_visible(
            AccessGraphs(), user="@peer:reader", agent="axi", fragment=frag
        ) is False
        g = add_user_agent_edge(AccessGraphs(), "@peer:reader", "axi")
        assert is_visible(
            g, user="@peer:reader", agent="axi", fragment=frag
        ) is True

    def test_transfer_clean_break_original_contributor_is_not_owner(self):
        # Ownership set to a NEW master (a post-transfer clean break, ADR-026):
        # the original contributor no longer owns it, so the base case denies
        # them; the new master is granted. Proves ownership (not principal_id)
        # is authoritative when set.
        from axiom.memory.access import AccessGraphs, is_visible

        frag = self._owned("@alice:home", master="@newmaster:home")
        assert is_visible(
            AccessGraphs(), user="@alice:home", agent="axi", fragment=frag
        ) is False
        assert is_visible(
            AccessGraphs(), user="@newmaster:home", agent="axi", fragment=frag
        ) is True

    def test_empty_user_owns_nothing(self):
        # An unresolved requester ("") must never match the base case.
        from axiom.memory.access import AccessGraphs, is_visible

        frag = self._owned("", master="")  # degenerate empty owner
        assert is_visible(
            AccessGraphs(), user="", agent="axi", fragment=frag
        ) is False

    def test_owner_of_vault_reads_but_serving_gate_denies(self):
        # The base case grants the READ right even for vault — read is not
        # serve. Vault-never lives at the serving gate (ADR-087 D7), which the
        # ownership base case does NOT touch: the same owned vault fragment is
        # denied when it reaches the gate.
        from axiom.memory.access import AccessGraphs, is_visible
        from axiom.memory.serving import (
            ConsumerCoordinate,
            DenyReason,
            ServableItem,
            ServingGate,
        )

        vault = self._owned("@alice:home", ctype="vault", master="@alice:home")
        assert is_visible(
            AccessGraphs(), user="@alice:home", agent="axi", fragment=vault
        ) is True
        decision = ServingGate().evaluate(
            ServableItem.from_fragment(vault),
            ConsumerCoordinate(
                principal="@alice:home", harness="cc", account="@alice:home",
            ),
        )
        assert decision.allowed is False
        assert decision.reason is DenyReason.VAULT


class TestQueryingAgentMustBeAccessible:
    """The agent the user is querying through must itself be in A(u,t)."""

    def test_query_via_unreachable_agent_denies(self):
        from axiom.memory.access import (
            AccessGraphs,
            add_agent_resource_edge,
            add_user_agent_edge,
            is_visible,
        )

        g = AccessGraphs()
        g = add_user_agent_edge(g, "u1", "a1")
        g = add_agent_resource_edge(g, "a1", "r1")
        g = add_agent_resource_edge(g, "a2", "r1")

        frag = _fragment({"a1"}, {"r1"})
        # User tries to query via a2, which they don't have access to
        assert is_visible(g, "u1", "a2", frag) is False
