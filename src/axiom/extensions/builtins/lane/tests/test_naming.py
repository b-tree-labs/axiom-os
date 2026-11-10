# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The derivation rules, which two tools must agree on forever.

Every test here is really one assertion: a lane's identity is a pure function
of its checkout directory. If that stops being true, two tools computing the
same lane get different answers and the isolation is worse than none, because
people will believe in it.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.lane import naming

SHARED = "axiom"


# --- slugs ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("axiom-wt-rollup", "axiom_wt_rollup"),
        ("Axiom_WT_Rollup", "axiom_wt_rollup"),
        ("feat/chat surface", "feat_chat_surface"),
        ("--leading-and-trailing--", "leading_and_trailing"),
        ("a..b__c", "a_b_c"),
    ],
)
def test_a_folder_becomes_one_boring_token(folder, expected):
    assert naming.slug(folder) == expected


def test_a_folder_with_nothing_usable_still_yields_a_name():
    """Better a dull constant than an empty identifier Postgres rejects."""
    assert naming.slug("-----") == "worktree"
    assert naming.slug("") == "worktree"


# --- database names ---------------------------------------------------------


def test_the_database_carries_the_prefix_and_the_folder():
    assert (
        naming.database_name("axiom-wt-rollup", prefix="axiom_lane") == "axiom_lane_axiom_wt_rollup"
    )


def test_a_long_name_is_truncated_by_US_not_by_postgres():
    """Postgres truncates at 63 silently, and two names that differ only past
    the cut become the same database. Cutting deliberately is the difference
    between a short name and a collision nobody sees."""
    name = naming.database_name("x" * 200, prefix="axiom_lane")

    assert len(name) <= naming.MAX_IDENTIFIER
    assert not name.endswith("_")


def test_the_same_folder_always_gives_the_same_database():
    a = naming.database_name("some-worktree", prefix="axiom_lane")
    b = naming.database_name("some-worktree", prefix="axiom_lane")

    assert a == b


# --- ports ------------------------------------------------------------------


def test_a_preferred_port_is_stable_for_a_name():
    assert naming.preferred_port("chat") == naming.preferred_port("chat")


def test_preferred_ports_sit_inside_the_lane_range_and_are_even_strides():
    for name in ("chat", "steer", "charts", "a", "zzzzzz"):
        p = naming.preferred_port(name)
        assert naming.FIRST_LANE_PORT <= p <= naming.LAST_LANE_PORT
        assert (p - naming.FIRST_LANE_PORT) % 2 == 0, (
            "a lane takes a pair, so it starts on a stride"
        )


def test_different_names_generally_differ():
    """Not a guarantee — it is a hash — which is why the allocator probes."""
    ports = {naming.preferred_port(n) for n in ("chat", "steer", "charts", "docs", "infra")}

    assert len(ports) >= 4


# --- the rewrite rule, which is the safety of the whole design --------------


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://localhost/axiom",
        "postgresql://127.0.0.1:5432/axiom",
        "postgres://user:pw@localhost:5432/axiom",
    ],
)
def test_a_local_shared_dev_url_is_rewritable(url):
    assert naming.is_rewritable(url, shared_database=SHARED)


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("postgresql://db.example.com/axiom", "a remote host is somebody's real database"),
        ("postgresql://localhost:6543/axiom", "a non-default port was chosen deliberately"),
        ("postgresql://localhost/axiom_prod_snapshot", "a database nobody was handed by default"),
        ("mysql://localhost/axiom", "not Postgres"),
        ("not a url at all", "unparseable is not permission"),
        ("", "empty is not permission"),
    ],
)
def test_anything_else_is_left_alone(url, why):
    assert not naming.is_rewritable(url, shared_database=SHARED), why


def test_rewriting_keeps_everything_except_the_database():
    out = naming.rewrite_database("postgresql://user:pw@localhost:5432/axiom", "axiom_lane_chat")

    assert out == "postgresql://user:pw@localhost:5432/axiom_lane_chat"


def test_the_maintenance_url_points_at_postgres_on_the_same_server():
    """A first run has no lane database yet; probing it would report the
    SERVER as down when only the database is absent."""
    assert naming.maintenance_url("postgresql://localhost:5432/axiom_lane_chat") == (
        "postgresql://localhost:5432/postgres"
    )


# --- worktree detection -----------------------------------------------------


def test_a_dot_git_directory_is_a_primary_clone(tmp_path):
    (tmp_path / ".git").mkdir()

    assert not naming.is_linked_worktree(tmp_path)


def test_a_dot_git_file_is_a_linked_worktree(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")

    assert naming.is_linked_worktree(tmp_path)


def test_a_directory_that_is_not_a_checkout_is_neither(tmp_path):
    assert not naming.is_linked_worktree(tmp_path)
    assert naming.repo_root(tmp_path) is None or naming.repo_root(tmp_path).exists()
