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


def _fragment(agents, resources):
    """Build a MemoryFragment with given contributing agents + resources."""
    from axiom.memory.fragment import create_fragment

    return create_fragment(
        content={"fact": "x"},
        cognitive_type="semantic",
        principal_id="u1",
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
