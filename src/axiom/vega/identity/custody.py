# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Pluggable custody for the local principal's private key (IDENT-8, ADR-074 §5b).

Custody is an adapter so the same ``attested`` posture works across security
models: ``keychain`` (default — OS keychain), ``memory`` (tests / ephemeral),
and later ``badge`` (key derived on-demand from biometric — no secret at rest)
and ``hardware`` (token/TPM). The contract is deliberately tiny: get/put raw
private bytes by id; the backend decides where they live (or whether they ever
exist at rest).
"""

from __future__ import annotations

import base64
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class CustodyBackend(Protocol):
    name: str

    def get(self, key_id: str) -> Optional[bytes]: ...

    def put(self, key_id: str, data: bytes) -> None: ...


class InMemoryCustody:
    """Ephemeral, process-local custody — for tests and `open`/throwaway nodes."""

    name = "memory"

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def get(self, key_id: str) -> Optional[bytes]:
        return self._store.get(key_id)

    def put(self, key_id: str, data: bytes) -> None:
        self._store[key_id] = data


class KeychainCustody:
    """OS keychain custody (macOS Keychain / Windows DPAPI / Linux Secret-Service)
    via the platform keychain path; the private key is never on disk in clear."""

    name = "keychain"

    def get(self, key_id: str) -> Optional[bytes]:
        from axiom.setup.secrets import get_secret

        value = get_secret(key_id)
        return base64.b64decode(value) if value else None

    def put(self, key_id: str, data: bytes) -> None:
        from axiom.setup.secrets import store_secret

        store_secret(key_id, base64.b64encode(data).decode("ascii"))


class BadgeCustody:
    """[SPIKE — ADR-076 gate (b), pending vendor evaluation] Privacy-preserving
    custody: the key is **derived on-demand from the user's biometric** — no
    secret at rest, nothing to sync (the differentiator). Implements the contract
    so the architecture is proven, but the Badge SDK integration is intentionally
    not wired: ``get`` raises so it can't be used unintentionally; ``put`` is a
    no-op by design (Badge derives, never stores)."""

    name = "badge"

    def get(self, key_id: str) -> Optional[bytes]:
        raise NotImplementedError(
            "Badge custody is a spike (ADR-076 gate b): on-demand biometric key "
            "derivation pending SDK + account evaluation. Use 'keychain' for now."
        )

    def put(self, key_id: str, data: bytes) -> None:
        return None  # Badge derives keys; it never stores them.


__all__ = ["BadgeCustody", "CustodyBackend", "InMemoryCustody", "KeychainCustody"]
