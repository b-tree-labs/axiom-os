# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The local principal's Ed25519 keypair (IDENT-4, ADR-074 §5).

Load-or-create the keypair that authenticates "me" at the ``attested`` posture,
custodied per the chosen backend (default: OS keychain). Loading the key is the
unlock — the OS gates keychain access (Touch ID / login), so possessing the
running process is no longer sufficient to *be* the principal.
"""

from __future__ import annotations

from typing import Optional

from axiom.vega.identity.custody import CustodyBackend, KeychainCustody
from axiom.vega.identity.keypair import Keypair, generate_keypair

DEFAULT_KEY_ID = "axiom.identity.local.ed25519"


def load_or_create_local_keypair(
    *, custody: Optional[CustodyBackend] = None, key_id: str = DEFAULT_KEY_ID
) -> Keypair:
    """Return the local principal's keypair, generating + custodying it on first
    use. Stable across sessions: the same private key (and thus public identity)
    is returned every time."""
    backend = custody or KeychainCustody()
    raw = backend.get(key_id)
    if raw is None:
        keypair = generate_keypair()
        backend.put(key_id, keypair.export_private())
        return keypair
    return Keypair.from_private_bytes(raw)


__all__ = ["DEFAULT_KEY_ID", "load_or_create_local_keypair"]
