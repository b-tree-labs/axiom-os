# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rotation engine — the autorotation layer above SecretStore (ADR-003 D3).

Backends split on who owns rotation:

- **Provider-native** (AWS Secrets Manager, Vault dynamic engines): the
  backend rotates and hands us the overlap window for free
  (``resolve_overlap``). Our job is only to *trigger* it.
- **Vendor-API** (SaaS keys — SendGrid, OpenAI, GitHub PATs, LangSmith):
  the backend is dumb storage; rotation means mint-new at the vendor,
  store the new version, keep the old valid through an overlap window,
  then revoke the old at the vendor.

The engine drives both through one ``RotationStrategy`` contract, records
the dual-valid overlap window, defers the revoke of the old credential
until the window closes, and supports a ``force`` path (rotate now,
regardless of cadence) — the leaked-key closer.

These tests use a fake strategy + a deterministic injected clock, so no
vendor API, real backend, or wall-clock is touched.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.secrets.providers.protocol import SecretRef
from axiom.extensions.builtins.secrets.rotation import (
    NotDue,
    RotationEngine,
    RotationOutcome,
    RotationPolicy,
    RotationRegistry,
    RotationStrategy,
)


# --- test doubles -----------------------------------------------------------


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeVendorStrategy:
    """Mints a monotonically-numbered key and records vendor-side revocations."""

    kind = "fake-vendor"

    def __init__(self) -> None:
        self.minted: list[bytes] = []
        self.revoked: list[SecretRef] = []
        self._n = 0
        self.stored: dict[str, bytes] = {}

    def perform(self, ref, store, *, now, policy) -> RotationOutcome:
        self._n += 1
        material = f"key-{self._n}".encode()
        self.minted.append(material)
        store.put(ref, material)  # new version becomes current
        overlap = policy.overlap_seconds
        return RotationOutcome(
            ref=ref,
            strategy=self.kind,
            rotated_at=now,
            new_version=self._n,
            old_valid_until=(now + overlap) if overlap else None,
            revoke_at=(now + overlap) if overlap else now,
            forced=False,
        )

    def revoke_previous(self, ref, store, outcome) -> None:
        self.revoked.append(ref)


class FakeStore:
    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes]] = []

    def put(self, ref, value) -> None:
        self.puts.append((ref.path, value))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def strategy() -> FakeVendorStrategy:
    return FakeVendorStrategy()


@pytest.fixture
def engine(clock, store, strategy) -> RotationEngine:
    return RotationEngine(
        resolver=lambda ref: strategy,
        store_for=lambda scheme: store,
        clock=clock,
    )


REF = SecretRef.parse("openbao://kv/sendgrid-api-key")


# --- policy -----------------------------------------------------------------


class TestRotationPolicy:
    def test_force_only_policy_never_due(self):
        p = RotationPolicy(cadence_seconds=None, overlap_seconds=3600)
        assert not p.is_due(last_rotated_at=None, now=10_000)

    def test_never_rotated_is_due_when_cadence_set(self):
        p = RotationPolicy(cadence_seconds=86_400)
        assert p.is_due(last_rotated_at=None, now=10_000)

    def test_due_after_cadence_elapses(self):
        p = RotationPolicy(cadence_seconds=100)
        assert not p.is_due(last_rotated_at=1000, now=1099)
        assert p.is_due(last_rotated_at=1000, now=1100)


# --- engine: mint + store + overlap -----------------------------------------


class TestRotateMintsAndStores:
    def test_rotate_mints_and_stores_new_version(self, engine, store, strategy):
        policy = RotationPolicy(cadence_seconds=None, overlap_seconds=3600)
        outcome = engine.rotate(REF, policy=policy, force=True)
        assert strategy.minted == [b"key-1"]
        assert store.puts == [("kv/sendgrid-api-key", b"key-1")]
        assert outcome.new_version == 1
        assert outcome.rotated_at == 1000.0

    def test_overlap_window_recorded_and_revoke_deferred(self, engine, strategy):
        policy = RotationPolicy(cadence_seconds=None, overlap_seconds=3600)
        outcome = engine.rotate(REF, policy=policy, force=True)
        # old credential stays valid through the window; not revoked yet
        assert outcome.old_valid_until == 1000.0 + 3600
        assert outcome.revoke_at == 1000.0 + 3600
        assert strategy.revoked == []
        assert engine.pending_revocations() == 1

    def test_zero_overlap_revokes_immediately(self, engine, strategy):
        policy = RotationPolicy(cadence_seconds=None, overlap_seconds=0)
        engine.rotate(REF, policy=policy, force=True)
        assert strategy.revoked == [REF]
        assert engine.pending_revocations() == 0


# --- engine: cadence gating + force -----------------------------------------


class TestCadenceAndForce:
    def test_rotate_refuses_when_not_due_without_force(self, engine):
        policy = RotationPolicy(cadence_seconds=100, overlap_seconds=0)
        with pytest.raises(NotDue):
            engine.rotate(REF, policy=policy, last_rotated_at=1000, force=False)

    def test_force_rotates_even_when_not_due(self, engine, strategy):
        policy = RotationPolicy(cadence_seconds=100, overlap_seconds=0)
        engine.rotate(REF, policy=policy, last_rotated_at=1000, force=True)
        assert strategy.minted == [b"key-1"]

    def test_due_rotation_proceeds_without_force(self, engine, strategy):
        policy = RotationPolicy(cadence_seconds=100, overlap_seconds=0)
        engine.rotate(REF, policy=policy, last_rotated_at=800, force=False)  # now=1000
        assert strategy.minted == [b"key-1"]


# --- engine: deferred revocation sweep --------------------------------------


class TestDeferredRevocation:
    def test_run_due_revocations_waits_for_window(self, engine, clock, strategy):
        policy = RotationPolicy(cadence_seconds=None, overlap_seconds=3600)
        engine.rotate(REF, policy=policy, force=True)  # revoke_at = 4600
        clock.t = 4599
        assert engine.run_due_revocations() == []
        assert strategy.revoked == []
        clock.t = 4600
        assert engine.run_due_revocations() == [REF]
        assert strategy.revoked == [REF]
        assert engine.pending_revocations() == 0

    def test_run_due_revocations_is_idempotent(self, engine, clock, strategy):
        policy = RotationPolicy(cadence_seconds=None, overlap_seconds=10)
        engine.rotate(REF, policy=policy, force=True)
        clock.t = 2000
        engine.run_due_revocations()
        assert engine.run_due_revocations() == []  # nothing left
        assert strategy.revoked == [REF]


# --- registry ---------------------------------------------------------------


class TestRotationRegistry:
    def test_register_and_get(self):
        reg = RotationRegistry()
        reg.register(FakeVendorStrategy)
        assert reg.get("fake-vendor") is FakeVendorStrategy

    def test_unknown_kind_raises(self):
        reg = RotationRegistry()
        with pytest.raises(KeyError):
            reg.get("nope")

    def test_available_kinds_lists_registered(self):
        reg = RotationRegistry()
        reg.register(FakeVendorStrategy)
        assert "fake-vendor" in reg.available_kinds()


def test_strategy_protocol_is_runtime_checkable():
    assert isinstance(FakeVendorStrategy(), RotationStrategy)
