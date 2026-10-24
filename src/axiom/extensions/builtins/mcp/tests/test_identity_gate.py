# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-114 §1 — the MCP identity gate matcher, caller resolution, refusal logic."""
from __future__ import annotations

import pytest

from axiom.extensions.builtins.mcp import identity_gate as ig


@pytest.mark.parametrize(
    "caller,patterns,ok",
    [
        ("@ben:local", ("@*:local",), True),           # any-local wildcard
        ("@ben:local", ("@ben:local",), True),          # exact
        ("@ben:local", ("@alice:local",), False),       # wrong name
        ("@ben:local", ("@*:acme",), False),            # wrong context (remote/org)
        ("@svc:acme", ("@*:local",), False),            # remote principal denied by any-local
        ("@svc:acme", ("@svc:*",), True),               # name-exact, context-wildcard
        ("@x:y", (), False),                            # empty patterns -> deny
        ("@a:b", ("@*:*",), True),                       # full wildcard
    ],
)
def test_principal_admitted(caller, patterns, ok):
    assert ig.principal_admitted(caller, patterns) is ok


def test_caller_principal_env_override(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_CLIENT_PRINCIPAL", "@svc:acme")
    assert ig.caller_principal() == "@svc:acme"


def test_caller_principal_defaults_to_local_owner(monkeypatch):
    monkeypatch.delenv("AXIOM_MCP_CLIENT_PRINCIPAL", raising=False)
    from axiom.infra.principal import local_handle

    assert ig.caller_principal() == local_handle()


def test_refusal_reason_admitted_is_none():
    assert ig.refusal_reason("t", "@ben:local", ("@*:local",)) is None


def test_refusal_reason_denied_gives_reason():
    r = ig.refusal_reason("secret_tool", "@svc:acme", ("@*:local",))
    assert r and "secret_tool" in r and "@svc:acme" in r


def test_no_patterns_falls_back_to_owner_only(monkeypatch):
    monkeypatch.delenv("AXIOM_MCP_CLIENT_PRINCIPAL", raising=False)
    from axiom.infra.principal import local_handle

    owner = local_handle()
    # owner admitted under the fail-closed default; a remote principal is not.
    assert ig.refusal_reason("t", owner, ()) is None
    assert ig.refusal_reason("t", "@svc:acme", ()) is not None
