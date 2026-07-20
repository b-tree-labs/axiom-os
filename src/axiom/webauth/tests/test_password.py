# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for webauth password hashing/validation (stdlib scrypt)."""

from __future__ import annotations

from axiom.webauth import get_password_hash, validate_password, verify_password


def test_hash_roundtrip() -> None:
    h = get_password_hash("Sup3rSecret")
    assert h.startswith("scrypt$16384$8$1$")
    assert "Sup3rSecret" not in h
    assert verify_password("Sup3rSecret", h) is True
    assert verify_password("wrong", h) is False


def test_hash_is_salted() -> None:
    assert get_password_hash("same") != get_password_hash("same")


def test_verify_rejects_malformed_hash() -> None:
    assert verify_password("x", "not-a-hash") is False
    assert verify_password("x", "bcrypt$a$b$c$d$e") is False  # wrong scheme


def test_validate_rules() -> None:
    assert validate_password("Sup3rSecret")[0] is True
    assert validate_password("short")[0] is False
    assert validate_password("nouppercase1")[0] is False
    assert validate_password("password123")[0] is False  # common
    assert validate_password("anythinggoes", complexity="relaxed")[0] is True
