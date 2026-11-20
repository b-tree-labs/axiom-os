# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for harness session-store bundle members (bundle v3, ADR-098 D1).

Export carries a harness's native session store (transcripts, metadata,
checkpoints) as an inner-tar member behind the fail-closed secret gate;
import restores files into the destination home, never overwriting.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

PRINCIPAL = "@alice:personal"


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
def source_home(tmp_path: Path) -> Path:
    """A fake $HOME with a populated claude-code session store."""
    home = tmp_path / "src-home"
    proj = home / ".claude" / "projects" / "-Users-alice-proj"
    proj.mkdir(parents=True)
    (proj / "sess-abc.jsonl").write_text(
        '{"role":"user","text":"hello"}\n{"role":"assistant","text":"hi"}\n'
    )
    mem = proj / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("# notes\n- a fact\n")
    todos = home / ".claude" / "todos"
    todos.mkdir(parents=True)
    (todos / "t1.json").write_text('{"todos":[]}')
    (home / ".claude.json").write_text('{"projects":{"/Users/alice/proj":{}}}')
    return home


def _export(composition, home: Path, out: Path, **extra):
    from axiom.extensions.builtins.memory.skills.export_bundle import (
        export_bundle,
    )

    return export_bundle({
        "composition": composition,
        "principal": PRINCIPAL,
        "out": str(out),
        "sessions_dir": str(home / "nonexistent-sessions"),
        "harness_stores": ["claude-code"],
        "home": str(home),
        **extra,
    }, None)


def _import(composition, bundle: Path, home: Path, **extra):
    from axiom.extensions.builtins.memory.skills.import_bundle import (
        import_bundle,
    )

    return import_bundle({
        "composition": composition,
        "bundle": str(bundle),
        "assume_principal": PRINCIPAL,
        "sessions_dir": str(home / "axi-sessions"),
        "home": str(home),
        **extra,
    }, None)


_STORE_MEMBER = "harness-store-claude-code.tar"


def _member_names(bundle: Path) -> dict[str, bytes]:
    with tarfile.open(bundle, "r:gz") as tf:
        return {
            m.name: tf.extractfile(m).read()
            for m in tf.getmembers() if m.isfile()
        }


class TestExport:
    def test_store_member_carries_relative_layout(
        self, composition, source_home, tmp_path,
    ):
        out = tmp_path / "b.tar.gz"
        result = _export(composition, source_home, out)
        assert result.ok, result.errors
        members = _member_names(out)
        assert _STORE_MEMBER in members
        inner = tarfile.open(fileobj=io.BytesIO(members[_STORE_MEMBER]))
        names = {m.name for m in inner.getmembers()}
        assert ".claude/projects/-Users-alice-proj/sess-abc.jsonl" in names
        assert ".claude/projects/-Users-alice-proj/memory/MEMORY.md" in names
        assert ".claude/todos/t1.json" in names
        assert ".claude.json" in names

    def test_member_is_hashed_and_counted_in_manifest(
        self, composition, source_home, tmp_path,
    ):
        out = tmp_path / "b.tar.gz"
        result = _export(composition, source_home, out)
        assert result.ok
        manifest = json.loads(_member_names(out)["manifest.json"])
        assert manifest["bundle_format_version"] >= 3
        assert _STORE_MEMBER in manifest["files"]
        assert manifest["counts"]["harness_stores"]["claude-code"] == 4
        assert result.value["counts"]["harness_stores"]["claude-code"] == 4

    def test_unknown_harness_store_is_refused(
        self, composition, source_home, tmp_path,
    ):
        out = tmp_path / "b.tar.gz"
        result = _export(
            composition, source_home, out, harness_stores=["mystery"],
        )
        assert not result.ok
        assert not out.exists()


class TestSecretGate:
    def test_secret_in_transcript_refuses_whole_export(
        self, composition, source_home, tmp_path,
    ):
        leaky = (
            source_home / ".claude" / "projects" / "-Users-alice-proj"
            / "sess-leak.jsonl"
        )
        leaky.write_text('{"text":"key is AKIAIOSFODNN7EXAMPLE"}\n')
        out = tmp_path / "b.tar.gz"
        result = _export(composition, source_home, out)
        assert not result.ok
        assert not out.exists()
        joined = " ".join(result.errors)
        assert "sess-leak.jsonl" in joined
        assert "aws-access-key-id" in joined
        assert "AKIA" not in joined  # findings never carry the secret

    def test_exclude_pattern_lets_a_cleaned_export_proceed(
        self, composition, source_home, tmp_path,
    ):
        leaky = (
            source_home / ".claude" / "projects" / "-Users-alice-proj"
            / "sess-leak.jsonl"
        )
        leaky.write_text('{"text":"key is AKIAIOSFODNN7EXAMPLE"}\n')
        out = tmp_path / "b.tar.gz"
        result = _export(
            composition, source_home, out,
            store_excludes=["*sess-leak*"],
        )
        assert result.ok, result.errors
        inner = tarfile.open(fileobj=io.BytesIO(
            _member_names(out)[_STORE_MEMBER]
        ))
        assert not any("sess-leak" in m.name for m in inner.getmembers())


