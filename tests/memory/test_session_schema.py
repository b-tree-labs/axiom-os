# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Spec-memory §3.3 + §3.7 schema tests.

Validates that ``Provenance.session_id`` flows through:
- the dataclass default
- ``create_fragment``
- ``to_dict`` / ``fragment_from_dict`` round-trip
- legacy decoders (v1 + v2 with missing session_id fall back to "")

Implementation lives in ``axiom.memory.fragment``.
"""

from __future__ import annotations

import pytest

from axiom.memory.fragment import (
    Provenance,
    create_fragment,
    fragment_from_dict,
)


class TestProvenanceSessionField:
    """Spec-memory §3.3: Provenance grows from (T,U,A,R) to (T,U,A,R,S)."""

    def test_default_session_id_is_empty(self):
        """Legacy provenance construction without session_id defaults to ""."""
        prov = Provenance(timestamp="2026-05-18T10:00:00+00:00", principal_id="@u:x")
        assert prov.session_id == ""

    def test_explicit_session_id_round_trips(self):
        prov = Provenance(
            timestamp="2026-05-18T10:00:00+00:00",
            principal_id="@u:x",
            session_id="session://abc-123",
        )
        assert prov.session_id == "session://abc-123"

    def test_provenance_is_still_frozen(self):
        """Schema extension preserves immutability invariant."""
        prov = Provenance(
            timestamp="2026-05-18T10:00:00+00:00",
            principal_id="@u:x",
            session_id="session://abc-123",
        )
        with pytest.raises(Exception):
            prov.session_id = "session://other"  # type: ignore[misc]


class TestCreateFragmentWithSession:
    def test_create_fragment_accepts_session_id(self):
        frag = create_fragment(
            content={"event_time": "2026-05-18T10:00:00+00:00"},
            cognitive_type="episodic",
            principal_id="@u:x",
            agents={"axi-chat:opus-4-7"},
            resources=set(),
            session_id="session://abc-123",
        )
        assert frag.provenance.session_id == "session://abc-123"

    def test_create_fragment_defaults_session_id_to_empty(self):
        frag = create_fragment(
            content={"event_time": "2026-05-18T10:00:00+00:00"},
            cognitive_type="episodic",
            principal_id="@u:x",
            agents=set(),
            resources=set(),
        )
        assert frag.provenance.session_id == ""


class TestSerializationRoundTrip:
    def test_to_dict_includes_session_id(self):
        frag = create_fragment(
            content={"event_time": "2026-05-18T10:00:00+00:00"},
            cognitive_type="episodic",
            principal_id="@u:x",
            agents=set(),
            resources=set(),
            session_id="session://abc-123",
        )
        d = frag.to_dict()
        assert d["provenance"]["session_id"] == "session://abc-123"

    def test_round_trip_preserves_session_id(self):
        frag = create_fragment(
            content={"event_time": "2026-05-18T10:00:00+00:00"},
            cognitive_type="episodic",
            principal_id="@u:x",
            agents=set(),
            resources=set(),
            session_id="session://xyz-789",
        )
        rebuilt = fragment_from_dict(frag.to_dict())
        assert rebuilt.provenance.session_id == "session://xyz-789"


class TestBackwardsCompatibility:
    """Spec-memory §3.7 + §3.3: legacy fragments without session_id
    decode as session_id == "" (interpreted by read paths as
    'cross-session, no migration required')."""

    def _legacy_v1_dict(self) -> dict:
        return {
            "id": "frag-legacy-v1",
            "cognitive_type": "episodic",
            "content": {"event_time": "2025-12-01T00:00:00+00:00"},
            "provenance": {
                "timestamp": "2025-12-01T00:00:00+00:00",
                "principal_id": "@u:legacy",
                "agents": [],
                "resources": [],
                # No session_id, no accountable_human_id, no delegation_chain.
            },
            "schema_version": 1,
        }

    def _legacy_v2_dict(self) -> dict:
        return {
            "id": "frag-legacy-v2",
            "cognitive_type": "episodic",
            "content": {"event_time": "2026-01-01T00:00:00+00:00"},
            "provenance": {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "principal_id": "@u:legacy",
                "agents": [],
                "resources": [],
                "accountable_human_id": "@u:legacy",
                "delegation_chain": [],
                # No session_id.
            },
            "schema_version": 2,
        }

    def test_v1_legacy_fragment_decodes_with_empty_session(self):
        frag = fragment_from_dict(self._legacy_v1_dict())
        assert frag.provenance.session_id == ""

    def test_v2_legacy_fragment_decodes_with_empty_session(self):
        frag = fragment_from_dict(self._legacy_v2_dict())
        assert frag.provenance.session_id == ""

    def test_v2_with_session_id_round_trips(self):
        data = self._legacy_v2_dict()
        data["provenance"]["session_id"] = "session://abc-123"
        frag = fragment_from_dict(data)
        assert frag.provenance.session_id == "session://abc-123"


def _service(tmp_path):
    """Mirror the test_composition.py fixture so this file's session
    tests don't take a dependency on that file's private helper."""
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph

    reg = ArtifactRegistry(backend=SQLiteBackend(tmp_path / "artifacts.db"))
    audit = AuditLog(tmp_path / "audit.jsonl", signing_keypair=None)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=None,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
        transform=None,
    )


class TestCompositionWriteThreadsSession:
    """Spec-memory §8: CompositionService.write threads session_id into
    the persisted fragment's provenance."""

    def test_explicit_session_id_lands_in_provenance(self, tmp_path):
        svc = _service(tmp_path)
        frag = svc.write(
            content={"event_time": "2026-05-18T10:00:00+00:00"},
            cognitive_type="episodic",
            principal_id="@u:write-test",
            agents={"axi"},
            resources=set(),
            accountable_human_id="@u:write-test",
            session_id="session://explicit-write",
        )
        assert frag.provenance.session_id == "session://explicit-write"

    def test_omitted_session_id_defaults_to_empty_in_test_context(self, tmp_path):
        """In the pytest context, auto-resolution is disabled so fragments
        written without an explicit session_id carry the empty sentinel
        (spec-memory §3.7 backwards-compatibility path). Production
        callers running outside pytest auto-resolve the active session."""
        svc = _service(tmp_path)
        frag = svc.write(
            content={"event_time": "2026-05-18T10:00:00+00:00"},
            cognitive_type="episodic",
            principal_id="@u:write-test",
            agents={"axi"},
            resources=set(),
            accountable_human_id="@u:write-test",
        )
        assert frag.provenance.session_id == ""
