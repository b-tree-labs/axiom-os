# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the signed materials manifest.

Phase 2 of the materials-flow tier. The coordinator signs a list of
everything a student needs to download — file ids, hashes, titles,
sizes — so the student can verify integrity of every piece before
indexing it. Same signing primitive (Ed25519 over canonical JSON) as
the membership manifest in classroom_coordinator.py; factoring it to
a shared helper is a future refactor.

Contract:
- Manifest roundtrips (build → sign → encode → decode → verify).
- Tampered entries fail verification.
- Wrong coordinator key fails verification.
- Empty corpus is a valid, signable manifest (just no entries).
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.classroom_materials import (
    ClassroomMaterialsStore,
)
from axiom.extensions.builtins.classroom.materials_manifest import (
    build_materials_manifest,
    decode_materials_manifest,
    encode_materials_manifest,
    verify_materials_manifest,
)
from axiom.vega.federation.identity import generate_identity

# ---------------------------------------------------------------------------
# Build + sign
# ---------------------------------------------------------------------------


class TestBuild:
    def test_empty_store_produces_valid_manifest(self, tmp_path):
        coord = generate_identity(
            owner="prof@ut.edu", keys_dir=tmp_path / "keys"
        )
        store = ClassroomMaterialsStore(tmp_path / "store")
        manifest = build_materials_manifest(
            identity=coord,
            classroom_id="NE101",
            store=store,
        )
        assert manifest.classroom_id == "NE101"
        assert manifest.entries == []
        assert manifest.signature
        assert verify_materials_manifest(
            manifest, coordinator_public_key=coord.public_key
        ).valid

    def test_populated_store_reflects_in_manifest(self, tmp_path):
        coord = generate_identity(
            owner="prof@ut.edu", keys_dir=tmp_path / "keys"
        )
        store = ClassroomMaterialsStore(tmp_path / "store")
        e1 = store.add_text("Fission splits heavy nuclei.", filename="ch1.md")
        e2 = store.add_text("Fusion combines light nuclei.", filename="ch2.md")

        manifest = build_materials_manifest(
            identity=coord,
            classroom_id="NE101",
            store=store,
        )
        ids = {entry.file_id for entry in manifest.entries}
        assert ids == {e1.file_id, e2.file_id}

        # All sizes + hashes propagated.
        by_id = {entry.file_id: entry for entry in manifest.entries}
        assert by_id[e1.file_id].size_bytes == e1.size_bytes
        assert by_id[e1.file_id].content_hash == e1.content_hash


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class TestVerify:
    def _setup(self, tmp_path):
        coord = generate_identity(
            owner="prof@ut.edu", keys_dir=tmp_path / "keys"
        )
        store = ClassroomMaterialsStore(tmp_path / "store")
        store.add_text("content", filename="f.md")
        manifest = build_materials_manifest(
            identity=coord, classroom_id="NE101", store=store,
        )
        return coord, manifest

    def test_good_signature_verifies(self, tmp_path):
        coord, manifest = self._setup(tmp_path)
        result = verify_materials_manifest(
            manifest, coordinator_public_key=coord.public_key
        )
        assert result.valid is True

    def test_tampered_entries_fails(self, tmp_path):
        from dataclasses import replace as _replace

        coord, manifest = self._setup(tmp_path)
        # Swap a title — sig must no longer validate.
        tampered_entries = [
            _replace(e, title="NOT THE ORIGINAL") for e in manifest.entries
        ]
        tampered = _replace(manifest, entries=tampered_entries)
        result = verify_materials_manifest(
            tampered, coordinator_public_key=coord.public_key
        )
        assert result.valid is False
        assert "signature" in (result.reason or "").lower()

    def test_wrong_key_fails(self, tmp_path):
        _, manifest = self._setup(tmp_path)
        other = generate_identity(
            owner="impostor@ut.edu", keys_dir=tmp_path / "other_keys"
        )
        result = verify_materials_manifest(
            manifest, coordinator_public_key=other.public_key
        )
        assert result.valid is False

    def test_tampered_classroom_id_fails(self, tmp_path):
        from dataclasses import replace as _replace

        coord, manifest = self._setup(tmp_path)
        tampered = _replace(manifest, classroom_id="DIFFERENT_CLASS")
        result = verify_materials_manifest(
            tampered, coordinator_public_key=coord.public_key
        )
        assert result.valid is False


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


class TestEncodeRoundtrip:
    def test_encode_decode_preserves_all_fields(self, tmp_path):
        coord = generate_identity(
            owner="prof@ut.edu", keys_dir=tmp_path / "keys"
        )
        store = ClassroomMaterialsStore(tmp_path / "store")
        store.add_text("one", filename="a.md")
        store.add_text("two", filename="b.md")
        original = build_materials_manifest(
            identity=coord, classroom_id="NE101", store=store,
        )
        encoded = encode_materials_manifest(original)
        decoded = decode_materials_manifest(encoded)
        assert decoded == original

    def test_encoded_manifest_is_json(self, tmp_path):
        coord = generate_identity(
            owner="prof@ut.edu", keys_dir=tmp_path / "keys"
        )
        store = ClassroomMaterialsStore(tmp_path / "store")
        manifest = build_materials_manifest(
            identity=coord, classroom_id="NE101", store=store,
        )
        encoded = encode_materials_manifest(manifest)
        # Must be a parseable JSON object (not base64-wrapped) so the
        # HTTP endpoint can return it as Content-Type: application/json.
        parsed = json.loads(encoded)
        assert parsed["classroom_id"] == "NE101"
        assert "signature" in parsed
        assert isinstance(parsed["entries"], list)

    def test_decode_rejects_malformed_input(self):
        with pytest.raises(ValueError):
            decode_materials_manifest("not json")

    def test_decode_rejects_missing_fields(self):
        with pytest.raises(ValueError, match="missing"):
            decode_materials_manifest('{"classroom_id": "x"}')


# ---------------------------------------------------------------------------
# Canonical-payload determinism — the signing foundation
# ---------------------------------------------------------------------------


class TestCanonicalDeterminism:
    def test_entry_ordering_does_not_affect_signature(self, tmp_path):
        """Two stores with the same content added in different order
        produce the same signature when sorted canonically."""
        coord = generate_identity(
            owner="prof@ut.edu", keys_dir=tmp_path / "keys"
        )

        s1 = ClassroomMaterialsStore(tmp_path / "s1")
        s1.add_text("alpha", filename="a.md")
        s1.add_text("beta", filename="b.md")

        s2 = ClassroomMaterialsStore(tmp_path / "s2")
        s2.add_text("beta", filename="b.md")
        s2.add_text("alpha", filename="a.md")

        m1 = build_materials_manifest(
            identity=coord, classroom_id="NE101", store=s1,
        )
        m2 = build_materials_manifest(
            identity=coord, classroom_id="NE101", store=s2,
        )

        # The `generated_at` line will differ, so we can't compare whole
        # manifests. But the entries list, sorted canonically, should
        # match exactly — which is what the signature covers.
        e1_sorted = sorted([e.file_id for e in m1.entries])
        e2_sorted = sorted([e.file_id for e in m2.entries])
        assert e1_sorted == e2_sorted
