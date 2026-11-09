# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Anchors: a signed Merkle root over a site's logbook heads (ADR-143).

A chain proves that nothing changed *unless the forger holds the node key*,
who can rewrite a record and re-sign everything after it. An anchor fixes
every head at a moment; once it is printed or held elsewhere, that rewrite no
longer matches it.

Each head is ``{logbook, seq, digest}``. Leaves are sorted by logbook and hashed with
a ``0x00`` prefix, inner nodes with ``0x01`` (as RFC 6962 does), and an odd node
is promoted unchanged rather than paired with itself, so no two different
head sets share a root. The node signs :data:`ANCHOR_DOMAIN` followed by the
32 root bytes; the prefix keeps anchor and record signatures apart.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from axiom.attest.canonical import canonical_bytes
from axiom.attest.chain import Signer
from axiom.vega.identity.keypair import verify as ed25519_verify

ANCHOR_DOMAIN: bytes = b"axiom/attest/anchor/v1\n"


def _leaf(head: Mapping[str, Any]) -> bytes:
    body = {"logbook": head["logbook"], "seq": head["seq"], "digest": head["digest"]}
    return hashlib.sha256(b"\x00" + canonical_bytes(body)).digest()


def merkle_root(heads: Iterable[Mapping[str, Any]]) -> str:
    """Hex Merkle root over ``heads``, in logbook order."""
    ordered = sorted(heads, key=lambda h: h["logbook"])
    if not ordered:
        raise ValueError("an anchor needs at least one logbook head")
    logbooks = [h["logbook"] for h in ordered]
    if len(set(logbooks)) != len(logbooks):
        raise ValueError("a logbook appears twice among the heads")
    level = [_leaf(h) for h in ordered]
    while len(level) > 1:
        nxt = [
            hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest()
            for i in range(0, len(level) - 1, 2)
        ]
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0].hex()


def anchor_message(root: str) -> bytes:
    return ANCHOR_DOMAIN + bytes.fromhex(root)


def sign_anchor(root: str, signer: Signer) -> dict[str, str]:
    return {
        "alg": "ed25519",
        "key_id": signer.key_id,
        "sig": base64.b64encode(signer.sign(anchor_message(root))).decode("ascii"),
    }


def verify_anchor(
    root: str,
    heads: Iterable[Mapping[str, Any]],
    node_sig: Mapping[str, str],
    public_keys: Mapping[str, bytes],
) -> tuple[bool, str | None]:
    """``(True, None)``, or ``(False, reason)`` with reason one of
    ``root_mismatch``, ``unknown_key``, ``bad_signature`` or ``malformed``."""
    try:
        if merkle_root(heads) != root:
            return False, "root_mismatch"
    except ValueError:
        return False, "root_mismatch"
    if node_sig.get("alg") != "ed25519":
        return False, "malformed"
    public = public_keys.get(node_sig.get("key_id", ""))
    if public is None:
        return False, "unknown_key"
    try:
        raw = base64.b64decode(node_sig.get("sig", ""), validate=True)
        good = ed25519_verify(public, anchor_message(root), raw)
    except ValueError:
        return False, "malformed"
    return (True, None) if good else (False, "bad_signature")


__all__ = ["ANCHOR_DOMAIN", "anchor_message", "merkle_root", "sign_anchor", "verify_anchor"]
