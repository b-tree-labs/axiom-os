# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""cross-mem P0 rehearsal — the full acceptance gate in one flow.

Two compositions with independent node keys and state trees on one
machine simulate a personal→work account port (ADR-087 D9/D10 P0):

    export(@alice:personal) → import --assume-principal @alice:work

Gate properties asserted here, in one scenario:
recall parity · audit-chain continuity · zero loss · idempotency ·
fail-closed authorization negatives · vault never leaves the source.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

PERSONAL = "@alice:personal"
WORK = "@alice:work"
AGENT = "axi"


def _make_composition(base: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base.mkdir(parents=True, exist_ok=True)
    return CompositionService(
        artifact_registry=ArtifactRegistry(
            backend=SQLiteBackend(base / "artifacts.db")
        ),
        audit_log=AuditLog(
            base / "audit.jsonl", signing_keypair=(kp := generate_keypair())
        ),
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


@pytest.fixture
def rehearsal(tmp_path: Path):
    """personal node (seeded) + work node (empty) + a fresh bundle."""
    from axiom.extensions.builtins.memory.skills.export_bundle import (
        export_bundle,
    )

    personal = _make_composition(tmp_path / "personal")
    work = _make_composition(tmp_path / "work")

    personal.write(
        content={"fact": "prefers TDD"}, cognitive_type="semantic",
        principal_id=PERSONAL, agents={AGENT}, resources=set(),
    )
    personal.write(
        content={"note": "sprint recap",
                 "event_time": "2026-07-01T09:00:00+00:00"},
        cognitive_type="episodic",
        principal_id=PERSONAL, agents={AGENT}, resources=set(),
    )
    personal.write(
        content={"secret": "TOKEN=hunter2"}, cognitive_type="vault",
        principal_id=PERSONAL, agents=set(), resources=set(),
    )

    sessions_src = tmp_path / "personal-sessions"
    sessions_src.mkdir()
    (sessions_src / "s1.json").write_text(json.dumps({
        "session_id": "session://rehearsal-1", "principal_id": PERSONAL,
        "name": "personal-rehearsal",
    }))

    bundle = tmp_path / "alice.tar.gz"
    result = export_bundle({
        "composition": personal,
        "principal": PERSONAL,
        "out": str(bundle),
        "sessions_dir": str(sessions_src),
    }, None)
    assert result.ok, result.errors
    return personal, work, bundle, tmp_path


def _import(work, bundle, sessions_dir, **overrides):
    from axiom.extensions.builtins.memory.skills.import_bundle import (
        import_bundle,
    )

    params = {
        "composition": work,
        "bundle": str(bundle),
        "assume_principal": WORK,
        "sessions_dir": str(sessions_dir),
    }
    params.update(overrides)
    return import_bundle(params, None)


def _non_vault_probe(comp, principal):
    """The recall-parity probe: (id, content) pairs for a principal."""
    return sorted(
        (a.name, json.dumps(a.data.get("content"), sort_keys=True))
        for a in comp.artifact_registry.list(kind="fragment")
        if (a.data or {}).get("provenance", {}).get("principal_id") == principal
        and (a.data or {}).get("cognitive_type") != "vault"
    )


def test_p0_rehearsal_full_gate(rehearsal):
    from axiom.memory.attest import verify_fragment_signature
    from axiom.memory.fragment import fragment_from_dict

    personal, work, bundle, tmp = rehearsal
    dst_sessions = tmp / "work-sessions"

    result = _import(work, bundle, dst_sessions)
    assert result.ok, result.errors
    assert result.value["imported"] == 2

    # --- Zero loss: manifest ids all present, content identical ------------
    with tarfile.open(bundle, "r:gz") as tf:
        members = {m.name: tf.extractfile(m).read() for m in tf.getmembers()}
    manifest = json.loads(members["manifest.json"])
    bundle_content = {
        (p := json.loads(line))["id"]: p["content"]
        for line in members["fragments.jsonl"].splitlines()
    }
    dest = {
        a.name: a.data
        for a in work.artifact_registry.list(kind="fragment")
    }
    assert set(dest) == set(manifest["fragment_hashes"]) == set(bundle_content)
    for fid, content in bundle_content.items():
        assert dest[fid]["content"] == content

    # --- Recall parity: same probe, same fragments --------------------------
    assert _non_vault_probe(personal, PERSONAL) == _non_vault_probe(
        work, PERSONAL
    )

    # --- Vault never leaves the source --------------------------------------
    for blob in members.values():
        assert b"hunter2" not in blob
    assert all(d.get("cognitive_type") != "vault" for d in dest.values())
    # ...and it is still recallable at the source.
    src_types = {
        (a.data or {}).get("cognitive_type")
        for a in personal.artifact_registry.list(kind="fragment")
    }
    assert "vault" in src_types

    # --- Re-home ceremony results -------------------------------------------
    for data in dest.values():
        frag = fragment_from_dict(data)
        assert frag.ownership.master == WORK
        assert frag.ownership.delegations == ()
        assert frag.provenance.principal_id == PERSONAL  # untouched
        assert frag.provenance.accountable_human_id == PERSONAL
        assert frag.provenance.origin.idempotency_key == (
            "axiom", PERSONAL, frag.id,
        )
        # Re-signed: destination key verifies, source key does not.
        assert verify_fragment_signature(
            frag, work.signing_keypair.public_bytes
        )
        assert not verify_fragment_signature(
            frag, personal.signing_keypair.public_bytes
        )

    # --- The destination read path accepts the re-signed fragments ----------
    from axiom.memory.access import (
        add_agent_resource_edge,
        add_user_agent_edge,
    )

    work.access_graphs = add_user_agent_edge(work.access_graphs, WORK, AGENT)
    fragment_ids = sorted(dest)
    for data in dest.values():
        for res in data["provenance"]["resources"]:
            work.access_graphs = add_agent_resource_edge(
                work.access_graphs, AGENT, res
            )
    got = work.read(fragment_ids, user=WORK, agent=AGENT)
    assert sorted(f.id for f in got) == fragment_ids

    # --- Audit-chain continuity ---------------------------------------------
    assert len(list(personal.audit_log.query(entry_type="export"))) == 1
    imports = list(work.audit_log.query(entry_type="import"))
    assert len(imports) == 1 and imports[0]["from_principal"] == PERSONAL
    rehomes = list(work.audit_log.query(entry_type="re_home"))
    assert sorted(e["fragment_id"] for e in rehomes) == fragment_ids

    # --- Session checkpoint restored -----------------------------------------
    assert json.loads((dst_sessions / "s1.json").read_text())[
        "session_id"
    ] == "session://rehearsal-1"

    # --- Idempotency: replaying the bundle is a no-op ------------------------
    before = {
        a.name: json.dumps(a.data, sort_keys=True)
        for a in work.artifact_registry.list(kind="fragment")
    }
    replay = _import(work, bundle, dst_sessions)
    assert replay.ok
    assert replay.value["imported"] == 0
    assert replay.value["skipped_duplicate"] == 2
    after = {
        a.name: json.dumps(a.data, sort_keys=True)
        for a in work.artifact_registry.list(kind="fragment")
    }
    assert before == after


def test_p0_rehearsal_fail_closed_negative(rehearsal):
    """A bundle whose consent signature cannot be established imports
    nothing — the authorization negative of the dual-signature ceremony."""
    personal, work, bundle, tmp = rehearsal

    with tarfile.open(bundle, "r:gz") as tf:
        members = {m.name: tf.extractfile(m).read() for m in tf.getmembers()}
    # Forge: re-point the manifest at a different signer without a
    # matching signature.
    manifest = json.loads(members["manifest.json"])
    manifest["node_pubkey"] = "ab" * 32
    members["manifest.json"] = json.dumps(manifest, indent=2).encode()
    with tarfile.open(bundle, "w:gz") as tf:
        for name, blob in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))

    result = _import(work, bundle, tmp / "work-sessions")
    assert not result.ok
    assert list(work.artifact_registry.list(kind="fragment")) == []
    denied = list(work.audit_log.query(entry_type="import_denied"))
    assert len(denied) == 1 and denied[0]["outcome"] == "signature_invalid"
