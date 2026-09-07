# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signed manifest of classroom materials — the coordinator's attestation.

Phase 2 of the materials-flow tier. The coordinator signs a list of
every file a student should download + verify before indexing it
locally. Same Ed25519 + canonical-JSON signing pattern used by the
membership manifest in classroom_coordinator.py.

On-wire format is JSON (not base64-wrapped, so the HTTP endpoint can
return it as ``Content-Type: application/json`` and a human can curl
+ jq the response):

    {
      "classroom_id": "NE101",
      "generated_at": "2026-04-22T23:15:00+00:00",
      "entries": [
        {"file_id": "...", "title": "...", "content_hash": "...",
         "size_bytes": 1234}
      ],
      "signature": "<base64 Ed25519 over canonical payload>"
    }

Canonical payload covers every field except ``signature`` itself,
with entries sorted by ``file_id`` so the order of uploads on the
instructor side doesn't change the bytes that get signed.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

from axiom.vega.federation.identity import NodeIdentity

from .classroom_materials import ClassroomMaterialsStore

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterialsManifestEntry:
    file_id: str
    title: str
    content_hash: str
    size_bytes: int


@dataclass(frozen=True)
class MaterialsManifest:
    classroom_id: str
    generated_at: str  # ISO 8601 with timezone
    entries: list[MaterialsManifestEntry]
    signature: str  # base64 Ed25519 signature over canonical payload


@dataclass(frozen=True)
class ManifestVerifyResult:
    valid: bool
    reason: str | None = None


_REQUIRED_FIELDS = ("classroom_id", "generated_at", "entries", "signature")


# ---------------------------------------------------------------------------
# Canonical signing payload
# ---------------------------------------------------------------------------


def _canonical_payload(manifest: MaterialsManifest) -> bytes:
    """Bytes that the signature covers. Deterministic for equal content."""
    payload = {
        "classroom_id": manifest.classroom_id,
        "generated_at": manifest.generated_at,
        "entries": [
            asdict(e)
            for e in sorted(manifest.entries, key=lambda e: e.file_id)
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_private_key(identity: NodeIdentity):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    priv_bytes = identity.private_key_path.read_bytes()
    return load_pem_private_key(priv_bytes, password=None)


# ---------------------------------------------------------------------------
# Build + sign
# ---------------------------------------------------------------------------


def build_materials_manifest(
    *,
    identity: NodeIdentity,
    classroom_id: str,
    store: ClassroomMaterialsStore,
) -> MaterialsManifest:
    """Snapshot ``store``'s current entries and sign the result."""
    entries = [
        MaterialsManifestEntry(
            file_id=e.file_id,
            title=e.title,
            content_hash=e.content_hash,
            size_bytes=e.size_bytes,
        )
        for e in store.list_entries()
    ]
    entries.sort(key=lambda e: e.file_id)

    unsigned = MaterialsManifest(
        classroom_id=classroom_id,
        generated_at=datetime.now(UTC).isoformat(),
        entries=entries,
        signature="",
    )
    payload = _canonical_payload(unsigned)
    priv = _load_private_key(identity)
    sig_bytes = priv.sign(payload)
    signature = base64.b64encode(sig_bytes).decode("ascii")
    return replace(unsigned, signature=signature)


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify_materials_manifest(
    manifest: MaterialsManifest,
    *,
    coordinator_public_key: str,
) -> ManifestVerifyResult:
    """Check that ``manifest.signature`` was produced by ``coordinator_public_key``."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    try:
        pub_bytes = base64.b64decode(coordinator_public_key)
    except (binascii.Error, ValueError) as exc:
        return ManifestVerifyResult(
            valid=False, reason=f"coordinator pubkey not base64: {exc}"
        )
    try:
        pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
    except ValueError as exc:
        return ManifestVerifyResult(
            valid=False, reason=f"coordinator pubkey not Ed25519: {exc}"
        )
    try:
        sig = base64.b64decode(manifest.signature)
    except (binascii.Error, ValueError) as exc:
        return ManifestVerifyResult(
            valid=False, reason=f"signature not base64: {exc}"
        )

    try:
        pub.verify(sig, _canonical_payload(manifest))
    except InvalidSignature:
        return ManifestVerifyResult(
            valid=False,
            reason="signature verification failed (manifest tampered or wrong coord key)",
        )
    return ManifestVerifyResult(valid=True)


# ---------------------------------------------------------------------------
# Wire format — JSON, not base64 (it's served raw over HTTP)
# ---------------------------------------------------------------------------


def encode_materials_manifest(manifest: MaterialsManifest) -> str:
    """Serialize for over-the-wire transport. Returns pretty JSON."""
    payload = {
        "classroom_id": manifest.classroom_id,
        "generated_at": manifest.generated_at,
        "entries": [asdict(e) for e in manifest.entries],
        "signature": manifest.signature,
    }
    return json.dumps(payload, indent=2)


def decode_materials_manifest(encoded: str) -> MaterialsManifest:
    if not encoded or not encoded.strip():
        raise ValueError("empty manifest")
    try:
        raw = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("manifest payload is not a JSON object")

    missing = [f for f in _REQUIRED_FIELDS if f not in raw]
    if missing:
        raise ValueError(
            f"manifest missing field(s): {', '.join(missing)}"
        )

    entries_raw = raw["entries"]
    if not isinstance(entries_raw, list):
        raise ValueError("manifest 'entries' must be a list")
    entries = []
    for e in entries_raw:
        if not isinstance(e, dict):
            raise ValueError("manifest entry is not a JSON object")
        entries.append(
            MaterialsManifestEntry(
                file_id=str(e["file_id"]),
                title=str(e["title"]),
                content_hash=str(e["content_hash"]),
                size_bytes=int(e["size_bytes"]),
            )
        )

    return MaterialsManifest(
        classroom_id=str(raw["classroom_id"]),
        generated_at=str(raw["generated_at"]),
        entries=entries,
        signature=str(raw["signature"]),
    )


__all__ = [
    "ManifestVerifyResult",
    "MaterialsManifest",
    "MaterialsManifestEntry",
    "build_materials_manifest",
    "decode_materials_manifest",
    "encode_materials_manifest",
    "verify_materials_manifest",
]
