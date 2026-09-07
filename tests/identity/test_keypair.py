# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Ed25519 keypair generation, serialization, sign, verify.

Foundation for ADR-020 identity layers and ADR-021 signed findings.
"""

from __future__ import annotations


def test_generate_keypair_yields_distinct_keys() -> None:
    from axiom.vega.identity import generate_keypair

    k1 = generate_keypair()
    k2 = generate_keypair()
    assert k1.public_bytes != k2.public_bytes


def test_sign_and_verify_roundtrip() -> None:
    from axiom.vega.identity import generate_keypair, verify

    kp = generate_keypair()
    sig = kp.sign(b"hello world")
    assert verify(kp.public_bytes, b"hello world", sig) is True


def test_verify_rejects_tampered_message() -> None:
    from axiom.vega.identity import generate_keypair, verify

    kp = generate_keypair()
    sig = kp.sign(b"original")
    assert verify(kp.public_bytes, b"tampered", sig) is False


def test_verify_rejects_wrong_key() -> None:
    from axiom.vega.identity import generate_keypair, verify

    kp1 = generate_keypair()
    kp2 = generate_keypair()
    sig = kp1.sign(b"msg")
    assert verify(kp2.public_bytes, b"msg", sig) is False


def test_keypair_serialization_roundtrip() -> None:
    from axiom.vega.identity import Keypair, generate_keypair

    kp = generate_keypair()
    serialized = kp.export_private()
    restored = Keypair.from_private_bytes(serialized)

    sig = restored.sign(b"x")
    from axiom.vega.identity import verify

    assert verify(kp.public_bytes, b"x", sig) is True


def test_handle_is_matrix_style() -> None:
    from axiom.vega.identity import Principal

    p = Principal(handle="@ben.booth:axiom", public_bytes=b"\x00" * 32)
    assert p.name == "ben.booth"
    assert p.context == "axiom"


def test_principal_handle_without_context() -> None:
    from axiom.vega.identity import Principal

    p = Principal(handle="@ben.booth", public_bytes=b"\x00" * 32)
    assert p.name == "ben.booth"
    assert p.context is None


def test_principal_rejects_double_at_form() -> None:
    import pytest

    from axiom.vega.identity import Principal

    with pytest.raises(ValueError, match="matrix-style"):
        Principal(handle="@ben.booth@axiom", public_bytes=b"\x00" * 32)


def test_principal_requires_leading_at() -> None:
    import pytest

    from axiom.vega.identity import Principal

    with pytest.raises(ValueError, match="must start with"):
        Principal(handle="ben.booth:axiom", public_bytes=b"\x00" * 32)
