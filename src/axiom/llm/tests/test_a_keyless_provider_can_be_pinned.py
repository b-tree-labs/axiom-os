# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``set_provider_override`` pins a keyless local provider, as ``prefer=`` and ``preview_provider`` already do.

Selection checked ``p.api_key``, so a provider that declares it needs no key (empty ``api_key_env``, for example a
local Ollama server) could never be pinned: the override was skipped and the request went to another provider, while
``preview_provider`` predicted the pinned one. A provider that needs a key and has none is still skipped.
"""

from __future__ import annotations

from tests.test_gateway import _gateway_with_providers, _make_provider


def _providers():
    return [
        _make_provider(name="cloud", routing_tier="public", priority=1),
        _make_provider(name="local-vision", api_key_env="", endpoint="http://localhost:11434/v1",
                       routing_tier="export_controlled", priority=5),
        _make_provider(name="keyed-but-unset", api_key_env="UNSET_KEY_FOR_TEST", priority=6),
    ]


def test_a_keyless_provider_can_be_pinned(monkeypatch):
    gw = _gateway_with_providers(_providers(), monkeypatch)
    gw.set_provider_override("local-vision")
    assert gw.preview_provider().name == "local-vision"
    assert gw._select_provider("chat").name == "local-vision"


def test_a_pinned_keyless_provider_still_obeys_the_tier(monkeypatch):
    gw = _gateway_with_providers(_providers(), monkeypatch)
    gw.set_provider_override("local-vision")
    chosen = gw._select_provider("chat", routing_tier="export_controlled")
    assert chosen is not None and chosen.name == "local-vision"


def test_a_provider_whose_key_is_missing_is_still_skipped(monkeypatch, capsys):
    monkeypatch.delenv("UNSET_KEY_FOR_TEST", raising=False)
    gw = _gateway_with_providers(_providers(), monkeypatch)
    gw.set_provider_override("keyed-but-unset")
    chosen = gw._select_provider("chat")
    assert chosen is not None and chosen.name != "keyed-but-unset"
    assert "keyed-but-unset" in capsys.readouterr().err
