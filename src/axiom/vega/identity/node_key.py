# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signing with the node's federation identity key (ADR-143).

:func:`node_signer` is the one custody operation for that key. It returns an
object that can ``sign`` and report public material, and nothing else: callers
such as the attestation chain never hold the private key.

It fails closed. A node with no identity, no private key, unreadable key
material, or a key that does not match the public key it publishes raises
:class:`NodeKeyUnavailable`; it never generates a replacement, because a
freshly minted key would sign records nobody can verify against the directory.

The key is loaded into this process today. Moving custody out of process (an
agent socket or a hardware token) replaces this module's internals; callers
depend only on the ``Signer`` shape.
"""

from __future__ import annotations

import base64
from pathlib import Path

from axiom.vega.federation.identity import load_identity
from axiom.vega.identity.keypair import Keypair


class NodeKeyUnavailable(RuntimeError):
    """The node cannot sign. Signing must stop, not fall back."""


class NodeKeySigner:
    """Signs with the node identity key. Build it with :func:`node_signer`."""

    __slots__ = ("_keypair", "key_id", "public_bytes")

    def __init__(self, key_id: str, keypair: Keypair) -> None:
        self.key_id = key_id
        self.public_bytes = keypair.public_bytes
        self._keypair = keypair

    def sign(self, message: bytes) -> bytes:
        return self._keypair.sign(message)

    def public_keys(self) -> dict[str, bytes]:
        """``{key_id: public key}`` for verifying what this signer signed."""
        return {self.key_id: self.public_bytes}

    def __repr__(self) -> str:
        return f"NodeKeySigner(key_id={self.key_id!r})"


def node_signer(keys_dir: Path | None = None) -> NodeKeySigner:
    """Return a signer over the node identity key in ``keys_dir`` (default
    ``~/.axi/identity``), or raise :class:`NodeKeyUnavailable`."""
    identity = load_identity(keys_dir)
    if identity is None:
        raise NodeKeyUnavailable("no node identity; run node setup before signing")

    try:
        pem = identity.private_key_path.read_bytes()
    except OSError as exc:
        raise NodeKeyUnavailable(
            f"node private key unreadable at {identity.private_key_path}: {exc.strerror}"
        ) from None

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            load_pem_private_key,
        )

        key = load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("not an Ed25519 key")
        raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    except (ValueError, TypeError):
        raise NodeKeyUnavailable("node private key cannot be loaded as Ed25519") from None

    keypair = Keypair.from_private_bytes(raw)
    if keypair.public_bytes != base64.b64decode(identity.public_key):
        raise NodeKeyUnavailable(
            "node private key does not match the published public key; "
            "refusing to sign records the directory cannot verify"
        )
    return NodeKeySigner(f"node:{identity.node_id}", keypair)


__all__ = ["NodeKeySigner", "NodeKeyUnavailable", "node_signer"]
