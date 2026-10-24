#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Verify a chain of attestation records with nothing but Python.

This file ships inside every evidence package. It imports only the standard
library, needs no network and no secret, and re-implements the canonical form
and Ed25519 verification itself, so whoever receives the package can check it
without trusting the software that produced it (ADR-143).

Usage:
    python verify.py records.jsonl --keys keys.json [--start-seq N --start-prev HEX]

``records.jsonl`` holds one record per line, in order. ``keys.json`` maps each
``key_id`` to its base64 public key. Exit status is 0 when every record
verifies and 1 at the first record that does not.

The Ed25519 code is the verification half of the reference implementation in
RFC 8032, section 6. It is slow and not constant-time, which is fine for
verifying public data.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal

GENESIS = "0" * 64
SIGNING_DOMAIN = b"axiom/attest/v1\n"
SIGNATURE_FIELDS = ("digest", "node_sig", "personal_sig")

# --------------------------------------------------------------------------
# Canonical form (must match axiom.attest.canonical exactly)
# --------------------------------------------------------------------------


class CanonicalError(ValueError):
    pass


def _nfc(s):
    return unicodedata.normalize("NFC", s)


def _normalise(value, path):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CanonicalError(f"{path}: float is not allowed in signed content")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalError(f"{path}: non-finite Decimal")
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CanonicalError(f"{path}: datetime has no timezone")
        utc = value.astimezone(UTC)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond:06d}Z"
    if isinstance(value, str):
        return _nfc(value)
    if isinstance(value, (list, tuple)):
        return [_normalise(v, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalError(f"{path}: non-string key")
            nk = _nfc(k)
            if nk in out:
                raise CanonicalError(f"{path}: duplicate key {nk!r}")
            out[nk] = _normalise(v, nk)
        return out
    raise CanonicalError(f"{path}: unsupported type {type(value).__name__}")


def canonical_bytes(record):
    return json.dumps(
        _normalise(record, ""),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(record):
    body = {k: v for k, v in record.items() if k not in SIGNATURE_FIELDS}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def decode_tagged(obj):
    """Records in a JSON file carry typed values already rendered to strings,
    so this is only needed for test vectors that tag them."""
    if isinstance(obj, dict):
        if set(obj) == {"$decimal"}:
            return Decimal(obj["$decimal"])
        if set(obj) == {"$datetime"}:
            return datetime.fromisoformat(obj["$datetime"])
        return {k: decode_tagged(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decode_tagged(v) for v in obj]
    return obj


# --------------------------------------------------------------------------
# Ed25519 verification (RFC 8032, section 6)
# --------------------------------------------------------------------------

_p = 2**255 - 19
_q = 2**252 + 27742317777372353535851937790883648493


def _inv(x):
    return pow(x, _p - 2, _p)


_d = -121665 * _inv(121666) % _p
_sqrt_m1 = pow(2, (_p - 1) // 4, _p)


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C = 2 * P[3] * Q[3] * _d % _p
    D = 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    if (P[0] * Q[2] - Q[0] * P[2]) % _p != 0:
        return False
    return (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _recover_x(y, sign):
    if y >= _p:
        return None
    x2 = (y * y - 1) * _inv(_d * y * y + 1) % _p
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _sqrt_m1 % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * _inv(5) % _p
_gx = _recover_x(_gy, 0)
_G = (_gx, _gy, 1, _gx * _gy % _p)


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _p)


def ed25519_verify(public, message, signature):
    """True when ``signature`` is a valid Ed25519 signature of ``message``."""
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _decompress(public)
    if A is None:
        return False
    Rs = signature[:32]
    R = _decompress(Rs)
    if R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _q:
        return False
    h = int.from_bytes(hashlib.sha512(Rs + public + message).digest(), "little") % _q
    return _equal(_mul(s, _G), _add(R, _mul(h, A)))


# --------------------------------------------------------------------------
# Chain walk
# --------------------------------------------------------------------------


def verify_records(records, keys, start_seq=1, start_prev=GENESIS):
    """Return ``(ok, checked, last_seq, last_digest, bad_seq, reason)``."""
    expected, prev, checked, last_seq, last_digest = start_seq, start_prev, 0, None, None
    for rec in records:
        seq = rec.get("seq")
        if seq != expected:
            return False, checked, last_seq, last_digest, seq, "seq_gap"
        if rec.get("prev_digest") != prev:
            return False, checked, last_seq, last_digest, seq, "prev_mismatch"
        stored = rec.get("digest")
        if not isinstance(stored, str) or digest(rec) != stored:
            return False, checked, last_seq, last_digest, seq, "digest_mismatch"
        sig = rec.get("node_sig") or {}
        if sig.get("alg") != "ed25519":
            return False, checked, last_seq, last_digest, seq, "malformed"
        public = keys.get(sig.get("key_id", ""))
        if public is None:
            return False, checked, last_seq, last_digest, seq, "unknown_key"
        try:
            raw = base64.b64decode(sig.get("sig", ""), validate=True)
        except (ValueError, TypeError):
            return False, checked, last_seq, last_digest, seq, "malformed"
        if not ed25519_verify(public, SIGNING_DOMAIN + bytes.fromhex(stored), raw):
            return False, checked, last_seq, last_digest, seq, "bad_signature"
        checked, last_seq, last_digest, prev = checked + 1, seq, stored, stored
        expected += 1
    return True, checked, last_seq, last_digest, None, None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verify a chain of attestation records.")
    ap.add_argument("records", help="records.jsonl, one record per line, in order")
    ap.add_argument("--keys", required=True, help="keys.json mapping key_id to base64 public key")
    ap.add_argument("--start-seq", type=int, default=1)
    ap.add_argument("--start-prev", default=GENESIS)
    args = ap.parse_args(argv)

    with open(args.keys, encoding="utf-8") as fh:
        keys = {k: base64.b64decode(v) for k, v in json.load(fh).items()}
    with open(args.records, encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]

    ok, checked, last_seq, last_digest, bad_seq, reason = verify_records(
        records, keys, args.start_seq, args.start_prev
    )
    if ok:
        print(f"OK: {checked} records verified; head seq {last_seq} digest {last_digest}")
        return 0
    print(f"BROKEN at seq {bad_seq}: {reason} ({checked} records verified before it)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
