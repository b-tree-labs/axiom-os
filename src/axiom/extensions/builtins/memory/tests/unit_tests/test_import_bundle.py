# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``memory.import`` skill — re-home a signed bundle
(ADR-087 D9, ADR-026 ceremony).

Import verifies member hashes + the manifest signature (fail closed),
re-homes ownership to the assumed principal via the dual-signature
ceremony (bundle signature = outgoing consent; destination node key
signs acceptance), preserves original provenance, stamps the ADR-087
SourceOrigin coordinate on previously-native fragments, re-signs under
the destination node key, and dedups exactly so re-import is a no-op.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

SRC_PRINCIPAL = "@alice:personal"
DST_PRINCIPAL = "@alice:work"


def _make_composition(base: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base.mkdir(parents=True, exist_ok=True)
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
def source(tmp_path: Path):
    comp = _make_composition(tmp_path / "src")
    comp.write(
        content={"fact": "likes tea"}, cognitive_type="semantic",
        principal_id=SRC_PRINCIPAL, agents={"axi"}, resources=set(),
    )
    comp.write(
        content={"note": "standup", "event_time": "2026-07-01T10:00:00+00:00"},
        cognitive_type="episodic",
        principal_id=SRC_PRINCIPAL, agents=set(), resources=set(),
    )
    sessions = tmp_path / "src-sessions"
    sessions.mkdir()
    (sessions / "s1.json").write_text(json.dumps({
        "session_id": "session://s1", "principal_id": SRC_PRINCIPAL,
        "name": "personal-2026-07-01",
    }))
    return comp, sessions


@pytest.fixture
def bundle(source, tmp_path: Path) -> Path:
    from axiom.extensions.builtins.memory.skills.export_bundle import (
        export_bundle,
    )

    comp, sessions = source
    out = tmp_path / "bundle.tar.gz"
    result = export_bundle({
        "composition": comp,
        "principal": SRC_PRINCIPAL,
        "out": str(out),
        "sessions_dir": str(sessions),
    }, None)
    assert result.ok, result.errors
    return out


@pytest.fixture
def destination(tmp_path: Path):
    comp = _make_composition(tmp_path / "dst")
    sessions = tmp_path / "dst-sessions"
    sessions.mkdir()
    return comp, sessions


def _run_import(destination, bundle: Path, **overrides):
    from axiom.extensions.builtins.memory.skills.import_bundle import (
        import_bundle,
    )

    comp, sessions = destination
    params = {
        "composition": comp,
        "bundle": str(bundle),
        "assume_principal": DST_PRINCIPAL,
        "sessions_dir": str(sessions),
    }
    params.update(overrides)
    return import_bundle(params, None)


def _members(bundle: Path) -> dict[str, bytes]:
    with tarfile.open(bundle, "r:gz") as tf:
        return {m.name: tf.extractfile(m).read() for m in tf.getmembers()}


def _write_members(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name, blob in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))


def _dest_fragments(comp) -> list:
    from axiom.memory.fragment import fragment_from_dict

    return [
        fragment_from_dict(a.data)
        for a in comp.artifact_registry.list(kind="fragment")
    ]


