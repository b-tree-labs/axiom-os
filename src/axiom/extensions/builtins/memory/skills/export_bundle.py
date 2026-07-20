# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``memory.export`` skill — signed portable memory bundle (ADR-087 D9).

Produces a single tar.gz bundle for one principal:

- ``fragments.jsonl`` — the principal's live (non-tombstoned) fragments,
  one persisted-payload JSON per line. ``vault`` is excluded in P0:
  opt-in re-encrypted vault export is ADR-087 open question 4 and any
  ``include_vault`` request is refused outright rather than
  half-implemented.
- ``sessions.jsonl`` — the principal's session checkpoints (one line per
  checkpoint file: ``{"filename": ..., "data": {...}}``).
- ``aliases.jsonl`` — the principal's dedup alias records (ADR-087 D3):
  every folded source coordinate → its canonical fragment. Fragment ids
  are preserved by import, so aliases ride verbatim and per-source
  extraction keeps working after migration (bundle format v2).
- ``audit.jsonl`` — the principal's slice of the audit log, carried for
  chain-of-custody evidence on the destination.
- ``manifest.json`` — bundle format version, principal, node pubkey,
  counts (incl. what was excluded), per-member sha256, per-fragment
  content hashes, schema-version histogram.
- ``manifest.sig`` — Ed25519 signature by the node key over the
  canonical manifest bytes. Verifying it authenticates every member
  (via the member hashes) and doubles as the outgoing-consent half of
  the ADR-026 re-home ceremony on import.

The ``composition`` is injected via ``params`` (forget-skill pattern) so
the skill stays a pure, testable function; the CLI builds the runtime
composition and passes it in.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

_AGENT = "axi-memory"

# v2: adds the optional ``aliases.jsonl`` member (D3 alias sets ride
# migrations). Import has no version gate; v1 bundles still import.
BUNDLE_FORMAT_VERSION = 2

_MEMBERS = ("fragments.jsonl", "sessions.jsonl", "aliases.jsonl", "audit.jsonl")


def canonical_json_bytes(payload: Any) -> bytes:
    """Deterministic JSON encoding shared by hashing + signing."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def default_sessions_dir() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "sessions"


def _collect_fragments(
    composition: Any, principal: str
) -> tuple[list[dict], int]:
    """The principal's live fragment payloads, vault excluded.

    Returns ``(payloads, vault_excluded_count)``. Registry ``list``
    already excludes tombstoned rows, so forgotten fragments never
    leave the node.
    """
    payloads: list[dict] = []
    vault_excluded = 0
    seen: set[str] = set()
    for art in composition.artifact_registry.list(kind="fragment"):
        data = art.data or {}
        prov = data.get("provenance") or {}
        if prov.get("principal_id") != principal:
            continue
        if art.name in seen:
            continue
        seen.add(art.name)
        if data.get("cognitive_type") == "vault":
            vault_excluded += 1
            continue
        payloads.append(data)
    return payloads, vault_excluded


def _collect_aliases(composition: Any, principal: str) -> list[dict]:
    """The principal's live dedup alias records (folded coordinates)."""
    from axiom.memory.dedup import ALIAS_KIND

    return [
        dict(art.data or {})
        for art in composition.artifact_registry.list(kind=ALIAS_KIND)
        if (art.data or {}).get("principal") == principal
    ]


def _collect_sessions(sessions_dir: Path, principal: str) -> list[dict]:
    """Session checkpoint files belonging to the principal."""
    records: list[dict] = []
    if not sessions_dir.is_dir():
        return records
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("principal_id") == principal:
            records.append({"filename": path.name, "data": data})
    return records


def export_bundle(
    params: dict[str, Any], ctx: SkillContext | None
) -> SkillResult:
    """Export a principal's memory as a signed portable bundle."""
    composition = params.get("composition")
    if composition is None:
        return SkillResult(ok=False, errors=["no composition service provided"])

    if params.get("include_vault"):
        return SkillResult(
            ok=False,
            errors=[
                "vault content is never exported in plaintext; opt-in "
                "re-encrypted vault export (ADR-087 OQ4) is not yet "
                "supported — re-run without --include-vault"
            ],
        )

    principal = params.get("principal")
    if not principal:
        return SkillResult(ok=False, errors=["--principal is required"])

    out_raw = params.get("out")
    if not out_raw:
        return SkillResult(ok=False, errors=["--out <bundle path> is required"])
    out = Path(out_raw)

    keypair = composition.signing_keypair
    if keypair is None:
        return SkillResult(
            ok=False,
            errors=[
                "this node has no signing keypair; bundles must be signed "
                "(unsigned bundles are refused on import)"
            ],
        )

    sessions_dir = Path(
        params.get("sessions_dir") or default_sessions_dir()
    )

    fragments, vault_excluded = _collect_fragments(composition, principal)
    sessions = _collect_sessions(sessions_dir, principal)
    aliases = _collect_aliases(composition, principal)
    audit_entries = list(
        composition.audit_log.query(principal_id=principal)
    )

    members: dict[str, bytes] = {
        "fragments.jsonl": b"".join(
            canonical_json_bytes(f) + b"\n" for f in fragments
        ),
        "sessions.jsonl": b"".join(
            canonical_json_bytes(s) + b"\n" for s in sessions
        ),
        "aliases.jsonl": b"".join(
            canonical_json_bytes(a) + b"\n" for a in aliases
        ),
        "audit.jsonl": b"".join(
            canonical_json_bytes(e) + b"\n" for e in audit_entries
        ),
    }

    schema_versions: dict[str, int] = {}
    for f in fragments:
        key = str(f.get("schema_version", 1))
        schema_versions[key] = schema_versions.get(key, 0) + 1

    manifest = {
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "principal": principal,
        "node_pubkey": keypair.public_bytes.hex(),
        "counts": {
            "fragments": len(fragments),
            "vault_excluded": vault_excluded,
            "sessions": len(sessions),
            "aliases": len(aliases),
            "audit_entries": len(audit_entries),
        },
        "schema_versions": schema_versions,
        "files": {name: _sha256(blob) for name, blob in members.items()},
        "fragment_hashes": {
            f["id"]: _sha256(canonical_json_bytes(f)) for f in fragments
        },
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    signature = keypair.sign(canonical_json_bytes(manifest))

    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tf:
        for name, blob in (
            ("manifest.json", manifest_bytes),
            ("manifest.sig", signature.hex().encode("ascii")),
            *members.items(),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))

    composition.audit_log.record(
        entry_type="export",
        principal_id=principal,
        agent_id=_AGENT,
        fragment_id="",
        outcome="ok",
        bundle=str(out),
        fragments=len(fragments),
        vault_excluded=vault_excluded,
        sessions=len(sessions),
    )

    return SkillResult(
        ok=True,
        value={
            "bundle": str(out),
            "principal": principal,
            "counts": manifest["counts"],
        },
        actions_taken=[
            f"exported {len(fragments)} fragment(s), {len(sessions)} "
            f"session checkpoint(s), {len(audit_entries)} audit entr(ies) "
            f"to {out}"
        ],
    )
