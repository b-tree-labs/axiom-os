# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Ed25519 keypair helpers.

Thin wrapper over the `cryptography` library so the rest of Axiom can
stay agnostic. Public bytes are the raw 32-byte form; private export is
also raw bytes. No PEM, no DER — keep the surface small.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


@dataclass
class Keypair:
    _private: Ed25519PrivateKey
    public_bytes: bytes

    def sign(self, message: bytes) -> bytes:
        return self._private.sign(message)

    def export_private(self) -> bytes:
        return self._private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

    @classmethod
    def from_private_bytes(cls, data: bytes) -> Keypair:
        priv = Ed25519PrivateKey.from_private_bytes(data)
        pub = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return cls(_private=priv, public_bytes=pub)


def generate_keypair() -> Keypair:
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return Keypair(_private=priv, public_bytes=pub)


def verify(public_bytes: bytes, message: bytes, signature: bytes) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(public_bytes)
        pub.verify(signature, message)
        return True
    except InvalidSignature:
        return False