class TestImportHappyPath:
    def test_zero_loss_and_re_home(self, bundle, destination):
        from axiom.memory.attest import verify_fragment_signature

        comp, _ = destination
        result = _run_import(destination, bundle)
        assert result.ok, result.errors
        assert result.value["imported"] == 2

        frags = _dest_fragments(comp)
        assert len(frags) == 2
        manifest = json.loads(_members(bundle)["manifest.json"])
        assert {f.id for f in frags} == set(manifest["fragment_hashes"])
        for frag in frags:
            # Re-homed: destination principal is the new master.
            assert frag.ownership.master == DST_PRINCIPAL
            assert frag.ownership.delegations == ()
            # Original provenance untouched.
            assert frag.provenance.principal_id == SRC_PRINCIPAL
            # Origin coordinate stamped on previously-native fragments.
            assert frag.provenance.origin is not None
            assert frag.provenance.origin.harness == "axiom"
            assert frag.provenance.origin.account == SRC_PRINCIPAL
            assert frag.provenance.origin.source_ref == frag.id
            # Re-signed under the destination node key.
            assert verify_fragment_signature(
                frag, comp.signing_keypair.public_bytes
            )
            assert frag.schema_version == 3

    def test_recall_parity(self, source, bundle, destination):
        """The same content-level query returns the same fragments on
        source and destination (ids + content match)."""
        src_comp, _ = source
        dst_comp, _ = destination
        assert _run_import(destination, bundle).ok

        def probe(comp):
            return sorted(
                (a.name, json.dumps(a.data.get("content"), sort_keys=True))
                for a in comp.artifact_registry.list(kind="fragment")
                if (a.data or {}).get("provenance", {}).get("principal_id")
                == SRC_PRINCIPAL
            )

        assert probe(src_comp) == probe(dst_comp)

    def test_sessions_imported(self, bundle, destination):
        _, sessions = destination
        assert _run_import(destination, bundle).ok
        files = list(sessions.glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["session_id"] == "session://s1"

    def test_audit_events_on_destination(self, bundle, destination):
        comp, _ = destination
        assert _run_import(destination, bundle).ok
        imports = list(comp.audit_log.query(entry_type="import"))
        assert len(imports) == 1
        assert imports[0]["outcome"] == "ok"
        rehomes = list(comp.audit_log.query(entry_type="re_home"))
        assert len(rehomes) == 2
        for entry in rehomes:
            assert entry["principal_id"] == DST_PRINCIPAL
            assert entry["from_principal"] == SRC_PRINCIPAL

    def test_dry_run_writes_nothing(self, bundle, destination):
        comp, sessions = destination
        result = _run_import(destination, bundle, dry_run=True)
        assert result.ok
        assert result.value["dry_run"] is True
        assert result.value["would_import"] == 2
        assert _dest_fragments(comp) == []
        assert list(sessions.glob("*.json")) == []


class TestIdempotency:
    def test_second_import_is_noop(self, bundle, destination):
        comp, sessions = destination
        assert _run_import(destination, bundle).ok
        first = {
            a.name: json.dumps(a.data, sort_keys=True)
            for a in comp.artifact_registry.list(kind="fragment")
        }
        result = _run_import(destination, bundle)
        assert result.ok
        assert result.value["imported"] == 0
        assert result.value["skipped_duplicate"] == 2
        second = {
            a.name: json.dumps(a.data, sort_keys=True)
            for a in comp.artifact_registry.list(kind="fragment")
        }
        assert first == second
        assert len(list(sessions.glob("*.json"))) == 1

    def test_conflicting_id_never_overwritten(self, bundle, destination):
        comp, _ = destination
        manifest = json.loads(_members(bundle)["manifest.json"])
        victim = next(iter(manifest["fragment_hashes"]))
        # Pre-plant a different fragment under the same id.
        planted = {"planted": True}
        line = next(
            json.loads(ln)
            for ln in _members(bundle)["fragments.jsonl"].splitlines()
            if json.loads(ln)["id"] == victim
        )
        line = dict(line)
        line["content"] = planted
        comp.artifact_registry.register(
            kind="fragment", name=victim, data=line,
        )
        result = _run_import(destination, bundle)
        assert result.ok
        assert result.value["conflicts"] == [victim]
        assert result.value["imported"] == 1
        kept = [
            a for a in comp.artifact_registry.list(kind="fragment")
            if a.name == victim
        ]
        assert len(kept) == 1
        assert kept[0].data["content"] == planted


class TestFailClosed:
    def test_tampered_member_refused(self, bundle, destination):
        comp, _ = destination
        members = _members(bundle)
        tampered = members["fragments.jsonl"].replace(
            b"likes tea", b"likes ETH"
        )
        members["fragments.jsonl"] = tampered
        _write_members(bundle, members)
        result = _run_import(destination, bundle)
        assert not result.ok
        assert _dest_fragments(comp) == []
        denied = list(comp.audit_log.query(entry_type="import_denied"))
        assert len(denied) == 1

    def test_invalid_signature_refused(self, bundle, destination):
        comp, _ = destination
        members = _members(bundle)
        members["manifest.sig"] = b"00" * 64
        _write_members(bundle, members)
        result = _run_import(destination, bundle)
        assert not result.ok
        assert _dest_fragments(comp) == []

    def test_missing_signature_refused(self, bundle, destination):
        comp, _ = destination
        members = _members(bundle)
        del members["manifest.sig"]
        _write_members(bundle, members)
        result = _run_import(destination, bundle)
        assert not result.ok
        assert _dest_fragments(comp) == []

    def test_no_destination_key_refused(self, bundle, destination, tmp_path):
        """No destination node key → no incoming acceptance → no ceremony."""
        import dataclasses

        comp, sessions = destination
        keyless = dataclasses.replace(comp, signing_keypair=None)
        result = _run_import((keyless, sessions), bundle)
        assert not result.ok
        assert any("accept" in e.lower() for e in result.errors)
        assert _dest_fragments(comp) == []

    def test_vault_in_bundle_refused_even_when_signed(
        self, source, bundle, destination
    ):
        """A correctly signed bundle containing vault content is refused:
        vault never rides a bundle, whoever signed it."""
        from axiom.extensions.builtins.memory.skills.export_bundle import (
            canonical_json_bytes,
        )
        from axiom.memory.fragment import create_fragment

        src_comp, _ = source
        comp, _ = destination
        members = _members(bundle)

        vault_frag = create_fragment(
            content={"secret": "API_KEY=xyz"}, cognitive_type="vault",
            principal_id=SRC_PRINCIPAL, agents=set(), resources=set(),
        ).to_dict()
        lines = members["fragments.jsonl"].splitlines()
        lines.append(canonical_json_bytes(vault_frag))
        members["fragments.jsonl"] = b"\n".join(lines) + b"\n"

        manifest = json.loads(members["manifest.json"])
        manifest["files"]["fragments.jsonl"] = hashlib.sha256(
            members["fragments.jsonl"]
        ).hexdigest()
        manifest["fragment_hashes"][vault_frag["id"]] = hashlib.sha256(
            canonical_json_bytes(vault_frag)
        ).hexdigest()
        manifest["counts"]["fragments"] += 1
        members["manifest.json"] = json.dumps(manifest, indent=2).encode()
        members["manifest.sig"] = src_comp.signing_keypair.sign(
            canonical_json_bytes(manifest)
        ).hex().encode("ascii")
        _write_members(bundle, members)

        result = _run_import(destination, bundle)
        assert not result.ok
        assert any("vault" in e.lower() for e in result.errors)
        assert _dest_fragments(comp) == []

    def test_missing_assume_principal_refused(self, bundle, destination):
        result = _run_import(destination, bundle, assume_principal=None)
        assert not result.ok
