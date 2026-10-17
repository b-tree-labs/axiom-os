# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The canonical form of an attestation record (ADR-143).

What is signed is ``sha256(canonical_bytes(record without signature fields))``.
An outside verifier must reproduce those bytes exactly, on another machine and
possibly in another language, so every rule below is deliberately narrow:

- JSON, UTF-8, no insignificant whitespace, keys sorted by code point at every
  level, non-ASCII written as itself rather than escaped.
- Strings, including keys, are NFC-normalised. Two keys that normalise to the
  same string are refused rather than silently merged.
- Timestamps must be timezone-aware and are written in UTC with exactly six
  fractional digits: ``2026-09-30T14:30:00.000000Z``.
- Decimals are written as strings, exactly as entered (``Decimal("31.20")`` is
  ``"31.20"``). Floats are refused: their text differs between languages and
  libraries, and a measured value's digits are part of what was signed.
- Anything else (sets, bytes, arbitrary objects) is refused rather than
  stringified, because a stringified object has no stable text.

``tools/verify.py`` re-implements these rules with the standard library alone;
both are checked against ``tests/attest/vectors/canonical.json``.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

#: Fields that hold the result of signing and so are not part of what is signed.
SIGNATURE_FIELDS: tuple[str, ...] = ("digest", "node_sig", "personal_sig")


class CanonicalError(ValueError):
    """A value cannot be put into canonical form."""


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _normalise(value: Any, path: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CanonicalError(
            f"{path}: float is not allowed in signed content; use Decimal or a decimal string"
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalError(f"{path}: non-finite Decimal {value!r} cannot be signed")
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CanonicalError(f"{path}: datetime has no timezone; signed times must be aware")
        utc = value.astimezone(UTC)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond:06d}Z"
    if isinstance(value, str):
        return _nfc(value)
    if isinstance(value, (list, tuple)):
        return [_normalise(v, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalError(f"{path}: key {k!r} is not a string")
            nk = _nfc(k)
            if nk in out:
                raise CanonicalError(f"{path}: duplicate key {nk!r} after NFC normalisation")
            out[nk] = _normalise(v, f"{path}.{nk}" if path else nk)
        return out
    raise CanonicalError(f"{path}: unsupported type {type(value).__name__}")


def normalise(record: dict[str, Any]) -> dict[str, Any]:
    """``record`` with every value in its canonical JSON-native form: Decimals
    and datetimes as their canonical strings, strings NFC. A normalised record
    survives a JSON (or JSONB) round trip with its digest unchanged, which is
    why a record is normalised before it is sealed and stored."""
    return _normalise(record, "")


def canonical_bytes(record: dict[str, Any]) -> bytes:
    """The exact bytes that are hashed for ``record`` (signature fields included
    if present; use :func:`digest` to hash a record for signing)."""
    normalised = _normalise(record, "")
    return json.dumps(
        normalised, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(record: dict[str, Any]) -> str:
    """SHA-256 hex digest of ``record`` without its signature fields."""
    body = {k: v for k, v in record.items() if k not in SIGNATURE_FIELDS}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


__all__ = ["SIGNATURE_FIELDS", "CanonicalError", "canonical_bytes", "digest", "normalise"]
