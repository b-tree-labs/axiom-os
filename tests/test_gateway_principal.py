# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who asked for this completion?

The gateway routes, enforces tiers, holds credentials and writes an audit
record for every request — but until now that record could not say *who* the
request was for. An action with no attributable principal cannot be authorised,
reviewed, or placed on an authority ladder, and a long agent run is exactly
where "who asked" is easiest to lose.

Two rules, and they pull in opposite directions on purpose:

- **Attribution must never become a new failure mode.** Resolution is
  best-effort. A public call from a process with no bound actor still works and
  is recorded as unattributed, because a gateway that starts refusing ordinary
  traffic to enforce bookkeeping is worse than the bookkeeping gap.
- **Except under export control**, where an unattributed call is refused. There
  the whole point of the tier is that every request is attributable to a
  responsible party.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def _make_provider(name="p1", routing_tier="any", priority=10):
    from axiom.llm.gateway import LLMProvider

    return LLMProvider(
        name=name,
        endpoint="https://api.example.com/v1",
        model="test-model",
        uid=f"uid-{name}",
        api_key_env="FAKE_KEY",
        priority=priority,
        use_for=["chat", "fallback"],
        routing_tier=routing_tier,
        routing_tags=[],
        requires_vpn=False,
        verify_ssl=True,
        max_tokens_default=0,
    )


def _gateway(providers, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sk-test-fake-key")
    from axiom.llm.gateway import Gateway

    with patch.object(Gateway, "_load_config", lambda self: None):
        gw = Gateway()
    gw.providers = list(providers)
    return gw


@pytest.fixture(autouse=True)
def _no_ambient_actor(monkeypatch):
    """Start every test with nothing bound, so attribution is explicit."""
    monkeypatch.delenv("AXIOM_ACTOR", raising=False)


@pytest.fixture
def routing_records(monkeypatch):
    """Capture what the gateway writes to the routing audit."""
    records: list[dict] = []

    class _FakeAudit:
        def write_routing(self, **kwargs):
            records.append(kwargs)

        def write_vpn(self, **kwargs):
            pass

    from axiom.infra import audit_log

    monkeypatch.setattr(audit_log.AuditLog, "get", classmethod(lambda cls: _FakeAudit()))
    return records


@pytest.fixture
def stub_completion():
    """Make the provider call succeed without touching the network."""
    from axiom.llm.gateway import CompletionResponse, Gateway

    with patch.object(
        Gateway,
        "_call_provider_with_tools",
        return_value=CompletionResponse(text="ok", provider="p1", success=True),
    ) as call:
        yield call


# --- resolution ---------------------------------------------------------------


def test_an_explicit_principal_is_recorded(monkeypatch, routing_records, stub_completion):
    gw = _gateway([_make_provider()], monkeypatch)

    gw.complete_with_tools(messages=[{"role": "user", "content": "hi"}], principal="@ben:ut")

    assert routing_records[-1]["principal"] == "@ben:ut"


def test_the_ambient_actor_is_used_when_no_principal_is_passed(
    monkeypatch, routing_records, stub_completion
):
    """`acting_as(...)` around a run should attribute the calls inside it
    without every caller threading a principal through."""
    monkeypatch.setenv("AXIOM_ACTOR", "@ambient:ut")
    gw = _gateway([_make_provider()], monkeypatch)

    gw.complete_with_tools(messages=[{"role": "user", "content": "hi"}])

    assert routing_records[-1]["principal"] == "@ambient:ut"


def test_an_explicit_principal_beats_the_ambient_one(
    monkeypatch, routing_records, stub_completion
):
    monkeypatch.setenv("AXIOM_ACTOR", "@ambient:ut")
    gw = _gateway([_make_provider()], monkeypatch)

    gw.complete_with_tools(
        messages=[{"role": "user", "content": "hi"}], principal="@explicit:ut"
    )

    assert routing_records[-1]["principal"] == "@explicit:ut"


# --- attribution must not break ordinary traffic ------------------------------


def test_an_unattributed_public_call_still_succeeds(
    monkeypatch, routing_records, stub_completion
):
    """A gateway that refuses ordinary traffic to enforce bookkeeping is worse
    than the bookkeeping gap."""
    gw = _gateway([_make_provider(routing_tier="public")], monkeypatch)

    response = gw.complete_with_tools(
        messages=[{"role": "user", "content": "hi"}], routing_tier="public"
    )

    assert response.success
    assert routing_records[-1]["principal"] is None


def test_resolution_never_raises(monkeypatch, routing_records, stub_completion):
    """`get_current_actor` raises when nothing is resolvable. Letting that
    escape would make every call fail on a host with no actor bound."""
    from axiom.llm import gateway as gw_mod

    def _explode(**_kwargs):
        raise RuntimeError("identity subsystem is down")

    monkeypatch.setattr(gw_mod, "_resolve_actor_handle", gw_mod._resolve_actor_handle)
    with patch("axiom.governance.get_current_actor", side_effect=_explode):
        gw = _gateway([_make_provider()], monkeypatch)
        response = gw.complete_with_tools(messages=[{"role": "user", "content": "hi"}])

    assert response.success
    assert routing_records[-1]["principal"] is None


# --- export control refuses the unattributed ----------------------------------


def test_an_unattributed_export_controlled_call_is_refused(
    monkeypatch, routing_records, stub_completion
):
    gw = _gateway([_make_provider(routing_tier="export_controlled")], monkeypatch)

    response = gw.complete_with_tools(
        messages=[{"role": "user", "content": "hi"}], routing_tier="export_controlled"
    )

    assert not response.success
    assert response.error == "EC_UNATTRIBUTED"
    stub_completion.assert_not_called()


def test_the_refusal_is_audited(monkeypatch, routing_records, stub_completion):
    """A refusal nobody can see is indistinguishable from a call that never
    happened."""
    gw = _gateway([_make_provider(routing_tier="export_controlled")], monkeypatch)

    gw.complete_with_tools(
        messages=[{"role": "user", "content": "hi"}], routing_tier="export_controlled"
    )

    record = routing_records[-1]
    assert record["blocked"] is True
    assert record["block_reason"] == "EC_UNATTRIBUTED"
    assert record["principal"] is None


def test_an_attributed_export_controlled_call_proceeds(
    monkeypatch, routing_records, stub_completion
):
    gw = _gateway([_make_provider(routing_tier="export_controlled")], monkeypatch)

    response = gw.complete_with_tools(
        messages=[{"role": "user", "content": "hi"}],
        routing_tier="export_controlled",
        principal="@ben:ut",
    )

    assert response.success
    assert routing_records[-1]["principal"] == "@ben:ut"


def test_the_refusal_message_says_what_to_do(
    monkeypatch, routing_records, stub_completion
):
    """An operator hitting this needs to know it is fixable and how."""
    gw = _gateway([_make_provider(routing_tier="export_controlled")], monkeypatch)

    response = gw.complete_with_tools(
        messages=[{"role": "user", "content": "hi"}], routing_tier="export_controlled"
    )

    assert "AXIOM_ACTOR" in response.text or "actor" in response.text.lower()
