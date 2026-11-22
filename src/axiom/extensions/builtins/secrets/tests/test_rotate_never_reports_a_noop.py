# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A rotation of a stored credential either persists or fails (#838).

``vault rotate <name> --force --value <new>`` was reported to exit clean while
the store kept the old value: the one outcome a leaked-key closer may never
have, because the operator walks away believing a revoked credential is gone.

Two things made that possible. The stored-name flow ignored ``--value``
entirely (it prompted on a terminal and failed headless), and nothing read the
store back after the write, so a backend that accepted the write without
keeping it reported success. Both are pinned here. No value is ever asserted
on output: only the store's own contents are compared.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.secrets import skills as secrets_skills
from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.infra.skills import SkillContext

from .test_foreign_store import InMemoryValueStore


@pytest.fixture(autouse=True)
def _jsonl_ledger(monkeypatch):
    from axiom.policy import action_ledger

    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    action_ledger._reset_backend_cache()
    yield
    action_ledger._reset_backend_cache()


class _WriteIsDropped(InMemoryValueStore):
    """A backend that accepts a write and keeps the old value."""

    def put(self, ref, value):  # noqa: ARG002 — the drop is the point
        if ref.path not in self.values:
            self.values[ref.path] = value


def _ctx(tmp_path, prompt=None):
    return SkillContext(
        registry=secrets_skills.bind_default(),
        state_dir=tmp_path,
        logger=logging.getLogger("test.rotate.noop"),
        user_prompt=prompt,
    )


def _stored(tmp_path, value_store):
    store = ForeignCredentialStore(tmp_path, value_store=value_store)
    store.set("leaked-pat", b"OLD-VALUE", provider="guided", expires_at="2026-09-03")
    return store


def test_value_flag_persists_a_guided_rotation_headless(tmp_path):
    values = InMemoryValueStore()
    store = _stored(tmp_path, values)
    ctx = _ctx(tmp_path, prompt=None)  # no terminal: the flag is the only input

    result = ctx.registry.invoke("secrets.rotate", {
        "ref": "leaked-pat", "_store": store, "force": True,
        "value": "NEW-VALUE", "expires_at": "2026-12-01",
    }, ctx)

    assert result.ok, result.errors
    assert values.values["leaked-pat"] == b"NEW-VALUE"
    assert store.metadata("leaked-pat")["expires_at"] == "2026-12-01"
    assert "NEW-VALUE" not in repr(result.value) + repr(result.actions_taken)


def test_value_flag_is_used_instead_of_a_prompt(tmp_path):
    values = InMemoryValueStore()
    store = _stored(tmp_path, values)
    asked: list[str] = []
    ctx = _ctx(tmp_path, prompt=lambda msg: asked.append(msg) or "PROMPTED")

    result = ctx.registry.invoke("secrets.rotate", {
        "ref": "leaked-pat", "_store": store, "force": True, "value": "NEW-VALUE",
    }, ctx)

    assert result.ok, result.errors
    assert values.values["leaked-pat"] == b"NEW-VALUE"
    assert asked == []


def test_value_flag_is_refused_for_a_provider_that_mints_its_own(tmp_path):
    values = InMemoryValueStore()
    store = ForeignCredentialStore(tmp_path, value_store=values)
    store.set("api-pat", b"OLD-VALUE", provider="gitlab-pat", issuer_url="https://git.example.org")
    ctx = _ctx(tmp_path)

    result = ctx.registry.invoke("secrets.rotate", {
        "ref": "api-pat", "_store": store, "force": True, "value": "NEW-VALUE",
    }, ctx)

    assert not result.ok
    assert result.exit_code != 0
    assert "--value" in " ".join(result.errors)
    assert values.values["api-pat"] == b"OLD-VALUE"


def test_a_write_the_store_did_not_keep_is_a_failure(tmp_path):
    values = _WriteIsDropped()
    store = _stored(tmp_path, values)
    ctx = _ctx(tmp_path, prompt=lambda _m: "NEW-VALUE")

    result = ctx.registry.invoke("secrets.rotate", {
        "ref": "leaked-pat", "_store": store, "force": True,
    }, ctx)

    assert values.values["leaked-pat"] == b"OLD-VALUE"
    assert not result.ok, "rotation reported success while the store kept the old value"
    assert result.exit_code != 0
    assert any("did not persist" in e for e in result.errors), result.errors
    assert "NEW-VALUE" not in " ".join(result.errors)
