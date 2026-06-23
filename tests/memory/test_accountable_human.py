# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ADR-035 — human-principal binding (accountable_human_id).

Schema bump v1 → v2 + CompositionService write-time validation +
backfill migration helper. See:

- docs/adrs/adr-035-human-principal-binding.md (D1, D2, D7, D8)
- docs/working/memory-persistence-plan.md §3 + §4 + §5
- docs/specs/spec-memory.md §3.3 (Provenance contract)
"""

from __future__ import annotations

import pytest

from axiom.artifacts.registry import ArtifactRegistry, InMemoryBackend
from axiom.memory.access import AccessGraphs
from axiom.memory.attest import AuditLog
from axiom.memory.composition import CompositionService
from axiom.memory.exceptions import AccountabilityError
from axiom.memory.policy import PolicyCoord
from axiom.memory.trust import TrustGraph

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cs(tmp_path):
    return CompositionService(
        artifact_registry=ArtifactRegistry(backend=InMemoryBackend()),
        audit_log=AuditLog(tmp_path / "audit.jsonl", signing_keypair=None),
        signing_keypair=None,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


def _semantic_kwargs(**overrides):
    """Default semantic-write kwargs; tests override what they care about."""
    base = dict(
        content={"x": 1},
        cognitive_type="semantic",
        principal_id="@ben:example-org",
        agents=set(),
        resources=set(),
        accountable_human_id="@ben:example-org",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Provenance shape — accountable_human_id field exists
# ---------------------------------------------------------------------------


class TestProvenanceShape:
    def test_provenance_carries_accountable_human(self):
        from axiom.memory.fragment import Provenance

        prov = Provenance(
            timestamp="2026-04-27T00:00:00+00:00",
            principal_id="@ben:example-org",
            accountable_human_id="@ben:example-org",
        )
        assert prov.accountable_human_id == "@ben:example-org"
        assert prov.delegation_chain == ()

    def test_provenance_construction_with_empty_string_is_allowed_at_type_level(self):
        """Type-level construction permits empty string; CompositionService is
        the enforcement seam at write time (ADR-035 §D1)."""
        from axiom.memory.fragment import Provenance

        prov = Provenance(
            timestamp="2026-04-27T00:00:00+00:00",
            principal_id="@ben:example-org",
            accountable_human_id="",
        )
        # Construction succeeds; rejection happens at CompositionService.write.
        assert prov.accountable_human_id == ""


# ---------------------------------------------------------------------------
# 2. Schema version bump — every new write produces schema_version=2
# ---------------------------------------------------------------------------


class TestSchemaVersionBump:
    def test_new_fragment_has_schema_version_2(self, cs):
        frag = cs.write(**_semantic_kwargs())
        assert frag.schema_version == 2

    def test_serialized_fragment_includes_schema_version_2(self, cs):
        frag = cs.write(**_semantic_kwargs())
        data = frag.to_dict()
        assert data["schema_version"] == 2

    def test_provenance_serialization_includes_accountable_fields(self, cs):
        frag = cs.write(**_semantic_kwargs())
        data = frag.to_dict()
        assert data["provenance"]["accountable_human_id"] == "@ben:example-org"
        assert data["provenance"]["delegation_chain"] == []


# ---------------------------------------------------------------------------
# 3. v1 decoder — legacy fragments decode with sentinel
# ---------------------------------------------------------------------------


class TestV1Decoder:
    def test_v1_dict_without_schema_version_decodes_with_legacy_sentinel(self):
        """A frozen v1 fragment dict (pre-bump shape, no schema_version key,
        no accountable_human_id) decodes successfully with the legacy
        sentinel."""
        from axiom.memory.fragment import fragment_from_dict

        v1_payload = {
            "id": "abc123",
            "cognitive_type": "semantic",
            "content": {"fact": "fission splits heavy nuclei"},
            "provenance": {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "principal_id": "@alice:demo",
                "agents": [],
                "resources": [],
            },
            "retention_tier": "active",
            "ttl": None,
            "effectiveness_score": None,
            "valid_time_start": None,
            "valid_time_end": None,
            "policy_coord": None,
            "signature": None,
            "ownership": None,
            "visibility": "scope_internal",
            "classification": {"level": "unclassified"},
        }
        decoded = fragment_from_dict(v1_payload)
        assert decoded.provenance.accountable_human_id == "legacy:unattributed"
        assert decoded.provenance.delegation_chain == ()

    def test_explicit_schema_version_1_uses_v1_decoder(self):
        from axiom.memory.fragment import fragment_from_dict

        v1_payload = {
            "id": "abc123",
            "cognitive_type": "semantic",
            "content": {"fact": "x"},
            "provenance": {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "principal_id": "@alice:demo",
                "agents": [],
                "resources": [],
            },
            "schema_version": 1,
            "retention_tier": "active",
            "visibility": "scope_internal",
            "classification": {"level": "unclassified"},
        }
        decoded = fragment_from_dict(v1_payload)
        assert decoded.provenance.accountable_human_id == "legacy:unattributed"


# ---------------------------------------------------------------------------
# 4. v2 decoder — accountable_human_id present + round-trip stable
# ---------------------------------------------------------------------------


class TestV2Decoder:
    def test_v2_dict_with_accountable_human_decodes(self):
        from axiom.memory.fragment import fragment_from_dict

        v2_payload = {
            "id": "abc123",
            "cognitive_type": "semantic",
            "content": {"fact": "x"},
            "provenance": {
                "timestamp": "2026-04-27T00:00:00+00:00",
                "principal_id": "@ben:example-org",
                "agents": [],
                "resources": [],
                "accountable_human_id": "@ben:example-org",
                "delegation_chain": [],
            },
            "schema_version": 2,
            "retention_tier": "active",
            "visibility": "scope_internal",
            "classification": {"level": "unclassified"},
        }
        decoded = fragment_from_dict(v2_payload)
        assert decoded.schema_version == 2
        assert decoded.provenance.accountable_human_id == "@ben:example-org"

    def test_v2_dict_missing_accountable_human_falls_back_to_legacy_default(self):
        """Per ADR-035 §D7: pre-bump fragments default to ``legacy:unattributed``
        for read-back compatibility. A v2-tagged but missing-field payload
        is treated as malformed-but-recoverable: it decodes with the legacy
        sentinel + the audit projection flags it. The CompositionService
        forbids these from being written; this is read-back tolerance only."""
        from axiom.memory.fragment import fragment_from_dict

        broken_v2 = {
            "id": "abc",
            "cognitive_type": "semantic",
            "content": {"fact": "x"},
            "provenance": {
                "timestamp": "2026-04-27T00:00:00+00:00",
                "principal_id": "@ben:example-org",
                "agents": [],
                "resources": [],
                # accountable_human_id absent — treat as legacy
            },
            "schema_version": 2,
            "retention_tier": "active",
            "visibility": "scope_internal",
            "classification": {"level": "unclassified"},
        }
        decoded = fragment_from_dict(broken_v2)
        assert decoded.provenance.accountable_human_id == "legacy:unattributed"

    def test_unsupported_schema_version_raises(self):
        from axiom.memory.fragment import (
            UnsupportedSchemaError,
            fragment_from_dict,
        )

        future_payload = {
            "id": "abc",
            "cognitive_type": "semantic",
            "content": {"fact": "x"},
            "provenance": {
                "timestamp": "2026-04-27T00:00:00+00:00",
                "principal_id": "@ben:example-org",
                "agents": [],
                "resources": [],
                "accountable_human_id": "@ben:example-org",
                "delegation_chain": [],
            },
            "schema_version": 99,
        }
        with pytest.raises(UnsupportedSchemaError):
            fragment_from_dict(future_payload)


# ---------------------------------------------------------------------------
# 5. CompositionService rejection — empty accountable_human_id
# ---------------------------------------------------------------------------


class TestCompositionServiceRejection:
    def test_write_with_empty_accountable_human_raises(self, cs):
        with pytest.raises(AccountabilityError, match="missing accountable_human_id"):
            cs.write(**_semantic_kwargs(accountable_human_id=""))

    def test_write_with_unset_falls_back_to_principal_id(self, cs):
        """Per ADR-035 §D1: when accountable_human_id is not explicitly
        supplied, the actor IS the human (human-acts-directly default).
        The bind still fires — the resolved value is validated — so an
        empty principal_id would still raise. Extension-level sweeps
        to set the field explicitly remain a tracked follow-on per
        ADR-035 §D8."""
        kwargs = _semantic_kwargs()
        kwargs.pop("accountable_human_id")
        kwargs["principal_id"] = "@ben:example-org"
        frag = cs.write(**kwargs)
        assert frag.provenance.accountable_human_id == "@ben:example-org"

    def test_write_with_legacy_sentinel_raises(self, cs):
        with pytest.raises(AccountabilityError):
            cs.write(**_semantic_kwargs(accountable_human_id="legacy:unattributed"))

    def test_write_with_legacy_prefix_raises(self, cs):
        with pytest.raises(AccountabilityError):
            cs.write(**_semantic_kwargs(accountable_human_id="legacy:foo"))


# ---------------------------------------------------------------------------
# 6. CompositionService accept — real principal succeeds
# ---------------------------------------------------------------------------


class TestCompositionServiceAccept:
    def test_write_with_real_principal_succeeds(self, cs):
        frag = cs.write(**_semantic_kwargs(accountable_human_id="@ben:example-org"))
        assert frag.provenance.accountable_human_id == "@ben:example-org"

    def test_write_with_human_email_principal_succeeds(self, cs):
        frag = cs.write(**_semantic_kwargs(accountable_human_id="ben@example.org"))
        assert frag.provenance.accountable_human_id == "ben@example.org"


# ---------------------------------------------------------------------------
# 7. Delegation chain — agent acting under a human
# ---------------------------------------------------------------------------


class TestDelegationChain:
    def test_agent_under_human_round_trips(self, cs):
        """An agent acting under a human writes a fragment with
        principal_id=agent, accountable_human_id=human, delegation_chain=[
        human, agent]. Round-trips through to_dict/from_dict cleanly."""
        from axiom.memory.fragment import fragment_from_dict

        frag = cs.write(
            **_semantic_kwargs(
                principal_id="agent:axi",
                accountable_human_id="@ben:example-org",
                delegation_chain=("@ben:example-org", "agent:axi"),
            )
        )
        assert frag.provenance.principal_id == "agent:axi"
        assert frag.provenance.accountable_human_id == "@ben:example-org"
        assert frag.provenance.delegation_chain == ("@ben:example-org", "agent:axi")

        decoded = fragment_from_dict(frag.to_dict())
        assert decoded.provenance.principal_id == "agent:axi"
        assert decoded.provenance.accountable_human_id == "@ben:example-org"
        assert decoded.provenance.delegation_chain == (
            "@ben:example-org", "agent:axi",
        )


# ---------------------------------------------------------------------------
# 9 + 10 + 11. Migration helper
# ---------------------------------------------------------------------------


def _seed_v1_fragment(cs, *, principal_id, scope, content_fact="x"):
    """Helper: seed a v1-shaped fragment directly into the registry,
    bypassing CompositionService (which would now reject it as v2)."""
    import uuid

    v1_data = {
        "id": uuid.uuid4().hex,
        "cognitive_type": "semantic",
        "content": {"fact": content_fact, "scope": scope},
        "provenance": {
            "timestamp": "2026-04-01T00:00:00+00:00",
            "principal_id": principal_id,
            "agents": [],
            "resources": [],
        },
        "schema_version": 1,
        "retention_tier": "active",
        "ttl": None,
        "visibility": "scope_internal",
        "classification": {"level": "unclassified"},
    }
    cs.artifact_registry.register(
        kind="fragment", name=v1_data["id"], data=v1_data,
    )
    return v1_data["id"]


class TestMigrationDryRun:
    def test_dry_run_reports_counts_without_writing(self, cs, tmp_path):

        ids = [
            _seed_v1_fragment(cs, principal_id="@ben:example-org", scope="ne101"),
            _seed_v1_fragment(cs, principal_id="@alice:example-org", scope="ne101"),
        ]
        before = list(cs.artifact_registry.list(kind="fragment"))

        # Drive migration via the CLI helper (dry-run path).
        from axiom.extensions.builtins.memory.cli import (
            backfill_accountable_human,
        )

        report = backfill_accountable_human(
            composition=cs, scope_id="ne101", dry_run=True,
        )

        # No new artifacts written.
        after = list(cs.artifact_registry.list(kind="fragment"))
        assert len(after) == len(before)

        # All originals still active.
        for fid in ids:
            assert any(a.name == fid and not a.deleted for a in after)

        assert report["scanned"] == 2
        assert report["would_migrate"] == 2
        assert report["written"] == 0


class TestMigrationLive:
    def test_live_migration_creates_v2_and_tombstones_v1(self, cs):
        from axiom.extensions.builtins.memory.cli import (
            backfill_accountable_human,
        )

        v1_id = _seed_v1_fragment(cs, principal_id="@ben:example-org", scope="ne101")

        report = backfill_accountable_human(
            composition=cs, scope_id="ne101", dry_run=False,
        )
        assert report["written"] == 1
        assert report["ambiguous"] == 0

        # Original is tombstoned with reason="migrated_to_v2".
        chain = cs.artifact_registry.version_chain(kind="fragment", name=v1_id)
        assert len(chain) == 1
        assert chain[0].deleted
        assert chain[0].deletion_reason == "migrated_to_v2"

        # A new v2 fragment exists, supersedes original.
        all_active = cs.artifact_registry.list(kind="fragment")
        v2_frags = [
            a for a in all_active
            if a.data.get("schema_version") == 2
            and a.data["provenance"]["principal_id"] == "@ben:example-org"
        ]
        assert len(v2_frags) == 1
        assert (
            v2_frags[0].data["provenance"]["accountable_human_id"]
            == "@ben:example-org"
        )


class TestMigrationAmbiguity:
    def test_agent_principal_without_default_human_flags_ambiguous(self, cs):
        """An agent-principal fragment with no inferrable human and no
        --default-human results in a ProvenanceAmbiguous event; the
        original is NOT tombstoned."""
        from axiom.extensions.builtins.memory.cli import (
            backfill_accountable_human,
        )

        v1_id = _seed_v1_fragment(
            cs, principal_id="agent:axi", scope="ne101",
        )

        report = backfill_accountable_human(
            composition=cs, scope_id="ne101", dry_run=False,
        )
        assert report["ambiguous"] == 1
        assert report["written"] == 0

        # Original is NOT tombstoned.
        chain = cs.artifact_registry.version_chain(kind="fragment", name=v1_id)
        assert len(chain) == 1
        assert not chain[0].deleted

        # A ProvenanceAmbiguous audit event was recorded.
        ambiguous_events = [
            e for e in cs.audit_log.read_all()
            if e.get("entry_type") == "ProvenanceAmbiguous"
        ]
        assert len(ambiguous_events) == 1
        assert ambiguous_events[0]["fragment_id"] == v1_id

    def test_agent_principal_with_default_human_succeeds(self, cs):
        from axiom.extensions.builtins.memory.cli import (
            backfill_accountable_human,
        )

        v1_id = _seed_v1_fragment(
            cs, principal_id="agent:axi", scope="ne101",
        )

        report = backfill_accountable_human(
            composition=cs,
            scope_id="ne101",
            dry_run=False,
            default_human="@ben:example-org",
        )
        assert report["written"] == 1
        assert report["ambiguous"] == 0

        chain = cs.artifact_registry.version_chain(kind="fragment", name=v1_id)
        assert chain[0].deleted

        all_active = cs.artifact_registry.list(kind="fragment")
        v2_frags = [
            a for a in all_active
            if a.data.get("schema_version") == 2
        ]
        assert len(v2_frags) == 1
        v2 = v2_frags[0].data["provenance"]
        assert v2["accountable_human_id"] == "@ben:example-org"
        # Delegation chain captures human → agent.
        assert v2["delegation_chain"] == ["@ben:example-org", "agent:axi"]


# ---------------------------------------------------------------------------
# CLI smoke — argparse path
# ---------------------------------------------------------------------------


class TestMigrationCLI:
    def test_cli_dry_run_succeeds(self, cs, monkeypatch, capsys):
        """``axi memory migrate --backfill-accountable-human <scope> --dry-run``
        runs cleanly and reports counts."""
        from axiom.extensions.builtins.memory import cli as memory_cli

        _seed_v1_fragment(cs, principal_id="@ben:example-org", scope="ne101")

        # Inject the test composition rather than the default runtime one.
        monkeypatch.setattr(
            memory_cli, "_build_default_composition", lambda: cs,
        )

        rc = memory_cli.main(
            [
                "migrate",
                "--backfill-accountable-human",
                "ne101",
                "--dry-run",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "scanned" in out.lower() or "1" in out
