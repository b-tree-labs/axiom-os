# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cryptographic provenance attestation + append-only audit log.

Closes the Collaborative Memory paper's §6 admission that provenance
is claim-style, not cryptographically attested. Axiom's federation
threat model (10k–100k nodes, regulated sectors) cannot rely on
unsigned claims — fragments must be signed, audit entries must be
tamper-evident, and verification must be cheap at retrieval time.

Two primitives:

1. **Fragment signing** — Ed25519 signature over a deterministic
   canonical encoding of the fragment (minus the signature slot).
   Tampering with any field invalidates the signature.

2. **Append-only audit log** — JSONL file. Each entry records a
   memory access (read/write/revoke) with principal, agent, fragment,
   and outcome. Optional per-entry signing for integrity across
   federation boundaries.

Federation layer (task #16, already built) uses these primitives
when fragments cross node boundaries.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.vega.identity.keypair import Keypair
from axiom.vega.identity.keypair import verify as _ed_verify

from .fragment import MemoryFragment

# ---------------------------------------------------------------------------
# Canonical encoding
# ---------------------------------------------------------------------------


def canonical_bytes(fragment: MemoryFragment) -> bytes:
    """Deterministic canonical byte encoding of a fragment.

    Excludes the `signature` slot itself (so signing/verifying are
    self-consistent) and serializes with sorted keys so dict
    insertion order can't change the output.
    """
    payload = fragment.to_dict()
    payload.pop("signature", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------


def sign_fragment(fragment: MemoryFragment, keypair: Keypair) -> MemoryFragment:
    """Return a new fragment with the `signature` field set.

    Uses dataclasses.replace so every existing field (including
    `ownership`) is preserved — prevents silent field-drop bugs
    when new slots are added to MemoryFragment.
    """
    import dataclasses as _dc

    sig = keypair.sign(canonical_bytes(fragment))
    return _dc.replace(fragment, signature=sig.hex())


def verify_fragment_signature(
    fragment: MemoryFragment, public_bytes: bytes
) -> bool:
    """True iff the signature verifies against the given public key."""
    if not fragment.signature:
        return False
    try:
        sig = bytes.fromhex(fragment.signature)
    except ValueError:
        return False
    return _ed_verify(public_bytes, canonical_bytes(fragment), sig)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _canonical_entry_bytes(entry: dict) -> bytes:
    """Canonical bytes for an audit entry (excluding the signature)."""
    payload = {k: v for k, v in entry.items() if k != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass
class AuditLog:
    """Append-only JSONL audit log.

    Thread-safety: a single process appending is safe; multi-process
    appends to the same file need OS-level file locking (out of scope).
    """

    path: Path
    signing_keypair: Keypair | None = None

    def record(
        self,
        entry_type: str,
        principal_id: str,
        agent_id: str,
        fragment_id: str,
        outcome: str,
        **extra: Any,
    ) -> dict:
        """Append one record. Returns the stored entry."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "entry_type": entry_type,
            "principal_id": principal_id,
            "agent_id": agent_id,
            "fragment_id": fragment_id,
            "outcome": outcome,
        }
        if extra:
            entry.update(extra)
        if self.signing_keypair is not None:
            sig = self.signing_keypair.sign(_canonical_entry_bytes(entry))
            entry["signature"] = sig.hex()
        from axiom.infra.state import locked_append_jsonl

        # Locked, fsynced, and it terminates a previous half-written line first
        # so this entry is not glued onto a fragment and lost with it.
        locked_append_jsonl(self.path, entry)
        return entry

    def read_all(self) -> Iterator[dict]:
        """Every readable entry, in order. Unreadable lines are skipped LOUDLY.

        The trail is signed per entry, so one cut-short line costs exactly that
        entry; it must not cost the reader the whole trail (it used to raise
        ``JSONDecodeError`` and name nothing).
        """
        import logging

        from axiom.infra.state_integrity import read_state_bytes

        raw = read_state_bytes(self.path)
        if raw is None:
            return
        skipped = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                skipped += 1
                continue
            if isinstance(entry, dict):
                yield entry
            else:
                skipped += 1
        if skipped:
            logging.getLogger(__name__).warning(
                "%s: %d unreadable audit line(s) skipped; the rest of the trail is intact "
                "and the file was not modified", self.path, skipped,
            )

    def query(
        self,
        fragment_id: str | None = None,
        principal_id: str | None = None,
        entry_type: str | None = None,
    ) -> Iterator[dict]:
        """Filter entries by common fields (cheap linear scan)."""
        for entry in self.read_all():
            if fragment_id is not None and entry.get("fragment_id") != fragment_id:
                continue
            if principal_id is not None and entry.get("principal_id") != principal_id:
                continue
            if entry_type is not None and entry.get("entry_type") != entry_type:
                continue
            yield entry


def verify_audit_entry(entry: dict, public_bytes: bytes) -> bool:
    """True iff the signed entry verifies against the given public key."""
    sig_hex = entry.get("signature")
    if not sig_hex:
        return False
    try:
        sig = bytes.fromhex(sig_hex)
    except ValueError:
        return False
    return _ed_verify(public_bytes, _canonical_entry_bytes(entry), sig)
