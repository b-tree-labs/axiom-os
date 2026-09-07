# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for student-side membership persistence.

Tier A PR 4 — student node receives the coordinator's signed
membership manifest, verifies it, and persists it locally so later
subsystems (RAG, agent, instructor brief) can ask "am I a member of
classroom X?" without re-doing the ceremony.

Persistence layout:
    <base_dir>/classrooms/<classroom_id>/membership.json

File contains the encoded manifest plus a ``coordinator_public_key``
side-channel so verification can be re-run offline without needing
the federation identity lookup.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.classroom_coordinator import (
    sign_membership_manifest,
)
from axiom.extensions.builtins.classroom.classroom_federation import (
    add_member,
    create_cohort,
)
from axiom.extensions.builtins.classroom.student_membership import (
    MembershipNotFoundError,
    MembershipStore,
    StoredMembership,
    is_member_of,
    list_memberships,
)
from axiom.vega.federation.identity import generate_identity

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def coordinator_identity(tmp_path):
    return generate_identity(
        owner="ondrej@ctu.cz",
        display_name="Test Peer",
        keys_dir=tmp_path / "coord-keys",
    )


@pytest.fixture
def other_identity(tmp_path):
    return generate_identity(
        owner="mallory@nowhere",
        display_name="Not the real coordinator",
        keys_dir=tmp_path / "other-keys",
    )


@pytest.fixture
def signed_manifest(coordinator_identity):
    cohort = create_cohort("ne101-prague-2026", coordinator_identity.node_id)
    cohort = add_member(cohort, "alice", "alice_node_abc", "tok")
    return sign_membership_manifest(
        identity=coordinator_identity, cohort=cohort, student_id="alice"
    )


@pytest.fixture
def store(tmp_path):
    return MembershipStore(base_dir=tmp_path / "student-state")


# ---------------------------------------------------------------------------
# Storage + retrieval
# ---------------------------------------------------------------------------


class TestStorage:
    def test_save_then_load_roundtrip(self, store, signed_manifest, coordinator_identity):
        store.save(
            manifest=signed_manifest,
            coordinator_public_key=coordinator_identity.public_key,
        )
        loaded = store.load(signed_manifest.classroom_id)
        assert loaded.manifest == signed_manifest
        assert loaded.coordinator_public_key == coordinator_identity.public_key

    def test_save_writes_expected_path(self, store, signed_manifest, coordinator_identity):
        store.save(signed_manifest, coordinator_identity.public_key)
        path = store.base_dir / "classrooms" / signed_manifest.classroom_id / "membership.json"
        assert path.is_file()
        # Content is JSON and parses.
        data = json.loads(path.read_text())
        assert "manifest" in data
        assert "coordinator_public_key" in data

    def test_load_unknown_classroom_raises(self, store):
        with pytest.raises(MembershipNotFoundError):
            store.load("no-such-classroom")

    def test_save_overwrites_prior_membership(self, store, signed_manifest, coordinator_identity):
        """Re-joining should replace the stored manifest, not duplicate."""
        store.save(signed_manifest, coordinator_identity.public_key)
        # Second save, same classroom — no exception, replaces in place.
        store.save(signed_manifest, coordinator_identity.public_key)
        loaded = store.load(signed_manifest.classroom_id)
        assert loaded.manifest == signed_manifest


# ---------------------------------------------------------------------------
# Verification on load
# ---------------------------------------------------------------------------


class TestVerificationOnLoad:
    def test_load_verifies_by_default(self, store, signed_manifest, coordinator_identity):
        """load() MUST re-verify the signature — files could be tampered on disk."""
        store.save(signed_manifest, coordinator_identity.public_key)
        loaded = store.load(signed_manifest.classroom_id)
        # Sanity — loaded should pass verification.
        assert loaded.verify().valid is True

    def test_tampered_file_rejects_verification(
        self, store, signed_manifest, coordinator_identity
    ):
        store.save(signed_manifest, coordinator_identity.public_key)
        path = store.base_dir / "classrooms" / signed_manifest.classroom_id / "membership.json"
        # Tamper the on-disk file: change student_id inside the manifest.
        data = json.loads(path.read_text())
        # The manifest is encoded base64url(json); we'll tamper AFTER load
        # by directly editing the record shape to swap the student_id.
        # Simpler: use the known wire format — flip a bit in the signature.
        encoded = data["manifest"]
        corrupted = encoded[:-8] + ("X" * 8)
        data["manifest"] = corrupted
        path.write_text(json.dumps(data))

        # load() should refuse or mark as unverified. We raise to force
        # callers to handle this explicitly rather than trusting stale state.
        with pytest.raises(MembershipNotFoundError):
            store.load(signed_manifest.classroom_id)

    def test_wrong_stored_coordinator_key_rejected(
        self, store, signed_manifest, coordinator_identity, other_identity
    ):
        # Save the manifest but with the WRONG coordinator_public_key next to it.
        # That simulates a student fed a forged coordinator identity on enrollment.
        store.save(signed_manifest, other_identity.public_key)
        with pytest.raises(MembershipNotFoundError):
            store.load(signed_manifest.classroom_id)


