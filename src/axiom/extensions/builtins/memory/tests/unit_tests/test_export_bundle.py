# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``memory.export`` skill — signed portable bundle (ADR-087 D9).

Bundle = tar.gz with fragments.jsonl + manifest.json (schema versions,
counts, content hashes, node pubkey) + sessions.jsonl + audit.jsonl +
manifest.sig. Vault is excluded in P0 — ``include_vault`` is refused
outright (opt-in re-encryption is ADR-087 OQ4, deferred).
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest

PRINCIPAL = "@alice:personal"
OTHER = "@bob:personal"


@pytest.fixture
def composition(tmp_path: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base = tmp_path / "memory"
    base.mkdir()
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


@pytest.fixture
def populated(composition, tmp_path: Path):
    """Composition with fragments for two principals (incl. vault) and
    session checkpoints for both."""
    composition.write(
        content={"fact": "likes tea"}, cognitive_type="semantic",
        principal_id=PRINCIPAL, agents={"axi"}, resources=set(),
    )
    composition.write(
        content={"note": "standup", "event_time": "2026-07-01T10:00:00+00:00"},
        cognitive_type="episodic",
        principal_id=PRINCIPAL, agents=set(), resources=set(),
    )
    composition.write(
        content={"secret": "API_KEY=xyz"}, cognitive_type="vault",
        principal_id=PRINCIPAL, agents=set(), resources=set(),
    )
    composition.write(
        content={"fact": "someone else's"}, cognitive_type="semantic",
        principal_id=OTHER, agents=set(), resources=set(),
    )

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "s1.json").write_text(json.dumps({
        "session_id": "session://s1", "principal_id": PRINCIPAL,
        "name": "work-2026-07-01",
    }))
    (sessions / "s2.json").write_text(json.dumps({
        "session_id": "session://s2", "principal_id": OTHER,
        "name": "other-2026-07-02",
    }))
    return composition, sessions


def _run_export(composition, sessions: Path, out: Path, **overrides):
    from axiom.extensions.builtins.memory.skills.export_bundle import (
        export_bundle,
    )

    params = {
        "composition": composition,
        "principal": PRINCIPAL,
        "out": str(out),
        "sessions_dir": str(sessions),
    }
    params.update(overrides)
    return export_bundle(params, None)


def _read_bundle(out: Path) -> dict[str, bytes]:
    with tarfile.open(out, "r:gz") as tf:
        return {m.name: tf.extractfile(m).read() for m in tf.getmembers()}


