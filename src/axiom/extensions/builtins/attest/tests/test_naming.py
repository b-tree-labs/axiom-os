# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A signer is named in a site's context, even when the sign-in carried none.

A session's ``site`` claim comes from the gate account, and accounts made
before the gate began stamping one have none. Attest named such a person
``@<id>`` — no context — and a role is only ever granted to ``@name:context``,
so that person could never sign anything, on any node. The draft endpoint and
the grant endpoint must also name the person identically, or a grant is
minted for someone other than the draft's owner.
"""

from __future__ import annotations

import pytest

from ..naming import signer_site, signer_handle

SUB = "ac5f42d3-3c30-450a-a394-db2b671437b9"


@pytest.fixture(autouse=True)
def no_site_env(monkeypatch):
    monkeypatch.delenv("AXIOM_SITE", raising=False)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)


def test_the_sessions_own_site_wins(monkeypatch):
    monkeypatch.setenv("AXIOM_SITE", "node-home")
    assert signer_site({"site": "site-a"}) == "site-a"
    assert signer_handle({"sub": SUB, "site": "site-a"}) == f"@{SUB}:site-a"


def test_without_one_the_single_served_site_names_the_signer(monkeypatch):
    monkeypatch.setenv("AXIOM_SERVED_SITES", "site-b")
    assert signer_handle({"sub": SUB}) == f"@{SUB}:site-b"


def test_without_either_the_nodes_own_site_names_the_signer(monkeypatch):
    monkeypatch.setenv("AXIOM_SITE", "local")
    assert signer_handle({"sub": SUB}) == f"@{SUB}:local"


def test_several_served_sites_do_not_choose_one(monkeypatch):
    """Picking one of several would put the person's signature on a site
    they did not sign for; the node's own site breaks no tie either."""
    monkeypatch.setenv("AXIOM_SERVED_SITES", "site-a,site-b")
    monkeypatch.setenv("AXIOM_SITE", "site-a")
    assert signer_site({}) is None


def test_with_nothing_to_go_on_the_handle_has_no_context():
    """Unchanged from before: such a person still cannot hold a role, and the
    refusal says why, rather than a context being invented."""
    assert signer_handle({"sub": SUB}) == f"@{SUB}"


def test_no_subject_is_no_signer():
    with pytest.raises(ValueError):
        signer_handle({})


def test_the_attest_api_and_the_gate_name_a_person_the_same_way():
    """Both import the one helper, so they cannot drift apart."""
    import inspect

    from axiom.extensions.builtins.webgate.api import signing as gate_signing

    from .. import api

    assert "signer_handle" in inspect.getsource(api._person)
    assert "signer_handle" in inspect.getsource(gate_signing)
