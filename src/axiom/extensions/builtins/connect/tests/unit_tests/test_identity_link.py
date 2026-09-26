# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Tests for external-identity → Axiom principal binding."""
from __future__ import annotations

from axiom.extensions.builtins.connect.identity_link import (
    link_identity,
    resolve_principal,
    unlink_identity,
)


def test_unbound_resolves_to_none(tmp_path):
    assert resolve_principal("slack", "U123", path=tmp_path / "links.json") is None


def test_link_then_resolve(tmp_path):
    p = tmp_path / "links.json"
    link_identity("slack", "U123", "@ben:lab", path=p)
    assert resolve_principal("slack", "U123", path=p) == "@ben:lab"
    # Distinct (connector, external_id) keys don't collide.
    link_identity("teams", "U123", "@ada:lab", path=p)
    assert resolve_principal("teams", "U123", path=p) == "@ada:lab"
    assert resolve_principal("slack", "U123", path=p) == "@ben:lab"


def test_unlink_is_idempotent(tmp_path):
    p = tmp_path / "links.json"
    link_identity("slack", "U123", "@ben:lab", path=p)
    assert unlink_identity("slack", "U123", path=p) is True
    assert resolve_principal("slack", "U123", path=p) is None
    # Second unlink is a no-op.
    assert unlink_identity("slack", "U123", path=p) is False
