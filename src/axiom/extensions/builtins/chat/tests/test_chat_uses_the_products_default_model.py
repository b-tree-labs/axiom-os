# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A product can name the model its people should chat with, and chat uses it.

On a laptop, chat answered questions about a deployment's data with a small
model running on that laptop, which had none of that data. The product knew
better: it knew which deployment the install belonged to and where that
deployment serves chat. It had no way to say so, so the laptop's local model
answered anyway.

``BrandingConfig.chat_provider_fn`` lets a product name that model. It is the
default, not a lock: ``--local`` (or ``--provider``) uses the machine's own
models instead, and a product default with no key in the vault is not used at
all. Asking it would only fail with 401, and the product's own start lines
already say how to get a key.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat.product_default import apply_product_default
from axiom.extensions.builtins.chat.start_context import start_context_lines
from axiom.llm import gateway as gw

SPEC = {
    "name": "deployment chat",
    "endpoint": "https://node.example.org/v1",
    "model": "deployment-assistant",
    "api_key_vault": "node-key",
}


class _Secret:
    def __init__(self, value):
        self._v = value

    def as_str(self):
        return self._v


class _Vault:
    """The OS keychain boundary, which tests never reach."""

    def __init__(self, values):
        self.values = values

    @contextmanager
    def get(self, name):
        if name not in self.values:
            raise KeyError(name)
        yield _Secret(self.values[name])


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    (tmp_path / "llm-providers.toml").write_text(
        '[gateway]\n[[gateway.providers]]\nname = "laptop"\n'
        'endpoint = "http://localhost:11434/v1"\nmodel = "small"\npriority = 1\n'
        'use_for = ["chat", "fallback"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(gw.Gateway, "_discover_local_llm", lambda self: None)
    return gw.Gateway(config_dir=tmp_path)


def _brand(spec=SPEC):
    return SimpleNamespace(cli_name="prod", chat_provider_fn=lambda: spec, chat_context_fn=None)


def test_with_a_key_the_products_model_answers(gateway, monkeypatch):
    monkeypatch.setattr(gw, "_open_vault", lambda: _Vault({"node-key": "k"}))
    assert apply_product_default(gateway, local=False, brand=_brand()) == "deployment chat"
    active = gateway.active_provider
    assert active.name == "deployment chat"
    assert active.api_key == "k"
    line = start_context_lines(active, brand=_brand())[0]
    assert "deployment-assistant" in line and "served from node.example.org" in line


def test_the_products_model_keeps_one_identity_and_asks_for_no_config(gateway, monkeypatch, caplog):
    monkeypatch.setattr(gw, "_open_vault", lambda: _Vault({"node-key": "k"}))
    apply_product_default(gateway, local=False, brand=_brand())
    first = gateway.active_provider.uid
    assert "has no 'uid'" not in caplog.text
    again = gw.Gateway(config_dir=gateway.config_dir)
    apply_product_default(again, local=False, brand=_brand())
    assert again.active_provider.uid == first


def test_local_keeps_the_machines_own_model(gateway, monkeypatch):
    monkeypatch.setattr(gw, "_open_vault", lambda: _Vault({"node-key": "k"}))
    assert apply_product_default(gateway, local=True, brand=_brand()) is None
    assert gateway.active_provider.name == "laptop"


def test_an_explicit_provider_choice_wins(gateway, monkeypatch):
    monkeypatch.setattr(gw, "_open_vault", lambda: _Vault({"node-key": "k"}))
    gateway.set_provider_override("laptop")
    assert apply_product_default(gateway, local=False, brand=_brand()) is None


def test_without_a_key_the_product_default_is_not_used(gateway, monkeypatch):
    monkeypatch.setattr(gw, "_open_vault", lambda: _Vault({}))
    assert apply_product_default(gateway, local=False, brand=_brand()) is None
    assert gateway.active_provider.name == "laptop"


def test_a_product_that_names_nothing_changes_nothing(gateway, monkeypatch):
    monkeypatch.setattr(gw, "_open_vault", lambda: _Vault({"node-key": "k"}))
    assert apply_product_default(gateway, local=False, brand=_brand(None)) is None

    def broken():
        raise RuntimeError("hook failed")

    brand = SimpleNamespace(cli_name="prod", chat_provider_fn=broken)
    assert apply_product_default(gateway, local=False, brand=brand) is None
    assert gateway.active_provider.name == "laptop"


def test_the_vault_entry_is_named_by_the_product_not_the_display_name(monkeypatch):
    monkeypatch.setattr(gw, "_open_vault", lambda: _Vault({"node-key": "k"}))
    named = gw.LLMProvider(name="deployment chat", endpoint="https://x", model="m", api_key_vault="node-key")
    assert named.api_key == "k" and named.is_usable
    missing = gw.LLMProvider(name="deployment chat", endpoint="https://x", model="m", api_key_vault="absent")
    assert missing.api_key is None and not missing.is_usable


def test_chat_takes_local():
    from axiom.extensions.builtins.chat.cli import get_parser

    assert get_parser().parse_args(["--local"]).local is True
    assert get_parser().parse_args([]).local is False
