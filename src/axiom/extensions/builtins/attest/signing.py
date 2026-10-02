# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The node signer the skills use, behind a seam tests can bind.

Production signs with the node identity key through its custody operation
(:func:`axiom.vega.identity.node_key.node_signer`), which fails closed.
"""

from __future__ import annotations

from collections.abc import Callable

from axiom.attest.chain import Signer


def _default() -> Signer:
    from axiom.vega.identity.node_key import node_signer

    return node_signer()


def _default_keys() -> dict[str, bytes]:
    # Verification reads the published public key; it never loads the private one.
    import base64

    from axiom.vega.federation.identity import load_identity

    identity = load_identity()
    if identity is None:
        return {}
    return {f"node:{identity.node_id}": base64.b64decode(identity.public_key)}


_provider: Callable[[], Signer] = _default
_keys: Callable[[], dict[str, bytes]] = _default_keys


def set_provider(
    provider: Callable[[], Signer], keys: Callable[[], dict[str, bytes]] | None = None
) -> None:
    global _provider, _keys
    _provider = provider
    _keys = keys or (lambda: {provider().key_id: provider().public_bytes})  # type: ignore[attr-defined]


def reset_provider() -> None:
    global _provider, _keys
    _provider = _default
    _keys = _default_keys


def signer() -> Signer:
    return _provider()


def public_keys() -> dict[str, bytes]:
    """Keys that verify this node's records. Retired keys join this map when
    rotation lands (ADR-143, key rotation)."""
    return dict(_keys())
