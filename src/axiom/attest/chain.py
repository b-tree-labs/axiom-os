# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The signed hash chain (ADR-143).

Each record carries ``seq`` (gapless within its chain), ``prev_digest`` (the
previous record's digest, or :data:`GENESIS`), its own ``digest`` and a
``node_sig``. The node signs a domain-separated message, :data:`SIGNING_DOMAIN`
followed by the 32 digest bytes, so an attestation signature can never be
replayed as a signature of some other kind by the same key.

Verification needs only public keys. That is the point of using public-key
signatures rather than an HMAC: whoever verifies cannot also forge.
"""

from __future__ import annotations

import base64
import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from axiom.attest.canonical import SIGNATURE_FIELDS, digest
from axiom.vega.identity.keypair import Keypair
from axiom.vega.identity.keypair import verify as ed25519_verify

#: ``prev_digest`` of the first record in every chain.
GENESIS: str = "0" * 64

#: Prefix of every message the node signs for an attestation.
SIGNING_DOMAIN: bytes = b"axiom/attest/v1\n"

_CHAIN_FIELDS = ("seq", "prev_digest", *SIGNATURE_FIELDS)


def signing_message(record_digest: str) -> bytes:
    """What the node actually signs for a record with this digest."""
    return SIGNING_DOMAIN + bytes.fromhex(record_digest)


class Signer(Protocol):
    """Anything that can sign with a named key. Key custody implements this
    so the private key never leaves it (ADR-143)."""

    key_id: str

    def sign(self, message: bytes) -> bytes: ...


@dataclass
class Ed25519Signer:
    """A signer over an in-process keypair. Used by tests and by custody
    backends that hold the key in this process."""

    key_id: str
    keypair: Keypair

    @property
    def public_bytes(self) -> bytes:
        return self.keypair.public_bytes

    def sign(self, message: bytes) -> bytes:
        return self.keypair.sign(message)


def seal(body: dict[str, Any], *, seq: int, prev_digest: str, signer: Signer) -> dict[str, Any]:
    """Return a new record: ``body`` placed at ``seq`` after ``prev_digest``,
    digested and signed. ``body`` is not modified."""
    clash = [k for k in _CHAIN_FIELDS if k in body]
    if clash:
        raise ValueError(f"body already carries chain fields {clash}; seal sets them")
    if seq < 1:
        raise ValueError("seq starts at 1")
    if len(prev_digest) != 64:
        raise ValueError("prev_digest must be a 64-character hex digest")
    record = copy.deepcopy(body)
    record["seq"] = seq
    record["prev_digest"] = prev_digest
    d = digest(record)
    sig = signer.sign(signing_message(d))
    record["digest"] = d
    record["node_sig"] = {
        "alg": "ed25519",
        "key_id": signer.key_id,
        "sig": base64.b64encode(sig).decode("ascii"),
    }
    return record


@dataclass(frozen=True)
class VerifyReport:
    """Outcome of verifying a run of records.

    ``reason`` is one of ``seq_gap``, ``prev_mismatch``, ``digest_mismatch``,
    ``unknown_key``, ``bad_signature`` or ``malformed``; ``None`` when ``ok``.
    """

    ok: bool
    checked: int
    head_seq: int | None
    head_digest: str | None
    first_bad_seq: int | None = None
    reason: str | None = None


def verify_chain(
    records: Iterable[Mapping[str, Any]],
    public_keys: Mapping[str, bytes],
    *,
    start_seq: int = 1,
    start_prev: str = GENESIS,
) -> VerifyReport:
    """Verify records in order, starting at ``start_seq`` after ``start_prev``
    (an anchor), and stop at the first broken record."""
    expected_seq = start_seq
    prev = start_prev
    checked = 0
    head_seq: int | None = None
    head_digest: str | None = None

    def fail(seq: Any, reason: str) -> VerifyReport:
        return VerifyReport(False, checked, head_seq, head_digest, seq, reason)

    for rec in records:
        seq = rec.get("seq")
        if seq != expected_seq:
            return fail(seq, "seq_gap")
        if rec.get("prev_digest") != prev:
            return fail(seq, "prev_mismatch")
        stored = rec.get("digest")
        if not isinstance(stored, str) or digest(dict(rec)) != stored:
            return fail(seq, "digest_mismatch")
        sig = rec.get("node_sig") or {}
        if sig.get("alg") != "ed25519":
            return fail(seq, "malformed")
        public = public_keys.get(sig.get("key_id", ""))
        if public is None:
            return fail(seq, "unknown_key")
        try:
            raw = base64.b64decode(sig.get("sig", ""), validate=True)
        except (ValueError, TypeError):
            return fail(seq, "malformed")
        try:
            good = ed25519_verify(public, signing_message(stored), raw)
        except ValueError:
            # A malformed public key or a signature of the wrong length is a
            # failed verification, not an error that aborts the walk.
            good = False
        if not good:
            return fail(seq, "bad_signature")
        checked += 1
        head_seq, head_digest = seq, stored
        prev = stored
        expected_seq += 1

    return VerifyReport(True, checked, head_seq, head_digest)


__all__ = [
    "GENESIS",
    "SIGNING_DOMAIN",
    "Ed25519Signer",
    "Signer",
    "VerifyReport",
    "seal",
    "signing_message",
    "verify_chain",
]
