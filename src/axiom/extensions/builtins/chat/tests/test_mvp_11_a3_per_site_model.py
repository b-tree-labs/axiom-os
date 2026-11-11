# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""MVP-11-A3: the model chosen for a site answers chat in each variant.

Each variant (bare platform, product, site) runs from its own state, so its own
binding. A variant bound to one site asks that site's node model with that node's
key; a variant bound to another site asks the other one; a variant that holds the
medallion itself is not sent to any node. Nothing chosen for one reaches another.
"""

from __future__ import annotations

from typing import Any

import pytest

from axiom.extensions.builtins.chat import bound_model
from axiom.extensions.builtins.data_platform import read_target
from axiom.extensions.builtins.data_platform.read_target import ReadTarget

#: variant -> (binding, key, the model its node serves)
VARIANTS = {
    "product": (
        ReadTarget("binding", site="site-a", face_url="https://a.example"),
        "key-a",
        "model-a",
    ),
    "site": (
        ReadTarget("binding", site="site-b", face_url="https://b.example"),
        "key-b",
        "model-b",
    ),
    "axiom": (ReadTarget("medallion", reason="this install holds the medallion"), "", ""),
}


class _Gateway:
    def __init__(self) -> None:
        self.providers: list[Any] = []
        self.auto_discovered: set[str] = set()
        self._provider_override = None

    def add_provider(self, provider: Any, first: bool = False) -> None:
        self.providers.insert(0, provider) if first else self.providers.append(provider)

    def preview_provider(self, tier: str) -> Any:
        return self.providers[0] if self.providers else None


@pytest.fixture
def variant(monkeypatch):
    asked: list[tuple[str, str]] = []

    def use(name: str) -> None:
        target, key, model = VARIANTS[name]
        monkeypatch.setattr(read_target, "resolve_read_target", lambda: target)
        monkeypatch.setattr(
            read_target, "bound_key_for", lambda url: key if url == target.face_url else ""
        )

        def node_model(face_url: str, k: str, **_kw: Any) -> tuple[str, str]:
            asked.append((face_url, k))
            return ({"https://a.example": "model-a", "https://b.example": "model-b"}[face_url], "")

        monkeypatch.setattr(bound_model, "_node_model", node_model)

    use.asked = asked  # type: ignore[attr-defined]
    return use


@pytest.mark.parametrize("name", ["product", "site"])
def test_mvp_11_a3_each_bound_variant_resolves_its_own_sites_model(variant, name: str) -> None:
    target, key, model = VARIANTS[name]
    variant(name)
    bound, got_key = bound_model.resolve_bound_model()
    assert (bound.site, bound.model, bound.endpoint) == (
        target.site,
        model,
        f"{target.face_url}/v1",
    )
    assert got_key == key
    assert variant.asked == [(target.face_url, key)], "the key went to its own node only"


def test_mvp_11_a3_two_variants_answer_on_two_models_and_neither_leaks(variant) -> None:
    gateways = {}
    for name in ("product", "site"):
        variant(name)
        gw = _Gateway()
        bound_model.apply_bound_model(gw)
        gateways[name] = gw
    a, b = gateways["product"].providers[0], gateways["site"].providers[0]
    assert (a.model, a.endpoint, a.node_host) == ("model-a", "https://a.example/v1", "a.example")
    assert (b.model, b.endpoint, b.node_host) == ("model-b", "https://b.example/v1", "b.example")
    assert len(gateways["product"].providers) == len(gateways["site"].providers) == 1


def test_mvp_11_a3_the_variant_holding_the_medallion_is_sent_nowhere(variant) -> None:
    variant("axiom")
    assert bound_model.resolve_bound_model() is None
    gw = _Gateway()
    assert bound_model.apply_bound_model(gw) is None
    assert gw.providers == [] and variant.asked == []


def test_mvp_11_a3_a_person_s_own_choice_still_wins_in_a_bound_variant(variant) -> None:
    variant("product")
    gw = _Gateway()
    gw._provider_override = "their-own"
    assert bound_model.apply_bound_model(gw) is None
    assert gw.providers == []
