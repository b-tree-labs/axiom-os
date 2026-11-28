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

from axiom.attest import _sigcheck
from axiom.attest.canonical import SIGNATURE_FIELDS, digest
from axiom.vega.identity.keypair import Keypair

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
    ``unknown_key``, ``bad_signature``, ``malformed`` or ``content_removed_uncovered``;
    ``None`` when ``ok``.
    """

    ok: bool
    checked: int
    head_seq: int | None
    head_digest: str | None
    first_bad_seq: int | None = None
    reason: str | None = None


def erased(rec: Mapping[str, Any]) -> str | None:
    """The tombstone id a content-less record names (unit LD-6), or None for a whole record."""
    mark = rec.get("erased")
    if "content" in rec or "evidence" in rec or not isinstance(mark, Mapping):
        return None
    tid = mark.get("tombstone")
    return tid if isinstance(tid, str) and tid else None


def tombstone_covers(rec: Mapping[str, Any]) -> dict[int, tuple[str, str]]:
    """``{seq: (attestation_id, digest)}`` a tombstone record names as erased; empty otherwise."""
    if rec.get("kind") != "tombstone":
        return {}
    body = ((rec.get("content") or {}).get("tombstone") or {}) if isinstance(rec.get("content"), Mapping) else {}
    out: dict[int, tuple[str, str]] = {}
    for e in body.get("erased") or ():
        try:
            out[int(e["seq"])] = (str(e["attestation_id"]), str(e["digest"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _signature_triple(
    rec: Mapping[str, Any], public_keys: Mapping[str, bytes]
) -> tuple[bytes, bytes, bytes] | None:
    """The ``(public key, message, signature)`` the walk would check for ``rec``'s node signature
    (over its stored digest, so a content-removed record gives the same triple), or ``None`` when
    the walk would stop on ``rec`` before its signature or the parts are not plain bytes. Never
    raises: anything odd is left to the walk, which reports it as it always has."""
    try:
        stored = rec.get("digest")
        sig = rec.get("node_sig") or {}
        if not isinstance(stored, str) or not isinstance(sig, Mapping):
            return None
        if sig.get("alg") != "ed25519":
            return None
        public = public_keys.get(sig.get("key_id", ""))
        if type(public) is not bytes:
            return None
        raw = base64.b64decode(sig.get("sig", ""), validate=True)
        return (public, signing_message(stored), raw)
    except Exception:  # noqa: BLE001 - the walk meets the same record and reports it
        return None


def verify_chain(
    records: Iterable[Mapping[str, Any]],
    public_keys: Mapping[str, bytes],
    *,
    start_seq: int = 1,
    start_prev: str = GENESIS,
    covered: Mapping[str, Mapping[int, tuple[str, str]]] | None = None,
) -> VerifyReport:
    """Verify records in order, starting at ``start_seq`` after ``start_prev``
    (an anchor), and stop at the first broken record.

    A record whose content was removed under its retention rule (unit LD-6, ADR-251 D7) keeps
    its digest, its link and its signatures but cannot be digested again. It is accepted only
    when a tombstone covers it: one verified later in this walk, or one the caller verified and
    passes in ``covered`` (``{tombstone id: {seq: (attestation_id, digest)}}``, for a slice that
    stops before the tombstone). Its stored digest must be the one the tombstone names, and its
    node signature over that digest must hold; a content-less record nothing covers fails
    ``content_removed_uncovered``.

    For a long run (a list or tuple of ``_sigcheck.PARALLEL_MIN`` records or more) the node
    signatures are also checked ahead in worker processes (#2200); the walk takes a worker's
    answer for a record only when it was computed over exactly the key, message and signature
    bytes the walk would verify itself, and verifies inline whatever the workers could not
    answer. Every check above is still made, in the same order."""
    ahead: _sigcheck.Pending | None = None
    triples: list[tuple[bytes, bytes, bytes] | None] = []
    # Only a list or tuple is checked ahead: any other iterable is read lazily, as it always was.
    if isinstance(records, (list, tuple)) and len(records) >= _sigcheck.PARALLEL_MIN:
        triples = [_signature_triple(rec, public_keys) for rec in records]
        ahead = _sigcheck.submit(triples)
    loaded_keys: dict[bytes, object] = {}
    expected_seq = start_seq
    prev = start_prev
    checked = 0
    head_seq: int | None = None
    head_digest: str | None = None
    pending: dict[int, tuple[str, str, str]] = {}  # seq -> (tombstone id, attestation id, digest)

    def fail(seq: Any, reason: str) -> VerifyReport:
        return VerifyReport(False, checked, head_seq, head_digest, seq, reason)

    def settle(tid: str, names: Mapping[int, tuple[str, str]]) -> int | None:
        """Resolve pending records ``tid`` names; the first that it names wrongly, else None."""
        for seq in [s for s, p in pending.items() if p[0] == tid]:
            aid, dig = pending[seq][1], pending[seq][2]
            if names.get(seq) != (aid, dig):
                return seq
            del pending[seq]
        return None

    try:
        for i, rec in enumerate(records):
            seq = rec.get("seq")
            if seq != expected_seq:
                return fail(seq, "seq_gap")
            if rec.get("prev_digest") != prev:
                return fail(seq, "prev_mismatch")
            stored = rec.get("digest")
            tid = erased(rec)
            if not isinstance(stored, str):
                return fail(seq, "digest_mismatch")
            if tid is not None:
                pending[int(seq)] = (tid, str(rec.get("attestation_id", "")), stored)
            elif digest(dict(rec)) != stored:
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
                message: bytes | None = signing_message(stored)
            except ValueError:
                message = None  # a stored digest that is not hex: a failed verification
            good: bool | None = None
            if message is None:
                good = False
            elif ahead is not None and triples[i] == (public, message, raw):
                good = ahead.answer(i)
            if good is None:
                # A malformed public key or a signature of the wrong length is a
                # failed verification, not an error that aborts the walk.
                good = _sigcheck.verify_one(public, message, raw, loaded_keys)  # type: ignore[arg-type]
            if not good:
                return fail(seq, "bad_signature")
            checked += 1
            head_seq, head_digest = seq, stored
            prev = stored
            expected_seq += 1
            names = tombstone_covers(rec)
            if names and pending:
                bad = settle(str(rec.get("attestation_id", "")), names)
                if bad is not None:
                    return fail(bad, "content_removed_uncovered")

        for tid, names in (covered or {}).items():
            bad = settle(tid, names)
            if bad is not None:
                return fail(bad, "content_removed_uncovered")
        if pending:
            return fail(min(pending), "content_removed_uncovered")
    finally:
        if ahead is not None:
            ahead.close()

    return VerifyReport(True, checked, head_seq, head_digest)


__all__ = [
    "GENESIS",
    "SIGNING_DOMAIN",
    "Ed25519Signer",
    "Signer",
    "VerifyReport",
    "erased",
    "seal",
    "signing_message",
    "tombstone_covers",
    "verify_chain",
]
