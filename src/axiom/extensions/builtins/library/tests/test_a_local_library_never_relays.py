# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``AXIOM_LIBRARY_LOCAL=1``: the Library stays on this install although it is bound (#1959).

``axi dev up`` sets it for every dev node (see the dev extension's
``test_a_dev_nodes_library_stays_in_its_lane``). The binding still decides
sign-in and data; only the Library is kept local.

- LL-1: bound, and the variable unset: the Library relays to the node (control).
- LL-2: bound, and the variable set: ``bound_node()`` is ``None``, an upload is
  filed in this install's catalog and bronze, and no request leaves for the node.
"""

from __future__ import annotations

import httpx
import pytest

from axiom.extensions.builtins.library import forward
from axiom.extensions.builtins.library.db_models import LibraryItem
from axiom.extensions.builtins.library.forward import bound_node as real_bound_node
from axiom.extensions.builtins.library.tests.test_library_upload import DOC, _up
from axiom.infra import serving_mode


@pytest.fixture
def bound(monkeypatch):
    mode = serving_mode.ServingMode("bound", site="alpha", node="node.example", face_url="https://node.example")
    monkeypatch.setattr(serving_mode, "resolve_mode", lambda: mode)
    monkeypatch.setattr(forward, "bound_node", real_bound_node)
    monkeypatch.setattr(forward, "_key", lambda face: "axk_test")

    def nowhere(*a, **k):
        raise AssertionError(f"a request left for the bound node: {a} {k.get('url', '')}")

    monkeypatch.setattr(httpx, "request", nowhere)


def test_ll1_bound_relays_by_default(library, bound, monkeypatch):
    monkeypatch.delenv("AXIOM_LIBRARY_LOCAL", raising=False)
    assert forward.bound_node() == forward.BoundNode(site="alpha", face_url="https://node.example", node="node.example")


def test_ll2_local_keeps_the_upload_here(client, library, bound, monkeypatch):
    monkeypatch.setenv("AXIOM_LIBRARY_LOCAL", "1")
    monkeypatch.setenv("AXIOM_LIBRARY_PIPELINE_INLINE", "1")
    assert forward.bound_node() is None
    r = _up(client, "pump.md", DOC)
    assert r.status_code == 201, r.text
    assert "x-axiom-served-by" not in r.headers
    with library["factory"]() as s:
        assert s.query(LibraryItem).count() == 1
    assert any(library["root"].rglob("*"))
