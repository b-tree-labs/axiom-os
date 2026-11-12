# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The gateway records which provider each call actually chose (ADR-168).

`active_provider` is the first usable provider in the list: it ignores an override and the request's
tier, so a status line built from it can name a model that never answered. A stamp must read the
real selection, and a prediction for the next turn must be labelled as one.
"""

from __future__ import annotations

from tests.test_gateway import _gateway_with_providers, _make_provider


def _gw(monkeypatch):
    return _gateway_with_providers(
        [
            _make_provider(name="first", routing_tier="public", priority=1),
            _make_provider(name="second", routing_tier="any", priority=2),
            _make_provider(name="ec", routing_tier="export_controlled", priority=3),
        ],
        monkeypatch,
    )


def test_active_provider_is_not_what_answered(monkeypatch):
    gw = _gw(monkeypatch)
    gw.set_provider_override("second")
    chosen = gw._select_provider("fallback")
    assert (
        chosen.name == "second" and gw.active_provider.name == "first"
    )  # the old status line said "first"
    assert gw.last_selected.name == "second"


def test_last_selected_follows_the_request_tier(monkeypatch):
    gw = _gw(monkeypatch)
    gw._select_provider("fallback", routing_tier="export_controlled")
    assert gw.last_selected.name == "ec"


def test_last_selected_is_none_before_any_call_and_after_a_refusal(monkeypatch):
    gw = _gw(monkeypatch)
    assert gw.last_selected is None
    gw._provider_override = "first"
    gw.providers[:] = [p for p in gw.providers if p.name != "ec"]
    assert gw._select_provider("fallback", routing_tier="export_controlled") is None
    assert gw.last_selected is None  # a failed selection must not leave a stale answer behind


def test_preview_predicts_without_recording_and_without_the_network(monkeypatch):
    gw = _gw(monkeypatch)
    gw.set_provider_override("second")
    assert gw.preview_provider().name == "second"
    assert gw.preview_provider("export_controlled").name == "ec"  # the override does not admit it
    assert gw.last_selected is None, "a preview is not a selection"


def test_preview_does_not_probe_a_vpn_endpoint(monkeypatch):
    vpn = _make_provider(
        name="vpn", requires_vpn=True, priority=1, endpoint="http://10.255.255.1:1/v1"
    )
    gw = _gateway_with_providers([vpn], monkeypatch)
    monkeypatch.setattr(
        type(gw),
        "_check_vpn",
        lambda self, p: (_ for _ in ()).throw(AssertionError("probed the network")),
    )
    assert gw.preview_provider().name == "vpn"