class TestExportBundle:
    def test_bundle_members_and_counts(self, populated, tmp_path):
        composition, sessions = populated
        out = tmp_path / "bundle.tar.gz"
        result = _run_export(composition, sessions, out)
        assert result.ok, result.errors
        members = _read_bundle(out)
        assert set(members) == {
            "manifest.json", "manifest.sig", "fragments.jsonl",
            "sessions.jsonl", "aliases.jsonl", "audit.jsonl",
        }
        manifest = json.loads(members["manifest.json"])
        # v2: aliases.jsonl joined the member set (P2 item 6 — D3 alias
        # sets ride migrations). Import has no version gate.
        assert manifest["bundle_format_version"] == 2
        assert manifest["principal"] == PRINCIPAL
        assert manifest["counts"]["fragments"] == 2
        assert manifest["counts"]["vault_excluded"] == 1
        assert manifest["counts"]["sessions"] == 1
        assert manifest["counts"]["aliases"] == 0

    def test_vault_never_in_bundle(self, populated, tmp_path):
        composition, sessions = populated
        out = tmp_path / "bundle.tar.gz"
        assert _run_export(composition, sessions, out).ok
        members = _read_bundle(out)
        for line in members["fragments.jsonl"].splitlines():
            frag = json.loads(line)
            assert frag["cognitive_type"] != "vault"
        assert b"API_KEY" not in members["fragments.jsonl"]

    def test_other_principal_not_included(self, populated, tmp_path):
        composition, sessions = populated
        out = tmp_path / "bundle.tar.gz"
        assert _run_export(composition, sessions, out).ok
        members = _read_bundle(out)
        for line in members["fragments.jsonl"].splitlines():
            frag = json.loads(line)
            assert frag["provenance"]["principal_id"] == PRINCIPAL
        sessions_lines = [
            json.loads(line)
            for line in members["sessions.jsonl"].splitlines()
        ]
        assert len(sessions_lines) == 1
        assert sessions_lines[0]["data"]["principal_id"] == PRINCIPAL

    def test_manifest_hashes_match_members(self, populated, tmp_path):
        composition, sessions = populated
        out = tmp_path / "bundle.tar.gz"
        assert _run_export(composition, sessions, out).ok
        members = _read_bundle(out)
        manifest = json.loads(members["manifest.json"])
        for name, expected in manifest["files"].items():
            assert hashlib.sha256(members[name]).hexdigest() == expected

    def test_manifest_signature_verifies(self, populated, tmp_path):
        from axiom.vega.identity.keypair import verify

        composition, sessions = populated
        out = tmp_path / "bundle.tar.gz"
        assert _run_export(composition, sessions, out).ok
        members = _read_bundle(out)
        manifest = json.loads(members["manifest.json"])
        pubkey = bytes.fromhex(manifest["node_pubkey"])
        assert pubkey == composition.signing_keypair.public_bytes
        canonical = json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        sig = bytes.fromhex(members["manifest.sig"].decode())
        assert verify(pubkey, canonical, sig)

    def test_fragment_hashes_in_manifest(self, populated, tmp_path):
        composition, sessions = populated
        out = tmp_path / "bundle.tar.gz"
        assert _run_export(composition, sessions, out).ok
        members = _read_bundle(out)
        manifest = json.loads(members["manifest.json"])
        frags = {
            json.loads(line)["id"]: line
            for line in members["fragments.jsonl"].splitlines()
        }
        assert set(manifest["fragment_hashes"]) == set(frags)
        for fid, line in frags.items():
            digest = hashlib.sha256(
                json.dumps(
                    json.loads(line), sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            assert manifest["fragment_hashes"][fid] == digest

    def test_include_vault_refused(self, populated, tmp_path):
        composition, sessions = populated
        out = tmp_path / "bundle.tar.gz"
        result = _run_export(composition, sessions, out, include_vault=True)
        assert not result.ok
        assert any("vault" in e.lower() for e in result.errors)
        assert not out.exists()

    def test_export_audit_event_recorded(self, populated, tmp_path):
        composition, sessions = populated
        out = tmp_path / "bundle.tar.gz"
        assert _run_export(composition, sessions, out).ok
        events = list(
            composition.audit_log.query(entry_type="export")
        )
        assert len(events) == 1
        assert events[0]["principal_id"] == PRINCIPAL
        assert events[0]["outcome"] == "ok"

    def test_missing_principal_refused(self, populated, tmp_path):
        composition, sessions = populated
        result = _run_export(
            composition, sessions, tmp_path / "b.tar.gz", principal=None,
        )
        assert not result.ok

    def test_forgotten_fragments_not_exported(self, populated, tmp_path):
        composition, sessions = populated
        live = [
            a for a in composition.artifact_registry.list(kind="fragment")
            if (a.data or {}).get("provenance", {}).get("principal_id")
            == PRINCIPAL
            and (a.data or {}).get("cognitive_type") == "semantic"
        ]
        composition.forget(
            [live[0].name], requester=PRINCIPAL, agent="axi-memory",
        )
        out = tmp_path / "bundle.tar.gz"
        assert _run_export(composition, sessions, out).ok
        members = _read_bundle(out)
        manifest = json.loads(members["manifest.json"])
        assert manifest["counts"]["fragments"] == 1
