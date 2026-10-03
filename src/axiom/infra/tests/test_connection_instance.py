# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Connection-as-first-class (ADR-074 Phase 2): a connection is the
authenticated *instance* of a connector definition — its own object with
status, scopes, webhooks, owner, and a secret_ref (never inline creds)."""

from __future__ import annotations

import pytest

from axiom.infra.connector_fabric import (
    ConnectionInstance,
    ConnectionStatus,
    ConnectionStore,
)


def _conn(name="slack-example-host", **kw):
    base = dict(
        name=name,
        connector="ai.axiom.connector.slack",
        owner="@ben",
        secret_ref="kubernetes://axiom-data/dp1-slack",
    )
    base.update(kw)
    return ConnectionInstance(**base)


def test_new_connection_is_pending_and_holds_no_plaintext():
    c = _conn()
    assert c.status is ConnectionStatus.PENDING
    # creds live behind a secret_ref; the instance must not carry a raw token
    assert c.secret_ref.startswith("kubernetes://")
    assert "xoxb" not in repr(c)


def test_secret_ref_required():
    with pytest.raises(ValueError, match="secret_ref"):
        ConnectionInstance(name="x", connector="ai.axiom.connector.slack", owner="@ben", secret_ref="")


def test_store_register_get_and_status_transition():
    store = ConnectionStore()
    store.put(_conn())
    got = store.get("slack-example-host")
    assert got.status is ConnectionStatus.PENDING
    store.set_status("slack-example-host", ConnectionStatus.ACTIVE, scopes=["chat:write", "app_mentions:read"])
    got = store.get("slack-example-host")
    assert got.status is ConnectionStatus.ACTIVE
    assert "chat:write" in got.scopes


def test_store_lists_by_connector():
    store = ConnectionStore()
    store.put(_conn(name="slack-a"))
    store.put(_conn(name="slack-b"))
    store.put(_conn(name="teams-a", connector="ai.axiom.connector.teams"))
    slack = store.for_connector("ai.axiom.connector.slack")
    assert {c.name for c in slack} == {"slack-a", "slack-b"}


def test_webhooks_and_owner_tracked():
    c = _conn(webhook_urls=["https://hooks.example/x"])
    assert c.owner == "@ben"
    assert c.webhook_urls == ["https://hooks.example/x"]


# --- App-type: auth_mode / tenancy / capability_scope (ADR-110 §Decision-2) ---

def test_auth_mode_enum_has_the_three_adr110_modes():
    from axiom.infra.connector_fabric import AuthMode
    assert {m.value for m in AuthMode} >= {"service", "delegated_user", "user_signin"}


def test_connection_defaults_are_backward_compatible():
    """Existing call sites construct a connection with no app-type fields; they
    must keep working, defaulting to unspecified/single."""
    from axiom.infra.connector_fabric import AuthMode, Tenancy
    c = _conn()
    assert c.auth_mode is AuthMode.UNSPECIFIED
    assert c.tenancy is Tenancy.SINGLE
    assert c.capability_scope == ""


def test_app_type_fields_carry_explicit_values():
    from axiom.infra.connector_fabric import AuthMode, Tenancy
    c = _conn(auth_mode=AuthMode.DELEGATED_USER, tenancy=Tenancy.MULTI,
              capability_scope="user-own")
    assert c.auth_mode is AuthMode.DELEGATED_USER
    assert c.tenancy is Tenancy.MULTI
    assert c.capability_scope == "user-own"


def test_string_app_type_values_coerce_to_enums():
    """Persisted records round-trip through JSON as strings; they coerce back."""
    from axiom.infra.connector_fabric import AuthMode, Tenancy
    c = _conn(auth_mode="service", tenancy="multi")
    assert c.auth_mode is AuthMode.SERVICE and c.tenancy is Tenancy.MULTI


def test_invalid_auth_mode_is_rejected():
    with pytest.raises(ValueError):
        _conn(auth_mode="root")
