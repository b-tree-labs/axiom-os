# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``memory.import`` skill — re-home a signed portable bundle
(ADR-087 D9 + ADR-026 dual-signature ceremony).

Import is fail-closed and two-phase: *everything* is verified and
transformed in memory first — member hashes, manifest signature,
vault-never, schema decodability, count/hash zero-loss checks — and only
then does the write pass touch the registry. A bundle that fails any
check imports nothing.

Ceremony (ADR-026 transfer semantics, applied per fragment):

- **Outgoing consent** = the bundle's manifest signature. The source
  node key holder signed the exact content leaving their store.
- **Incoming acceptance** = the destination node key signs the same
  canonical manifest bytes at import time. A node without a signing key
  cannot accept a transfer.
- Transfer is a clean break: the assumed principal becomes master,
  delegations clear. Original (T, U, A, R, S) provenance is preserved
  untouched; previously-native fragments gain their ADR-087
  ``SourceOrigin`` coordinate ``(axiom, <source principal>, <fragment
  id>)`` so per-source extraction survives the move; fragments are
  re-signed under the destination node key.

Dedup is the exact tier only (ADR-087 D3): an incoming fragment whose id
already exists with identical content is skipped (idempotent re-import);
same id with different content is a conflict — the existing fragment is
never overwritten and the conflict is reported.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .export_bundle import canonical_json_bytes, default_sessions_dir

_AGENT = "axi-memory"

_REQUIRED_MEMBERS = ("manifest.json", "manifest.sig", "fragments.jsonl")


def _read_members(bundle: Path) -> dict[str, bytes]:
    with tarfile.open(bundle, "r:gz") as tf:
        return {
            m.name: tf.extractfile(m).read()
            for m in tf.getmembers()
            if m.isfile()
        }


