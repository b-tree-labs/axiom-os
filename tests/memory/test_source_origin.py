# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for ADR-087 D1 — SourceOrigin origin coordinate (schema v3).

Every memory has an origin coordinate ``(harness, account)``. Provenance
gains a write-once ``SourceOrigin`` record (harness, account, source_ref,
imported_at); ``(harness, account, source_ref)`` is the idempotency key
for dedup and sync. Fragments without one decode as native origin via
the established versioned-decoder pattern.

Byte-compatibility constraint: pre-v3 fragments were signed over their
v2 ``to_dict()`` canonical bytes. Decoding a v2 payload and re-encoding
it must reproduce the original payload exactly (no injected ``origin``
key), or every existing signature breaks.
"""

from __future__ import annotations

import dataclasses

import pytest


def _base_payload(version: int) -> dict:
    """A representative persisted fragment payload at the given version.

    Built from the real serializer (not hand-written) so field shapes —
    classification, visibility, ownership — always match what the code
    actually persisted at earlier versions.
    """
    from axiom.memory.fragment import create_fragment

    frag = create_fragment(
        content={"fact": "water boils at 100C"},
        cognitive_type="semantic",
        principal_id="@alice:home",
        agents={"axi"},
        resources={"rag-org"},
    )
    payload = frag.to_dict()
    payload["id"] = "frag-1"
    payload["provenance"]["timestamp"] = "2026-07-01T00:00:00+00:00"
    payload["provenance"]["accountable_human_id"] = "@alice:home"
    payload["provenance"].pop("origin", None)  # pre-v3 payloads never had it
    payload["schema_version"] = version
    if version == 1:
        prov = payload["provenance"]
        del prov["accountable_human_id"]
        del prov["delegation_chain"]
    return payload


class TestSourceOriginRecord:
    def test_fields_and_frozen(self):
        from axiom.memory.fragment import SourceOrigin

        origin = SourceOrigin(
            harness="claude-code",
            account="acct-personal",
            source_ref="frag-1",
            imported_at="2026-07-10T00:00:00+00:00",
        )
        assert origin.harness == "claude-code"
        assert origin.account == "acct-personal"
        assert origin.source_ref == "frag-1"
        assert origin.imported_at == "2026-07-10T00:00:00+00:00"
        with pytest.raises(Exception):  # noqa: B017  # FrozenInstanceError
            origin.harness = "other"

    def test_idempotency_key(self):
        from axiom.memory.fragment import SourceOrigin

        origin = SourceOrigin(
            harness="claude-code", account="a", source_ref="r",
            imported_at="2026-07-10T00:00:00+00:00",
        )
        # imported_at is deliberately NOT part of the key — the same
        # source fragment re-imported later must collide.
        assert origin.idempotency_key == ("claude-code", "a", "r")


class TestSchemaV3:
    def test_current_schema_version_is_3(self):
        from axiom.memory.fragment import CURRENT_SCHEMA_VERSION

        assert CURRENT_SCHEMA_VERSION == 3

    def test_new_fragments_are_native_v3(self):
        from axiom.memory.fragment import create_fragment

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        assert frag.schema_version == 3
        assert frag.provenance.origin is None  # native

    def test_native_to_dict_omits_origin_key(self):
        from axiom.memory.fragment import create_fragment

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        assert "origin" not in frag.to_dict()["provenance"]

    def test_origin_round_trip(self):
        from axiom.memory.fragment import (
            SourceOrigin,
            create_fragment,
            fragment_from_dict,
        )

        frag = create_fragment(
            content={"x": 1}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        origin = SourceOrigin(
            harness="codex", account="acct-b", source_ref="src-9",
            imported_at="2026-07-10T12:00:00+00:00",
        )
        frag = dataclasses.replace(
            frag,
            provenance=dataclasses.replace(frag.provenance, origin=origin),
        )
        data = frag.to_dict()
        assert data["provenance"]["origin"] == {
            "harness": "codex",
            "account": "acct-b",
            "source_ref": "src-9",
            "imported_at": "2026-07-10T12:00:00+00:00",
        }
        restored = fragment_from_dict(data)
        assert restored.provenance.origin == origin
        assert restored.schema_version == 3

    def test_v3_payload_without_origin_decodes_native(self):
        from axiom.memory.fragment import fragment_from_dict

        frag = fragment_from_dict(_base_payload(3))
        assert frag.schema_version == 3
        assert frag.provenance.origin is None


class TestLegacyDecodersNativeOrigin:
    def test_v1_decodes_native(self):
        from axiom.memory.fragment import fragment_from_dict

        frag = fragment_from_dict(_base_payload(1))
        assert frag.schema_version == 1
        assert frag.provenance.origin is None

    def test_v2_decodes_native(self):
        from axiom.memory.fragment import fragment_from_dict

        frag = fragment_from_dict(_base_payload(2))
        assert frag.schema_version == 2
        assert frag.provenance.origin is None

    def test_v2_reencode_is_byte_identical(self):
        """Decode→re-encode of a v2 payload reproduces it exactly, so
        signatures computed under the v2 encoding still verify."""
        from axiom.memory.fragment import fragment_from_dict

        payload = _base_payload(2)
        assert fragment_from_dict(payload).to_dict() == payload

    def test_future_version_fails_closed(self):
        from axiom.memory.exceptions import UnsupportedSchemaError
        from axiom.memory.fragment import fragment_from_dict

        with pytest.raises(UnsupportedSchemaError):
            fragment_from_dict(_base_payload(4))
