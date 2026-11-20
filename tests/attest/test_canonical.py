# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The canonical form is what gets signed, so every rule here is a promise to
an outside verifier: the same record must produce the same bytes on any
machine, in any language, years from now (ADR-143)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from axiom.attest.canonical import (
    SIGNATURE_FIELDS,
    CanonicalError,
    canonical_bytes,
    digest,
)

VECTORS = Path(__file__).parent / "vectors" / "canonical.json"


def test_keys_are_sorted_and_output_is_compact_utf8():
    out = canonical_bytes({"b": 1, "a": "é"})
    assert out == '{"a":"é","b":1}'.encode()


def test_nested_keys_are_sorted_at_every_level():
    out = canonical_bytes({"z": {"y": 1, "x": [{"b": 2, "a": 1}]}})
    assert out == b'{"z":{"x":[{"a":1,"b":2}],"y":1}}'


def test_floats_are_refused_because_their_text_is_not_portable():
    with pytest.raises(CanonicalError, match="float"):
        canonical_bytes({"power": 950.0})


def test_decimals_keep_the_digits_as_entered():
    assert canonical_bytes({"v": Decimal("31.20")}) == b'{"v":"31.20"}'


def test_non_finite_decimals_are_refused():
    with pytest.raises(CanonicalError):
        canonical_bytes({"v": Decimal("NaN")})


def test_datetimes_are_utc_with_six_fractional_digits():
    t = datetime(2026, 9, 30, 9, 30, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert canonical_bytes({"t": t}) == b'{"t":"2026-09-30T14:30:00.000000Z"}'


def test_naive_datetimes_are_refused():
    with pytest.raises(CanonicalError, match="timezone"):
        canonical_bytes({"t": datetime(2026, 9, 30, 9, 30)})


def test_strings_are_nfc_normalised():
    decomposed = "é"  # e + combining acute
    composed = "é"
    assert canonical_bytes({"s": decomposed}) == canonical_bytes({"s": composed})


def test_keys_that_collide_after_normalisation_are_refused():
    with pytest.raises(CanonicalError, match="duplicate"):
        canonical_bytes({"é": 1, "é": 2})


def test_non_string_keys_are_refused():
    with pytest.raises(CanonicalError):
        canonical_bytes({1: "x"})


def test_bools_and_none_are_json_literals():
    assert canonical_bytes({"a": True, "b": None, "c": False}) == b'{"a":true,"b":null,"c":false}'


def test_tuples_serialise_as_lists():
    assert canonical_bytes({"t": ("a", "b")}) == b'{"t":["a","b"]}'


def test_unsupported_types_are_refused_rather_than_stringified():
    with pytest.raises(CanonicalError):
        canonical_bytes({"x": object()})


def test_digest_ignores_signature_fields_and_nothing_else():
    base = {"seq": 1, "content": {"title": "Console check"}}
    signed = {**base, "digest": "ab" * 32, "node_sig": {"sig": "x"}, "personal_sig": None}
    assert digest(signed) == digest(base)
    assert set(SIGNATURE_FIELDS) == {"digest", "node_sig", "personal_sig"}
    assert digest({**base, "seq": 2}) != digest(base)


def test_shared_vectors_match():
    """Each vector's ``canonical`` is the exact signed text: the record without
    its signature fields. The same vectors are checked by the standalone
    verifier's tests, so the two implementations cannot drift apart without a
    test failing."""
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert vectors, "vector file must not be empty"
    for v in vectors:
        record = _decode(v["record"])
        signed = {k: x for k, x in record.items() if k not in SIGNATURE_FIELDS}
        assert canonical_bytes(signed).decode("utf-8") == v["canonical"], v["name"]
        assert digest(record) == v["digest"], v["name"]


def _decode(obj):
    """Vectors are JSON, so typed values are tagged: {"$decimal": "..."} and
    {"$datetime": "..."}."""
    if isinstance(obj, dict):
        if set(obj) == {"$decimal"}:
            return Decimal(obj["$decimal"])
        if set(obj) == {"$datetime"}:
            return datetime.fromisoformat(obj["$datetime"])
        return {k: _decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode(v) for v in obj]
    return obj


def test_normalise_survives_a_json_round_trip_with_the_same_digest():
    from axiom.attest.canonical import normalise

    record = {
        "t": datetime(2026, 9, 30, 9, 30, tzinfo=UTC),
        "v": Decimal("31.20"),
        "s": "é",
        "n": [1, None, True],
    }
    flat = normalise(record)
    assert flat == {
        "t": "2026-09-30T09:30:00.000000Z",
        "v": "31.20",
        "s": "é",
        "n": [1, None, True],
    }
    assert digest(json.loads(json.dumps(flat))) == digest(record)
