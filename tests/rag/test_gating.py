# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for axiom/rag/gating.py — retrieval-time classification gate.

Generic primitive: filter chunks by classification tag + principal
attestation + signature verification. Any extension uses it:
- Classroom: EC classification × nationality attestation
- Healthcare: PHI classification × clearance attestation
- DoD: CUI classification × citizenship attestation
- Any domain: <tag> classification × <attribute_key> attestation

Not coupled to any specific tag, attribute, or domain.
"""

from __future__ import annotations


class TestUnclassifiedPasses:
    def test_chunks_without_classification_always_allowed(self):
        from axiom.rag.gating import filter_chunks_by_classification

        chunks = [
            {"id": "c1", "text": "public stuff"},
            {"id": "c2", "text": "more public"},
        ]
        attestation = {"principal_id": "u1", "attributes": {"nationality": "US"}}
        allowed, denied = filter_chunks_by_classification(
            chunks, attestation, verify_signature=lambda a: True
        )
        assert len(allowed) == 2
        assert denied == []


class TestClassifiedChunkAllowed:
    def test_matching_attribute_passes(self):
        from axiom.rag.gating import filter_chunks_by_classification

        chunks = [{
            "id": "c1", "text": "EC content",
            "classification": "EC",
            "required_attribute": "nationality",
            "allowed_values": ["US", "CA"],
        }]
        attestation = {
            "principal_id": "u1",
            "attributes": {"nationality": "US"},
        }
        allowed, denied = filter_chunks_by_classification(
            chunks, attestation, verify_signature=lambda a: True
        )
        assert len(allowed) == 1
        assert denied == []

    def test_mismatched_value_denied(self):
        from axiom.rag.gating import filter_chunks_by_classification

        chunks = [{
            "id": "c1", "classification": "EC",
            "required_attribute": "nationality",
            "allowed_values": ["US"],
        }]
        attestation = {
            "principal_id": "u1",
            "attributes": {"nationality": "RU"},
        }
        allowed, denied = filter_chunks_by_classification(
            chunks, attestation, verify_signature=lambda a: True
        )
        assert allowed == []
        assert len(denied) == 1
        assert denied[0]["reason"] == "attribute_value_not_allowed"


class TestDifferentDomains:
    """Same gate handles any domain — EC, PHI, CUI."""

    def test_phi_clearance_gate(self):
        from axiom.rag.gating import filter_chunks_by_classification

        chunks = [{
            "id": "c1", "classification": "PHI",
            "required_attribute": "clearance",
            "allowed_values": ["medical_staff", "attending_physician"],
        }]
        attestation = {
            "principal_id": "u1",
            "attributes": {"clearance": "medical_staff"},
        }
        allowed, _ = filter_chunks_by_classification(
            chunks, attestation, verify_signature=lambda a: True
        )
        assert len(allowed) == 1

    def test_cui_citizenship_gate(self):
        from axiom.rag.gating import filter_chunks_by_classification

        chunks = [{
            "id": "c1", "classification": "CUI",
            "required_attribute": "citizenship",
            "allowed_values": ["US"],
        }]
        attestation = {
            "principal_id": "u1",
            "attributes": {"citizenship": "US"},
        }
        allowed, _ = filter_chunks_by_classification(
            chunks, attestation, verify_signature=lambda a: True
        )
        assert len(allowed) == 1


class TestMissingAttestation:
    def test_no_attestation_denies_classified(self):
        from axiom.rag.gating import filter_chunks_by_classification

        chunks = [
            {"id": "c1"},
            {"id": "c2", "classification": "EC",
             "required_attribute": "nationality",
             "allowed_values": ["US"]},
        ]
        allowed, denied = filter_chunks_by_classification(
            chunks, attestation=None, verify_signature=lambda a: True
        )
        assert len(allowed) == 1  # c1 unclassified passes
        assert allowed[0]["id"] == "c1"
        assert len(denied) == 1
        assert denied[0]["reason"] == "no_attestation"

    def test_attestation_missing_required_attribute_denies(self):
        from axiom.rag.gating import filter_chunks_by_classification

        chunks = [{
            "id": "c1", "classification": "EC",
            "required_attribute": "nationality",
            "allowed_values": ["US"],
        }]
        # Attestation has "clearance" but not "nationality"
        attestation = {
            "principal_id": "u1",
            "attributes": {"clearance": "L3"},
        }
        allowed, denied = filter_chunks_by_classification(
            chunks, attestation, verify_signature=lambda a: True
        )
        assert allowed == []
        assert denied[0]["reason"] == "no_attestation"


class TestSignatureVerification:
    def test_unverified_signature_denies(self):
        from axiom.rag.gating import filter_chunks_by_classification

        chunks = [{
            "id": "c1", "classification": "EC",
            "required_attribute": "nationality",
            "allowed_values": ["US"],
        }]
        attestation = {
            "principal_id": "u1",
            "attributes": {"nationality": "US"},
            "signature": "forged",
        }
        allowed, denied = filter_chunks_by_classification(
            chunks, attestation,
            verify_signature=lambda a: False,
        )
        assert allowed == []
        assert denied[0]["reason"] == "attestation_not_verified"


class TestAuditLog:
    def test_write_jsonl_audit(self, tmp_path):
        from axiom.rag.gating import (
            filter_chunks_by_classification,
            log_denied_accesses,
        )

        chunks = [{
            "id": "c1", "classification": "EC",
            "required_attribute": "nationality",
            "allowed_values": ["US"],
        }]
        attestation = {
            "principal_id": "u1",
            "attributes": {"nationality": "RU"},
        }
        _, denied = filter_chunks_by_classification(
            chunks, attestation, verify_signature=lambda a: True
        )
        log_path = tmp_path / "audit.jsonl"
        log_denied_accesses(
            denied=denied,
            principal_id="u1",
            context={"classroom_id": "cr"},
            log_path=log_path,
        )
        content = log_path.read_text()
        assert "c1" in content
        assert "u1" in content


class TestAttestationBuilder:
    def test_build_generic_attestation(self):
        from axiom.rag.gating import build_principal_attestation

        claim = build_principal_attestation(
            principal_id="u1",
            attributes={"nationality": "US", "clearance": "L3"},
            signer_node="example-host.axiom.example-org",
            context={"classroom_id": "cr"},
        )
        assert claim["principal_id"] == "u1"
        assert claim["attributes"] == {"nationality": "US", "clearance": "L3"}
        assert claim["signer_node"] == "example-host.axiom.example-org"
        assert claim["context"] == {"classroom_id": "cr"}
        assert "issued_at" in claim
        assert "signature" in claim


class TestMultipleAttributesOnOneChunk:
    """Chunks can require multiple attributes (e.g. citizenship AND clearance)."""

    def test_multiple_requirements_all_must_match(self):
        from axiom.rag.gating import filter_chunks_by_classification

        chunks = [{
            "id": "c1", "classification": "COMPOUND",
            "required_attributes": [
                {"attribute": "nationality", "allowed_values": ["US"]},
                {"attribute": "clearance", "allowed_values": ["L3", "L4"]},
            ],
        }]
        # Passes: both attributes match
        att_pass = {"principal_id": "u1",
                    "attributes": {"nationality": "US", "clearance": "L3"}}
        allowed, _ = filter_chunks_by_classification(
            chunks, att_pass, verify_signature=lambda a: True
        )
        assert len(allowed) == 1

        # Denies: nationality matches but clearance doesn't
        att_fail = {"principal_id": "u1",
                    "attributes": {"nationality": "US", "clearance": "L1"}}
        allowed, denied = filter_chunks_by_classification(
            chunks, att_fail, verify_signature=lambda a: True
        )
        assert allowed == []
        assert denied[0]["reason"] == "attribute_value_not_allowed"
