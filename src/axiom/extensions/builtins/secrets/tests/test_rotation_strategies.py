# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Concrete rotation strategies (ADR-095): the three shapes.

- ``ProviderNativeRotation`` — the backend rotates itself (AWS SM / Vault);
  we trigger it and let it age out the old version.
- ``HitlRotation`` — the vendor API can't mint (GitHub classic PATs, OpenAI,
  Anthropic, Qwen): a human supplies the new value behind a single confirm;
  we stage it and notify to revoke the old one.
- ``SendGridRotation`` — the vendor API mints/revokes (the general vendor-API
  shape): mint a new key, stage it, then delete the superseded managed keys.

All exercised against fakes — no live backend, no vendor API.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.secrets.providers.protocol import SecretRef
from axiom.extensions.builtins.secrets.rotation import (
    RotationPolicy,
    RotationStrategy,
)
from axiom.extensions.builtins.secrets.rotation.strategies import (
    HitlRotation,
    ProviderNativeRotation,
    RotationError,
    SendGridRotation,
)

REF = SecretRef.parse("openbao://kv/sendgrid-api-key")
POLICY = RotationPolicy(cadence_seconds=None, overlap_seconds=3600)


class RotatingStore:
    def __init__(self) -> None:
        self.rotated: list[str] = []
        self.puts: list[bytes] = []
        self.overlap_reads = 0

    def rotate(self, ref) -> None:
        self.rotated.append(ref.path)

    def put(self, ref, value) -> None:
        self.puts.append(value)

    def resolve_overlap(self, ref):
        self.overlap_reads += 1
        return []


# --- provider-native --------------------------------------------------------


class TestProviderNativeRotation:
    def test_perform_triggers_backend_rotate(self):
        store = RotatingStore()
        out = ProviderNativeRotation().perform(REF, store, now=1000.0, policy=POLICY)
        assert store.rotated == ["kv/sendgrid-api-key"]
        assert out.strategy == "provider-native"
        assert out.old_valid_until == 1000.0 + 3600

    def test_revoke_previous_is_noop(self):
        store = RotatingStore()
        strat = ProviderNativeRotation()
        out = strat.perform(REF, store, now=1000.0, policy=POLICY)
        strat.revoke_previous(REF, store, out)  # backend ages out old; no error
        assert store.puts == []  # provider-native never writes through us

    def test_satisfies_strategy_protocol(self):
        assert isinstance(ProviderNativeRotation(), RotationStrategy)


# --- HITL --------------------------------------------------------------------


class TestHitlRotation:
    def test_perform_stages_human_value(self):
        store = RotatingStore()
        strat = HitlRotation(value_provider=lambda ref: b"human-pasted-key")
        out = strat.perform(REF, store, now=1000.0, policy=POLICY)
        assert store.puts == [b"human-pasted-key"]
        assert out.strategy == "hitl"

    def test_empty_human_value_raises(self):
        strat = HitlRotation(value_provider=lambda ref: b"")
        with pytest.raises(RotationError):
            strat.perform(REF, RotatingStore(), now=1000.0, policy=POLICY)

    def test_revoke_previous_notifies_human(self):
        notes: list[str] = []
        strat = HitlRotation(
            value_provider=lambda ref: b"x", notifier=notes.append
        )
        out = strat.perform(REF, RotatingStore(), now=1000.0, policy=POLICY)
        strat.revoke_previous(REF, RotatingStore(), out)
        assert len(notes) == 1
        assert "revoke" in notes[0].lower()

    def test_satisfies_strategy_protocol(self):
        assert isinstance(
            HitlRotation(value_provider=lambda ref: b"x"), RotationStrategy
        )


# --- SendGrid (vendor-API shape) --------------------------------------------


class FakeSendGrid:
    """Models SendGrid's /v3/api_keys create/list/delete."""

    def __init__(self) -> None:
        self._keys: dict[str, dict] = {}
        self._n = 0
        self.deleted: list[str] = []

    def post(self, path: str, body: dict) -> dict:
        assert path == "/v3/api_keys"
        self._n += 1
        kid = f"KID{self._n}"
        self._keys[kid] = {"api_key_id": kid, "name": body["name"]}
        return {"api_key_id": kid, "api_key": f"SG.{kid}.secret"}

    def get(self, path: str) -> dict:
        assert path == "/v3/api_keys"
        return {"result": list(self._keys.values())}

    def delete(self, path: str) -> None:
        kid = path.rsplit("/", 1)[-1]
        self._keys.pop(kid, None)
        self.deleted.append(kid)


class TestSendGridRotation:
    def test_perform_mints_and_stages_new_key(self):
        http = FakeSendGrid()
        store = RotatingStore()
        strat = SendGridRotation(http=http, key_name="axiom-managed:sendgrid")
        out = strat.perform(REF, store, now=1000.0, policy=POLICY)
        assert store.puts == [b"SG.KID1.secret"]
        assert out.new_handle == "KID1"
        assert out.old_valid_until == 1000.0 + 3600  # old key still valid

    def test_revoke_previous_deletes_only_superseded_keys(self):
        http = FakeSendGrid()
        store = RotatingStore()
        strat = SendGridRotation(http=http, key_name="axiom-managed:sendgrid")
        # first rotation → KID1 minted
        out1 = strat.perform(REF, store, now=1000.0, policy=POLICY)
        # second rotation → KID2 minted; now KID1 is the superseded one
        out2 = strat.perform(REF, store, now=2000.0, policy=POLICY)
        strat.revoke_previous(REF, store, out2)
        # KID1 deleted (superseded), KID2 (current) kept
        assert http.deleted == ["KID1"]
        assert "KID2" in http._keys and "KID1" not in http._keys
        # keeping out1 referenced so the intent (KID1 was the prior current) is legible
        assert out1.new_handle == "KID1"

    def test_revoke_never_touches_unmanaged_keys(self):
        http = FakeSendGrid()
        # a pre-existing key under a different name must be left alone
        http._keys["EXT"] = {"api_key_id": "EXT", "name": "someone-elses-key"}
        store = RotatingStore()
        strat = SendGridRotation(http=http, key_name="axiom-managed:sendgrid")
        out = strat.perform(REF, store, now=1000.0, policy=POLICY)
        strat.revoke_previous(REF, store, out)
        assert "EXT" in http._keys  # untouched
        assert http.deleted == []  # only KID1 exists under our name, and it's current

    def test_satisfies_strategy_protocol(self):
        assert isinstance(
            SendGridRotation(http=FakeSendGrid(), key_name="x"), RotationStrategy
        )