# ---------------------------------------------------------------------------
# Convenience queries
# ---------------------------------------------------------------------------


class TestQueries:
    def test_is_member_of_true_after_save(self, store, signed_manifest, coordinator_identity):
        store.save(signed_manifest, coordinator_identity.public_key)
        assert is_member_of(store, signed_manifest.classroom_id) is True

    def test_is_member_of_false_for_unknown(self, store):
        assert is_member_of(store, "never-heard-of-it") is False

    def test_list_memberships_returns_all(self, store, coordinator_identity):
        # Save two classrooms' manifests under the same student store.
        for classroom_id, student_id in [("c1", "alice"), ("c2", "alice")]:
            cohort = create_cohort(classroom_id, coordinator_identity.node_id)
            cohort = add_member(cohort, student_id, f"{student_id}_node", "t")
            manifest = sign_membership_manifest(
                identity=coordinator_identity, cohort=cohort, student_id=student_id
            )
            store.save(manifest, coordinator_identity.public_key)

        ids = sorted(list_memberships(store))
        assert ids == ["c1", "c2"]

    def test_list_memberships_empty_when_nothing_saved(self, store):
        assert list_memberships(store) == []


# ---------------------------------------------------------------------------
# StoredMembership as a dataclass
# ---------------------------------------------------------------------------


class TestStoredMembership:
    def test_stored_membership_exposes_identity_fields(
        self, store, signed_manifest, coordinator_identity
    ):
        store.save(signed_manifest, coordinator_identity.public_key)
        loaded: StoredMembership = store.load(signed_manifest.classroom_id)
        assert loaded.classroom_id == signed_manifest.classroom_id
        assert loaded.student_id == signed_manifest.student_id
        assert loaded.coordinator_node == signed_manifest.coordinator_node


# ---------------------------------------------------------------------------
# Delete — student-side leave a classroom
# ---------------------------------------------------------------------------


class TestDeleteMembership:
    """The `delete` method is the primitive `axi classroom leave` builds
    on. Idempotent + scoped to one classroom — never wipes the student's
    other memberships."""

    def test_delete_removes_existing_manifest(
        self, store, signed_manifest, coordinator_identity
    ):
        store.save(signed_manifest, coordinator_identity.public_key)
        assert is_member_of(store, signed_manifest.classroom_id)

        removed = store.delete(signed_manifest.classroom_id)
        assert removed is True
        assert is_member_of(store, signed_manifest.classroom_id) is False

    def test_delete_unknown_classroom_returns_false(self, store):
        assert store.delete("never-heard-of-it") is False

    def test_delete_is_idempotent(
        self, store, signed_manifest, coordinator_identity
    ):
        store.save(signed_manifest, coordinator_identity.public_key)
        assert store.delete(signed_manifest.classroom_id) is True
        # Second call: nothing on disk, but no exception either.
        assert store.delete(signed_manifest.classroom_id) is False

    def test_delete_scopes_to_one_classroom(self, store, coordinator_identity):
        for classroom_id, student_id in [("c1", "alice"), ("c2", "alice")]:
            cohort = create_cohort(classroom_id, coordinator_identity.node_id)
            cohort = add_member(cohort, student_id, f"{student_id}_node", "t")
            manifest = sign_membership_manifest(
                identity=coordinator_identity, cohort=cohort, student_id=student_id,
            )
            store.save(manifest, coordinator_identity.public_key)

        assert sorted(list_memberships(store)) == ["c1", "c2"]
        store.delete("c1")
        assert list_memberships(store) == ["c2"]