class TestRestore:
    def test_round_trip_restores_files_byte_identical(
        self, composition, source_home, tmp_path,
    ):
        out = tmp_path / "b.tar.gz"
        assert _export(composition, source_home, out).ok
        dest = tmp_path / "dest-home"
        result = _import(composition, out, dest)
        assert result.ok, result.errors
        assert result.value["harness_stores"]["claude-code"]["restored"] == 4
        restored = (
            dest / ".claude" / "projects" / "-Users-alice-proj"
            / "sess-abc.jsonl"
        )
        original = (
            source_home / ".claude" / "projects" / "-Users-alice-proj"
            / "sess-abc.jsonl"
        )
        assert restored.read_bytes() == original.read_bytes()

    def test_existing_files_are_never_overwritten(
        self, composition, source_home, tmp_path,
    ):
        out = tmp_path / "b.tar.gz"
        assert _export(composition, source_home, out).ok
        dest = tmp_path / "dest-home"
        keep = dest / ".claude" / "todos" / "t1.json"
        keep.parent.mkdir(parents=True)
        keep.write_text('{"todos":["mine already"]}')
        result = _import(composition, out, dest)
        assert result.ok
        assert keep.read_text() == '{"todos":["mine already"]}'
        assert result.value["harness_stores"]["claude-code"]["skipped_existing"] == 1
        assert result.value["harness_stores"]["claude-code"]["restored"] == 3

    def test_dry_run_reports_without_writing(
        self, composition, source_home, tmp_path,
    ):
        out = tmp_path / "b.tar.gz"
        assert _export(composition, source_home, out).ok
        dest = tmp_path / "dest-home"
        result = _import(composition, out, dest, dry_run=True)
        assert result.ok
        assert result.value["harness_stores"]["claude-code"]["files"] == 4
        assert not dest.exists()


class TestPathTraversalGuard:
    def test_inner_tar_escaping_home_refuses_whole_bundle(
        self, composition, tmp_path,
    ):
        """A store member with '..' paths must refuse in phase 1."""
        from axiom.extensions.builtins.memory.skills.export_bundle import (
            canonical_json_bytes,
        )

        keypair = composition.signing_keypair
        evil_inner = io.BytesIO()
        with tarfile.open(fileobj=evil_inner, mode="w") as tf:
            blob = b"evil"
            info = tarfile.TarInfo(name="../outside/evil.txt")
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))
        evil_bytes = evil_inner.getvalue()

        import hashlib

        members = {
            "fragments.jsonl": b"",
            _STORE_MEMBER: evil_bytes,
        }
        manifest = {
            "bundle_format_version": 3,
            "principal": PRINCIPAL,
            "node_pubkey": keypair.public_bytes.hex(),
            "counts": {"fragments": 0, "harness_stores": {"claude-code": 1}},
            "fragment_hashes": {},
            "files": {
                name: hashlib.sha256(blob).hexdigest()
                for name, blob in members.items()
            },
        }
        manifest_bytes = json.dumps(manifest, indent=2).encode()
        sig = keypair.sign(canonical_json_bytes(manifest))
        bundle = tmp_path / "evil.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            for name, blob in (
                ("manifest.json", manifest_bytes),
                ("manifest.sig", sig.hex().encode("ascii")),
                *members.items(),
            ):
                info = tarfile.TarInfo(name=name)
                info.size = len(blob)
                tf.addfile(info, io.BytesIO(blob))

        dest = tmp_path / "dest-home"
        result = _import(composition, bundle, dest)
        assert not result.ok
        assert not (tmp_path / "outside").exists()
        assert not dest.exists()


class TestFragmentSecretGate:
    """Ledger fragments are scanned at export (gap found in live manual
    testing: absorbed harness memory can carry plaintext credentials)."""

    def _leaky_fragment(self, composition):
        return composition.write(
            content={"note": "ci token is glpat-" + "aB3x9zQ" * 4},
            cognitive_type="semantic",
            principal_id=PRINCIPAL, agents=set(), resources=set(),
        )

    def test_credential_fragment_refuses_whole_export(
        self, composition, tmp_path,
    ):
        frag = self._leaky_fragment(composition)
        out = tmp_path / "b.tar.gz"
        result = _export(
            composition, tmp_path / "empty-home", out, harness_stores=[],
        )
        assert not result.ok
        assert not out.exists()
        joined = " ".join(result.errors)
        assert frag.id in joined
        assert "gitlab-pat" in joined
        assert "glpat-" not in joined  # never the secret itself

    def test_skip_fragment_exports_without_it_and_counts_it(
        self, composition, tmp_path,
    ):
        frag = self._leaky_fragment(composition)
        composition.write(
            content={"fact": "clean"}, cognitive_type="semantic",
            principal_id=PRINCIPAL, agents=set(), resources=set(),
        )
        out = tmp_path / "b.tar.gz"
        result = _export(
            composition, tmp_path / "empty-home", out,
            harness_stores=[], skip_fragments=[frag.id],
        )
        assert result.ok, result.errors
        members = _member_names(out)
        assert frag.id not in members["fragments.jsonl"].decode()
        manifest = json.loads(members["manifest.json"])
        assert manifest["counts"]["fragments_flagged_skipped"] == 1
        assert manifest["counts"]["fragments"] == 1

    def test_clean_ledger_exports_untouched(self, composition, tmp_path):
        composition.write(
            content={"fact": "harmless"}, cognitive_type="semantic",
            principal_id=PRINCIPAL, agents=set(), resources=set(),
        )
        out = tmp_path / "b.tar.gz"
        result = _export(
            composition, tmp_path / "empty-home", out, harness_stores=[],
        )
        assert result.ok
        assert result.value["counts"]["fragments"] == 1
        assert result.value["counts"]["fragments_flagged_skipped"] == 0
