# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""/export --seal signs the written bytes with the node signer, or says why it is unsigned."""

from __future__ import annotations

import hashlib
import json

import pytest

from axiom.attest.chain import Ed25519Signer, verify_chain
from axiom.extensions.builtins.attest import signing
from axiom.extensions.builtins.chat.tests._transcript_support import make_agent, run
from axiom.vega.identity.keypair import generate_keypair
from axiom.vega.identity.node_key import NodeKeyUnavailable

TURNS = [("q", "a")]


@pytest.fixture(autouse=True)
def _reset():
    yield
    signing.reset_provider()


def test_seal_writes_a_sidecar_that_verifies_and_binds_the_exact_bytes(tmp_path, monkeypatch):
    signer = Ed25519Signer("node:test", generate_keypair())
    signing.set_provider(lambda: signer)
    dest = tmp_path / "t.md"
    out = run(make_agent(monkeypatch, TURNS), f"/export {dest} --seal")
    seal = json.loads((tmp_path / "t.md.seal.json").read_text())
    assert seal["file_sha256"] == hashlib.sha256(dest.read_bytes()).hexdigest()
    assert seal["node_sig"]["key_id"] == "node:test"
    assert verify_chain([seal], signing.public_keys()).ok
    assert "sealed" in out.lower()


def test_tampering_with_the_export_breaks_the_binding(tmp_path, monkeypatch):
    signing.set_provider(lambda: Ed25519Signer("node:test", generate_keypair()))
    dest = tmp_path / "t.md"
    run(make_agent(monkeypatch, TURNS), f"/export {dest} --seal")
    seal = json.loads((tmp_path / "t.md.seal.json").read_text())
    dest.write_text(dest.read_text() + "edited")
    assert seal["file_sha256"] != hashlib.sha256(dest.read_bytes()).hexdigest()


def test_without_an_identity_the_export_is_written_unsigned_and_says_why(tmp_path, monkeypatch):
    def _none():
        raise NodeKeyUnavailable("no node identity; run node setup before signing")

    signing.set_provider(_none, keys=lambda: {})
    dest = tmp_path / "t.md"
    out = run(make_agent(monkeypatch, TURNS), f"/export {dest} --seal")
    assert dest.exists() and not (tmp_path / "t.md.seal.json").exists()
    assert "unsigned" in out.lower() and "no node identity" in out