def import_bundle(
    params: dict[str, Any], ctx: SkillContext | None
) -> SkillResult:
    """Verify + re-home a portable bundle into this node's store."""
    composition = params.get("composition")
    if composition is None:
        return SkillResult(ok=False, errors=["no composition service provided"])

    bundle_raw = params.get("bundle")
    if not bundle_raw:
        return SkillResult(ok=False, errors=["bundle path is required"])
    bundle = Path(bundle_raw)

    assume = params.get("assume_principal")
    if not assume:
        return SkillResult(
            ok=False,
            errors=[
                "--assume-principal <principal> is required: import "
                "re-homes ownership and must know the destination identity"
            ],
        )

    keypair = composition.signing_keypair
    if keypair is None:
        return SkillResult(
            ok=False,
            errors=[
                "destination node has no signing keypair; cannot sign the "
                "incoming acceptance for the ADR-026 re-home ceremony"
            ],
        )

    dry_run = bool(params.get("dry_run", False))
    sessions_dir = Path(params.get("sessions_dir") or default_sessions_dir())

    def deny(reason: str, message: str) -> SkillResult:
        composition.audit_log.record(
            entry_type="import_denied",
            principal_id=assume,
            agent_id=_AGENT,
            fragment_id="",
            outcome=reason,
            bundle=str(bundle),
        )
        return SkillResult(ok=False, errors=[message])

    # ---- Phase 1: verify everything, write nothing -------------------------

    if not bundle.exists():
        return SkillResult(ok=False, errors=[f"bundle not found: {bundle}"])
    try:
        members = _read_members(bundle)
    except (tarfile.TarError, OSError) as exc:
        return deny("unreadable_bundle", f"unreadable bundle: {exc}")

    for name in _REQUIRED_MEMBERS:
        if name not in members:
            return deny(
                "member_missing",
                f"bundle is missing required member {name!r} — refusing "
                "(unsigned or incomplete bundles never import)",
            )

    try:
        manifest = json.loads(members["manifest.json"])
    except json.JSONDecodeError as exc:
        return deny("manifest_invalid", f"manifest.json is not valid JSON: {exc}")

    # Member hashes: every file the manifest names must be present and
    # byte-identical.
    for name, expected in (manifest.get("files") or {}).items():
        blob = members.get(name)
        if blob is None:
            return deny(
                "member_missing", f"manifest names {name!r} but it is absent"
            )
        if hashlib.sha256(blob).hexdigest() != expected:
            return deny(
                "hash_mismatch",
                f"bundle member {name!r} does not match its manifest hash — "
                "bundle is corrupt or tampered; nothing was imported",
            )

    # Manifest signature — the outgoing-consent half of the ceremony.
    from axiom.vega.identity.keypair import verify as _ed_verify

    try:
        pubkey = bytes.fromhex(manifest.get("node_pubkey", ""))
        consent = bytes.fromhex(members["manifest.sig"].decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return deny(
            "signature_invalid", "bundle signature is malformed — refusing"
        )
    if not pubkey or not _ed_verify(
        pubkey, canonical_json_bytes(manifest), consent
    ):
        return deny(
            "signature_invalid",
            "bundle signature does not verify against the source node key "
            "— refusing (outgoing consent cannot be established)",
        )

    source_principal = manifest.get("principal", "")

    try:
        payloads = [
            json.loads(line)
            for line in members["fragments.jsonl"].splitlines()
            if line.strip()
        ]
    except json.JSONDecodeError as exc:
        return deny("fragments_invalid", f"fragments.jsonl is corrupt: {exc}")

    # Vault never rides a bundle — whoever signed it.
    for payload in payloads:
        if payload.get("cognitive_type") == "vault":
            return deny(
                "vault_in_bundle",
                "bundle contains vault content; vault is never imported "
                "from a bundle (ADR-087 D7) — refusing the entire bundle",
            )

    # Zero-loss: counts and per-fragment hashes must line up exactly.
    counts = manifest.get("counts") or {}
    fragment_hashes = manifest.get("fragment_hashes") or {}
    if counts.get("fragments") != len(payloads) or set(
        fragment_hashes
    ) != {p.get("id") for p in payloads}:
        return deny(
            "count_mismatch",
            "fragment count/ids do not match the manifest — refusing",
        )
    for payload in payloads:
        digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        if fragment_hashes.get(payload["id"]) != digest:
            return deny(
                "hash_mismatch",
                f"fragment {payload['id']} does not match its manifest "
                "hash — refusing",
            )

    # Decode + transform in memory. Any failure aborts before any write.
    from axiom.memory.exceptions import UnsupportedSchemaError
    from axiom.memory.fragment import SourceOrigin, fragment_from_dict
    from axiom.memory.ownership import new_ownership, transfer

    acceptance = keypair.sign(canonical_json_bytes(manifest))
    imported_at = datetime.now(UTC).isoformat()

    existing = {
        a.name: (a.data or {})
        for a in composition.artifact_registry.list(kind="fragment")
    }

    to_write = []
    skipped: list[str] = []
    conflicts: list[str] = []
    try:
        for payload in payloads:
            fid = payload["id"]
            if fid in existing:
                if existing[fid].get("content") == payload.get("content"):
                    skipped.append(fid)
                else:
                    conflicts.append(fid)
                continue
            frag = fragment_from_dict(payload)
            own = frag.ownership or new_ownership(
                master=frag.provenance.principal_id
            )
            new_own = transfer(
                own,
                new_master=assume,
                outgoing_signature=consent,
                incoming_acceptance=acceptance,
            )
            origin = frag.provenance.origin or SourceOrigin(
                harness="axiom",
                account=source_principal,
                source_ref=fid,
                imported_at=imported_at,
            )
            frag = dataclasses.replace(
                frag,
                provenance=dataclasses.replace(
                    frag.provenance, origin=origin
                ),
                ownership=new_own,
                schema_version=3,
            )
            from axiom.memory.attest import sign_fragment

            to_write.append(sign_fragment(frag, keypair))
    except (UnsupportedSchemaError, KeyError, ValueError) as exc:
        return deny(
            "fragment_undecodable",
            f"bundle fragment could not be decoded ({exc}) — refusing; "
            "nothing was imported",
        )

    session_records = []
    for line in members.get("sessions.jsonl", b"").splitlines():
        if line.strip():
            session_records.append(json.loads(line))

    # Alias records (bundle format v2; absent in v1 bundles). Folded
    # source coordinates keep resolving on the destination because
    # fragment ids are preserved by import (D3: no silent loss).
    alias_records = []
    for line in members.get("aliases.jsonl", b"").splitlines():
        if line.strip():
            alias_records.append(json.loads(line))

    if dry_run:
        return SkillResult(
            ok=True,
            value={
                "dry_run": True,
                "would_import": len(to_write),
                "skipped_duplicate": len(skipped),
                "conflicts": conflicts,
                "sessions": len(session_records),
                "aliases": len(alias_records),
                "from_principal": source_principal,
                "assume_principal": assume,
            },
            actions_taken=[],
        )

    # ---- Phase 2: write pass ------------------------------------------------

    for frag in to_write:
        composition.artifact_registry.register(
            kind="fragment", name=frag.id, data=frag.to_dict(),
        )
        composition.audit_log.record(
            entry_type="re_home",
            principal_id=assume,
            agent_id=_AGENT,
            fragment_id=frag.id,
            outcome="ok",
            from_principal=source_principal,
        )

    aliases_imported = 0
    if alias_records:
        from axiom.memory.dedup import ALIAS_KIND, alias_name

        for record in alias_records:
            if not isinstance(record, dict) or not record.get("canonical_id"):
                continue
            name = alias_name(record)
            if composition.artifact_registry.find_by_name(ALIAS_KIND, name):
                continue  # idempotent — re-import never duplicates
            data = dict(record)
            data["principal"] = assume
            composition.artifact_registry.register(
                kind=ALIAS_KIND, name=name, data=data,
            )
            aliases_imported += 1

    sessions_imported = 0
    if session_records:
        sessions_dir.mkdir(parents=True, exist_ok=True)
        for record in session_records:
            filename = record.get("filename")
            data = record.get("data")
            if not filename or data is None:
                continue
            target = sessions_dir / Path(filename).name
            if target.exists():
                continue
            target.write_text(json.dumps(data, indent=2))
            sessions_imported += 1

    composition.audit_log.record(
        entry_type="import",
        principal_id=assume,
        agent_id=_AGENT,
        fragment_id="",
        outcome="ok",
        bundle=str(bundle),
        from_principal=source_principal,
        imported=len(to_write),
        skipped_duplicate=len(skipped),
        conflicts=len(conflicts),
        sessions_imported=sessions_imported,
        aliases_imported=aliases_imported,
    )

    return SkillResult(
        ok=True,
        value={
            "imported": len(to_write),
            "skipped_duplicate": len(skipped),
            "conflicts": conflicts,
            "sessions_imported": sessions_imported,
            "aliases_imported": aliases_imported,
            "from_principal": source_principal,
            "assume_principal": assume,
        },
        actions_taken=(
            [
                f"imported {len(to_write)} fragment(s) re-homed to "
                f"{assume}, {sessions_imported} session checkpoint(s)"
            ]
            if to_write or sessions_imported
            else []
        ),
    )
