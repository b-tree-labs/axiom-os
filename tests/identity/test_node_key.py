# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The node identity key signs attestations through one custody operation
(ADR-143). Callers get ``sign`` and public material; they never get the key,
and a node without a usable key refuses to sign rather than inventing one."""

from __future__ import annotations

import base64

import pytest

from axiom.attest.chain import GENESIS, seal, verify_chain
from axiom.vega.federation.identity import generate_identity
from axiom.vega.identity.keypair import verify
from axiom.vega.identity.node_key import (
    NodeKeyUnavailable,
    node_signer,
)


@pytest.fixture
def identity(tmp_path):
    return generate_identity("owner@example.org", keys_dir=tmp_path)


def test_signs_with_the_key_the_node_publishes(identity, tmp_path):
    signer = node_signer(tmp_path)
    sig = signer.sign(b"message")
    assert verify(base64.b64decode(identity.public_key), b"message", sig)
    assert signer.public_bytes == base64.b64decode(identity.public_key)


def test_key_id_names_the_node_key(identity, tmp_path):
    assert node_signer(tmp_path).key_id == f"node:{identity.node_id}"


def test_public_keys_map_verifies_a_sealed_chain(identity, tmp_path):
    signer = node_signer(tmp_path)
    r1 = seal({"content": {"t": "a"}}, seq=1, prev_digest=GENESIS, signer=signer)
    r2 = seal({"content": {"t": "b"}}, seq=2, prev_digest=r1["digest"], signer=signer)
    report = verify_chain([r1, r2], signer.public_keys())
    assert report.ok and report.checked == 2


def test_the_private_key_is_not_reachable_from_the_signer(identity, tmp_path):
    signer = node_signer(tmp_path)
    public_attrs = {a for a in dir(signer) if not a.startswith("_")}
    assert public_attrs == {"key_id", "public_bytes", "public_keys", "sign"}
    assert "private" not in repr(signer).lower()


def test_no_identity_fails_closed(tmp_path):
    with pytest.raises(NodeKeyUnavailable, match="no node identity"):
        node_signer(tmp_path)
    assert not (tmp_path / "private.pem").exists(), "must never generate a key"


def test_missing_private_key_fails_closed(identity, tmp_path):
    (tmp_path / "private.pem").unlink()
    with pytest.raises(NodeKeyUnavailable, match="private key"):
        node_signer(tmp_path)


def test_key_that_does_not_match_the_published_identity_fails_closed(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    generate_identity("owner@example.org", keys_dir=first)
    generate_identity("owner@example.org", keys_dir=second)
    (first / "private.pem").write_bytes((second / "private.pem").read_bytes())
    with pytest.raises(NodeKeyUnavailable, match="does not match"):
        node_signer(first)


def test_unreadable_key_material_fails_closed(identity, tmp_path):
    (tmp_path / "private.pem").write_bytes(b"not a key")
    with pytest.raises(NodeKeyUnavailable, match="cannot be loaded"):
        node_signer(tmp_path)
