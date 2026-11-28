# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The v1 federation fixtures agree with the rules they claim to encode.

The AEOS federation conformance kit (spec-aeos-federation-conformance-kit,
ADR-276) ships golden vectors under ``fixtures/v1``. This module checks them
against an independent, minimal reading of spec-agent-accord-protocol §3:
RFC 8785 canonical JSON under the AEOS restrictions, and a detached JWS with
EdDSA over ``protected + "." + base64url(canonical bytes)``.

It proves the fixtures, not an implementation. An implementation is proven
when the kit's runners pass against it (AEOS §18.6, still draft).
"""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

V1 = Path(__file__).parent / "fixtures" / "v1"
AEOS_EXT = "urn:aeos:ext:federation:1"

#: Expected ``aeos`` object type in the protected header, per signed fixture.
SIGNED = {
    "card.json": "card",
    "message.json": "message",
    "delegation.json": "delegation",
    "workload-offer.json": "offer",
}


class Refused(ValueError):
    """A value the AEOS restrictions on RFC 8785 do not allow."""


def _load(rel: str):
    return json.loads((V1 / rel).read_text(encoding="utf-8"))


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _check(v, path: str = "$") -> None:
    if isinstance(v, bool) or v is None:
        return
    if isinstance(v, float):
        raise Refused(f"{path}: float")
    if isinstance(v, int):
        if abs(v) > 2**53 - 1:
            raise Refused(f"{path}: integer out of range")
        return
    if isinstance(v, str):
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in v):
            raise Refused(f"{path}: lone surrogate")
        return
    if isinstance(v, list):
        for i, x in enumerate(v):
            _check(x, f"{path}[{i}]")
        return
    if isinstance(v, dict):
        for k, x in v.items():
            if not k.isascii():
                raise Refused(f"{path}: non-ASCII member name {k!r}")
            _check(x, f"{path}.{k}")
        return
    raise Refused(f"{path}: type {type(v).__name__}")


def _nfc(v):
    if isinstance(v, str):
        return unicodedata.normalize("NFC", v)
    if isinstance(v, list):
        return [_nfc(x) for x in v]
    if isinstance(v, dict):
        return {k: _nfc(x) for k, x in v.items()}
    return v


def canonical(v) -> bytes:
    """RFC 8785 under the AEOS restrictions (integers only, ASCII member names).

    With those restrictions, sorted keys plus compact separators and no ASCII
    escaping is byte-identical to RFC 8785.
    """
    _check(v)
    return json.dumps(
        v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _split_signature(obj: dict) -> tuple[dict, list[dict]]:
    o = json.loads(json.dumps(obj))
    md = o.get("metadata", {}).get(AEOS_EXT)
    if md is not None and "signatures" in md:
        return o, md.pop("signatures")
    return o, o.pop("signatures")


def verify(obj: dict, expected_type: str, pinned: dict[str, str]) -> None:
    """Raise unless ``obj`` carries a valid AEOS signature of ``expected_type``.

    ``pinned`` maps node id to public key hex. A key offered inside the object
    is never used.
    """
    body, sigs = _split_signature(obj)
    if len(sigs) != 1:
        raise Refused("exactly one signature expected")
    sig = sigs[0]
    header = json.loads(_b64u_decode(sig["protected"]))
    if header.get("alg") != "EdDSA":
        raise Refused(f"alg {header.get('alg')!r}")
    if header.get("typ") != "aeos+jws":
        raise Refused(f"typ {header.get('typ')!r}")
    if header.get("aeos") != expected_type:
        raise Refused(f"type {header.get('aeos')!r}, expected {expected_type!r}")
    kid = header.get("kid")
    if kid not in pinned:
        raise Refused(f"kid {kid!r} is not pinned")
    raw = _b64u_decode(sig["signature"])
    if len(raw) != 64:
        raise Refused("not an Ed25519 signature")
    signing_input = f"{sig['protected']}.{_b64u(canonical(body))}".encode("ascii")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pinned[kid])).verify(raw, signing_input)
    except InvalidSignature as exc:
        raise Refused("signature does not verify") from exc


@pytest.fixture(scope="module")
def pinned() -> dict[str, str]:
    keys = _load("keys.json")
    by_name = {n["name"]: n for n in keys["nodes"]}
    out = {}
    for name, node_id in keys["pinned"].items():
        pub = bytes.fromhex(by_name[name]["public_key_hex"])
        assert hashlib.sha256(pub).hexdigest()[:16] == node_id, name
        assert by_name[name]["node_id"] == node_id, name
        out[node_id] = by_name[name]["public_key_hex"]
    return out


def _canonical_vectors():
    return _load("canonical.json")["vectors"]


@pytest.mark.parametrize("vec", _canonical_vectors(), ids=lambda v: v["id"])
def test_canonical_vector(vec):
    if vec.get("refused"):
        with pytest.raises(Refused):
            canonical(json.loads(vec["input_json_text"]))
        return
    value = _nfc(vec["input"]) if vec.get("signer_nfc") else vec["input"]
    got = canonical(value)
    assert got.hex() == vec["canonical_hex"]
    assert got.decode("utf-8") == vec["canonical_utf8"]


def test_canonical_vectors_cover_the_refusals():
    refused = {v["id"] for v in _canonical_vectors() if v.get("refused")}
    assert refused == {
        "jcs-neg-float",
        "jcs-neg-big-int",
        "jcs-neg-non-ascii-name",
        "jcs-neg-lone-surrogate",
    }


@pytest.mark.parametrize("name,kind", sorted(SIGNED.items()))
def test_signed_fixture_verifies(name, kind, pinned):
    verify(_load(f"signed/{name}"), kind, pinned)


def test_receipt_chain_links_and_verifies(pinned):
    receipts = _load("signed/receipt-chain.json")["receipts"]
    assert len(receipts) >= 2
    assert receipts[0]["prev"] is None
    for prior, nxt in zip(receipts, receipts[1:]):
        verify(prior, "receipt", pinned)
        verify(nxt, "receipt", pinned)
        body, _ = _split_signature(prior)
        assert nxt["prev"] == "sha256:" + hashlib.sha256(canonical(body)).hexdigest()
        assert nxt["seq"] == prior["seq"] + 1


def test_a_signed_fixture_with_one_changed_byte_fails(pinned):
    card = _load("signed/card.json")
    card["description"] += "."
    with pytest.raises(Refused):
        verify(card, "card", pinned)


def _negative_vectors():
    return _load("negative.json")["vectors"]


@pytest.mark.parametrize("vec", _negative_vectors(), ids=lambda v: v["id"])
def test_negative_vector(vec, pinned):
    if vec["expect"] == "refuse":
        with pytest.raises(Refused):
            verify(vec["object"], "message", pinned)
    else:
        assert vec["expect"] == "accept-as-data"
        # The signature is valid; the instruction-shaped text stays message content.
        verify(vec["object"], "message", pinned)
        assert all("text" in part for part in vec["object"]["parts"])


def test_negative_vectors_are_the_documented_set():
    assert {v["id"]: v["expect"] for v in _negative_vectors()} == {
        "neg-hash-as-sig": "refuse",
        "neg-wrong-type": "refuse",
        "neg-tampered": "refuse",
        "neg-kid-unpinned": "refuse",
        "neg-alg-none": "refuse",
        "neg-instruction-in-content": "accept-as-data",
    }


def test_unpinned_key_offered_in_the_vector_would_verify_but_is_refused(pinned):
    """The offered key is real, so refusal comes from pinning, not a bad signature."""
    vec = next(v for v in _negative_vectors() if v["id"] == "neg-kid-unpinned")
    offered = vec["offered_public_key_hex"]
    kid = hashlib.sha256(bytes.fromhex(offered)).hexdigest()[:16]
    assert kid not in pinned
    verify(vec["object"], "message", {**pinned, kid: offered})
    with pytest.raises(Refused, match="not pinned"):
        verify(vec["object"], "message", pinned)
