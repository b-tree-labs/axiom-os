# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A dev node's Library is its lane's own, even when the machine is bound to a site (#1959).

A bound dev node is a view of its site for sign-in and data, but a file a
developer drops into the Library while trying a change must not land on the
site's node, and two lanes must not share one bronze directory while each has
its own database.

- DN-1: the node ``axi dev up`` starts keeps its Library local and its bronze
  under the lane's home; two lanes get two directories.
- DN-2: a person who means to use the site's Library from a dev node says so
  (``AXIOM_LIBRARY_LOCAL=0`` in the environment), and that is kept.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.dev import node

CHECKOUT = Path(__file__).resolve().parents[6]


def test_dn1_the_library_is_local_and_bronze_is_the_lanes(tmp_path):
    a = node.build_plan(lane="a", root=CHECKOUT, port=1, state_dir=tmp_path, base_env={})
    b = node.build_plan(lane="b", root=CHECKOUT, port=2, state_dir=tmp_path, base_env={})
    assert a.env["AXIOM_LIBRARY_LOCAL"] == "1"
    root_a, root_b = Path(a.env["AXIOM_LIBRARY_BRONZE_ROOT"]), Path(b.env["AXIOM_LIBRARY_BRONZE_ROOT"])
    assert root_a.is_relative_to(a.home) and root_b.is_relative_to(b.home)
    assert root_a != root_b


def test_dn1_a_machine_wide_bronze_root_is_not_inherited(tmp_path):
    shared = str(tmp_path / "machine" / "bronze")
    plan = node.build_plan(lane="a", root=CHECKOUT, port=1, state_dir=tmp_path,
                           base_env={"AXIOM_LIBRARY_BRONZE_ROOT": shared})
    assert plan.env["AXIOM_LIBRARY_BRONZE_ROOT"] != shared


def test_dn2_an_explicit_choice_of_the_sites_library_is_kept(tmp_path):
    plan = node.build_plan(lane="a", root=CHECKOUT, port=1, state_dir=tmp_path,
                           base_env={"AXIOM_LIBRARY_LOCAL": "0"})
    assert plan.env["AXIOM_LIBRARY_LOCAL"] == "0"


# -- DN-3: the Running block says where the Library's files go --------------------------------


class _Bound:
    bound = True
    site = "alpha"
    face_url = "https://node.example"


def test_dn3_running_says_the_library_is_the_lanes_even_when_bound():
    from axiom.extensions.builtins.dev import running

    lib = running.library({"AXIOM_LIBRARY_LOCAL": "1", "AXIOM_LIBRARY_BRONZE_ROOT": "/lane/library/bronze"}, _Bound())
    assert running.library_line(lib) == "Library: this lane's (/lane/library/bronze), not relayed to alpha"
    lib = running.library({"AXIOM_LIBRARY_BRONZE_ROOT": "/lane/library/bronze", "AXIOM_LIBRARY_LOCAL": "1"}, object())
    assert running.library_line(lib) == "Library: this lane's (/lane/library/bronze)"
    lib = running.library({"AXIOM_LIBRARY_LOCAL": "0"}, _Bound())
    assert running.library_line(lib) == "Library: alpha's, relayed to node.example"
