"""A provider's key is read from the Axiom vault, where the rule says it lives.

The gateway read a key from the environment, then from the connections
credential files, and never from the vault `axi secrets set` writes to. So a
key stored the way the platform asks for was invisible to it, and the only way
to make chat work was to export the key into a shell, which is the exposure the
vault exists to prevent.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from axiom.llm import gateway as gw


class _Secret:
    def __init__(self, value):
        self._v = value

    def as_str(self):
        return self._v


class _Vault:
    def __init__(self, values):
        self.values = values
        self.reads = 0

    @contextmanager
    def get(self, name):
        self.reads += 1
        if name not in self.values:
            raise KeyError(name)
        yield _Secret(self.values[name])


@pytest.fixture
def vault(monkeypatch):
    v = _Vault({"private-model": "from-the-vault"})
    monkeypatch.setattr(gw, "_open_vault", lambda: v)
    return v


def _provider(**kw):
    return gw.LLMProvider(name="private-model", endpoint="https://llm.example", model="m",
                          api_key_env="PRIVATE_MODEL_KEY", **kw)


def test_the_vault_supplies_a_key_the_environment_does_not(vault, monkeypatch):
    monkeypatch.delenv("PRIVATE_MODEL_KEY", raising=False)
    assert _provider().api_key == "from-the-vault"


def test_the_environment_still_wins_when_set(vault, monkeypatch):
    monkeypatch.setenv("PRIVATE_MODEL_KEY", "from-env")
    assert _provider().api_key == "from-env"
    assert vault.reads == 0


def test_the_vault_is_read_once_per_provider(vault, monkeypatch):
    monkeypatch.delenv("PRIVATE_MODEL_KEY", raising=False)
    p = _provider()
    for _ in range(5):
        assert p.api_key == "from-the-vault"
    assert vault.reads == 1


def test_a_key_is_resolved_by_this_providers_name_only(vault, monkeypatch):
    """An export-controlled provider must never pick up a key stored for another."""
    monkeypatch.delenv("OTHER_KEY", raising=False)
    other = gw.LLMProvider(name="some-public-api", endpoint="https://x", model="m", api_key_env="OTHER_KEY")
    assert other.api_key is None or other.api_key != "from-the-vault"


def test_an_unreachable_vault_is_not_an_error(monkeypatch):
    def broken():
        raise RuntimeError("keychain locked")

    monkeypatch.setattr(gw, "_open_vault", broken)
    monkeypatch.delenv("PRIVATE_MODEL_KEY", raising=False)
    assert _provider().api_key is None or isinstance(_provider().api_key, str)
