# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Student-side persistence of classroom membership manifests.

Tier A PR 4. After the coordinator returns a signed membership manifest
(via the ceremony in PR 3), the student node persists it locally so
later subsystems — RAG policy, the chat agent, the instructor brief
— can ask "am I a member of classroom X?" without re-running the
ceremony.

Layout on disk::

    <base_dir>/classrooms/<classroom_id>/membership.json

The file stores the encoded manifest + the coordinator's public key
so verification can be re-run offline every time the record is
loaded. Tampering with either the file or the claimed public key
causes `load` to raise :class:`MembershipNotFoundError`, preventing
the rest of the stack from trusting a forged or corrupted record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .classroom_coordinator import (
    MembershipManifest,
    decode_membership_manifest,
    encode_membership_manifest,
    verify_membership_manifest,
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MembershipNotFoundError(KeyError):
    """Raised when a classroom membership is absent OR fails re-verification on load.

    A tampered on-disk file is treated as "not found" rather than a silent
    read — callers must see this as a missing-membership signal, not a
    recoverable warning.
    """


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredMembership:
    """Exactly what lives at rest for one classroom."""

    manifest: MembershipManifest
    coordinator_public_key: str

    # ----- Convenience accessors (pass-through) -----
    @property
    def classroom_id(self) -> str:
        return self.manifest.classroom_id

    @property
    def student_id(self) -> str:
        return self.manifest.student_id

    @property
    def coordinator_node(self) -> str:
        return self.manifest.coordinator_node

    @property
    def status(self) -> str:
        return self.manifest.status

    def verify(self):
        return verify_membership_manifest(
            self.manifest, coordinator_public_key=self.coordinator_public_key
        )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class MembershipStore:
    """Local filesystem store of classroom memberships.

    ``base_dir`` is typically ``~/.axi``. Tests inject a tmp_path.
    """

    base_dir: Path

    # ---- Path helpers ----
    def _classroom_dir(self, classroom_id: str) -> Path:
        return self.base_dir / "classrooms" / classroom_id

    def _membership_path(self, classroom_id: str) -> Path:
        return self._classroom_dir(classroom_id) / "membership.json"

    # ---- Public API ----
    def save(
        self,
        manifest: MembershipManifest,
        coordinator_public_key: str,
    ) -> None:
        """Write the manifest + coordinator pubkey atomically.

        Overwrites any prior file at the same path (re-join replaces).
        """
        self._classroom_dir(manifest.classroom_id).mkdir(parents=True, exist_ok=True)
        payload = {
            "manifest": encode_membership_manifest(manifest),
            "coordinator_public_key": coordinator_public_key,
        }
        path = self._membership_path(manifest.classroom_id)
        path.write_text(json.dumps(payload, indent=2))

    def load(self, classroom_id: str) -> StoredMembership:
        """Load + re-verify the membership; raise on missing or tamper."""
        path = self._membership_path(classroom_id)
        if not path.is_file():
            raise MembershipNotFoundError(
                f"no membership record for classroom {classroom_id!r}"
            )

        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise MembershipNotFoundError(
                f"membership file for {classroom_id!r} is not valid JSON: {exc}"
            ) from exc

        if not isinstance(raw, dict) or "manifest" not in raw or "coordinator_public_key" not in raw:
            raise MembershipNotFoundError(
                f"membership file for {classroom_id!r} is malformed"
            )

        try:
            manifest = decode_membership_manifest(raw["manifest"])
        except (ValueError, KeyError) as exc:
            raise MembershipNotFoundError(
                f"manifest for {classroom_id!r} failed to decode: {exc}"
            ) from exc

        stored = StoredMembership(
            manifest=manifest,
            coordinator_public_key=str(raw["coordinator_public_key"]),
        )

        # Re-verify on load — on-disk state could have been edited between
        # save and read. Tamper or wrong-key → treat as not-found so the
        # caller doesn't act on untrusted state.
        verify_result = stored.verify()
        if not verify_result.valid:
            raise MembershipNotFoundError(
                f"membership for {classroom_id!r} failed verification: "
                f"{verify_result.reason}"
            )

        return stored

    def list_ids(self) -> list[str]:
        """Return sorted classroom_ids the student is a member of."""
        classrooms_root = self.base_dir / "classrooms"
        if not classrooms_root.is_dir():
            return []
        return sorted(
            child.name
            for child in classrooms_root.iterdir()
            if child.is_dir() and (child / "membership.json").is_file()
        )

    def delete(self, classroom_id: str) -> bool:
        """Remove the membership manifest for ``classroom_id``.

        Returns True if a manifest existed and was removed; False if
        there was nothing on disk. Idempotent — safe to call twice.
        Does not touch sibling files in the class directory (cached
        materials, brief cache, coordinator URL sidecar) so callers
        can decide whether to keep those for offline review.
        """
        path = self._membership_path(classroom_id)
        if not path.is_file():
            return False
        path.unlink()
        return True


# ---------------------------------------------------------------------------
# Convenience functions (thin wrappers over the store)
# ---------------------------------------------------------------------------


def is_member_of(store: MembershipStore, classroom_id: str) -> bool:
    """True iff a valid membership record exists for this classroom."""
    try:
        store.load(classroom_id)
    except MembershipNotFoundError:
        return False
    return True


def list_memberships(store: MembershipStore) -> list[str]:
    """List classroom_ids the student is currently a member of."""
    return store.list_ids()


__all__ = [
    "MembershipNotFoundError",
    "MembershipStore",
    "StoredMembership",
    "is_member_of",
    "list_memberships",
]
