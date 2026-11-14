# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A model switch cannot send controlled content to a provider that may not have it (issue #1136, ADR-168).

Two layers, each tested alone so neither can hide the other's absence:

1. The switch itself (`/model`, `@provider`, `/m`): refused when the session already holds content the
   target's tier does not admit, with the providers that would be admitted named.
2. Selection: an override can never downgrade an export-controlled REQUEST. The override used to be
   honored "unconditionally", before any tier logic, so the layer-1 check could be bypassed by any
   caller that set the override directly.
"""

from __future__ import annotations

import pytest

from axiom.llm.tier_policy import ProviderNotAdmitted, admits, decide_switch
from tests.test_gateway import _gateway_with_providers, _make_provider


def _providers():
    return [
        _make_provider(name="cloud", routing_tier="public", priority=1),
        _make_provider(name="anywhere", routing_tier="any", priority=2),
        _make_provider(
            name="ec-node",
            routing_tier="export_controlled",
            priority=3,
            endpoint="http://10.0.0.5/v1",
        ),
    ]


# ---- the rule ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider_tier,content_tier,ok",
    [
        ("public", "public", True),
        ("any", "public", True),
        (
            "export_controlled",
            "public",
            True,
        ),  # a more-restricted provider may hold less-restricted content
        ("export_controlled", "export_controlled", True),
        ("public", "export_controlled", False),
        ("any", "export_controlled", False),  # "any" is NOT cleared for controlled content
    ],
)
def test_admits(provider_tier, content_tier, ok):
    assert admits(provider_tier, content_tier) is ok


def test_decide_switch_names_the_providers_that_would_be_admitted():
    d = decide_switch(_providers(), "cloud", "export_controlled")
    assert not d.allowed and d.admitted == ("ec-node",)
    assert "export_controlled" in d.reason and "ec-node" in d.reason
    assert decide_switch(_providers(), "ec-node", "export_controlled").allowed
    assert decide_switch(_providers(), "cloud", "public").allowed


def test_a_provider_that_does_not_exist_is_not_allowed():
    d = decide_switch(_providers(), "nope", "any")
    assert not d.allowed and "no provider named" in d.reason


# ---- layer 1: the switch ---------------------------------------------------------------------


def test_the_switch_is_refused_and_nothing_is_stored(monkeypatch):
    gw = _gateway_with_providers(_providers(), monkeypatch)
    with pytest.raises(ProviderNotAdmitted) as exc:
        gw.set_provider_override("cloud", content_tier="export_controlled")
    assert exc.value.decision.admitted == ("ec-node",)
    assert gw._provider_override is None


def test_the_switch_is_allowed_when_the_tier_admits_it(monkeypatch):
    gw = _gateway_with_providers(_providers(), monkeypatch)
    gw.set_provider_override("ec-node", content_tier="export_controlled")
    assert gw._provider_override == "ec-node"
    gw.set_provider_override("cloud", content_tier="public")
    assert gw._provider_override == "cloud"


def test_the_old_call_shape_still_works_for_uncontrolled_content(monkeypatch):
    gw = _gateway_with_providers(_providers(), monkeypatch)
    gw.set_provider_override("cloud")
    assert gw._provider_override == "cloud"


# ---- layer 2: selection cannot be downgraded by an override ---------------------------------


def test_an_override_cannot_downgrade_an_export_controlled_request(monkeypatch):
    gw = _gateway_with_providers(_providers(), monkeypatch)
    gw._provider_override = "cloud"  # set directly: the layer-1 check was bypassed
    chosen = gw._select_provider("fallback", routing_tier="export_controlled")
    assert chosen is not None and chosen.name == "ec-node", (
        "the override was honored for controlled content"
    )


def test_with_no_cleared_provider_the_request_fails_closed_not_to_the_override(monkeypatch):
    gw = _gateway_with_providers(_providers()[:2], monkeypatch)
    gw._provider_override = "cloud"
    assert gw._select_provider("fallback", routing_tier="export_controlled") is None


def test_the_override_still_works_for_uncontrolled_requests(monkeypatch):
    gw = _gateway_with_providers(_providers(), monkeypatch)
    gw._provider_override = "cloud"
    assert gw._select_provider("fallback", routing_tier="public").name == "cloud"
    assert gw._select_provider("fallback").name == "cloud"


def test_a_refused_switch_is_audited(tmp_path, monkeypatch):
    from axiom.infra import routing_audit

    audit = tmp_path / "routing_audit.jsonl"
    monkeypatch.setattr(routing_audit, "_AUDIT_PATH", audit)
    gw = _gateway_with_providers(_providers(), monkeypatch)
    with pytest.raises(ProviderNotAdmitted):
        gw.set_provider_override("cloud", content_tier="export_controlled")
    text = audit.read_text()
    assert "switch refused" in text and "cloud" in text and "export_controlled" in text
