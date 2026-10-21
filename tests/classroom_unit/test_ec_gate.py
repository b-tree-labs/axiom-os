# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for cross-node export-control gate (§5.11.4).

Per spec-classroom.md §5.11.4 + spec-classification-boundary.md.

EC-tagged corpus chunks are filtered out at retrieval time for
students whose nationality doesn't match the chunk's allowed list.
Attestation lives on the student's home node (issued at enrollment)
and is transported cross-node as a signed claim; the retrieving
node verifies the signature before honoring the claim.

Design:
- Chunks carry `ec_classification` + `allowed_nationalities` metadata
- Student attestation carries `nationality` + signature
- filter_chunks_by_ec returns (allowed, denied) — the denied list
  feeds an audit log (not suppressed silently)
"""

from __future__ import annotations


class TestUnclassifiedChunksAlwaysPass:
    def test_no_classification_means_public(self):
        from axiom.extensions.builtins.classroom.ec_gate import filter_chunks_by_ec

        chunks = [
            {"id": "c1", "text": "public content"},
            {"id": "c2", "text": "more public content"},
        ]
        attestation = {"student_id": "s1", "nationality": "US"}

        allowed, denied = filter_chunks_by_ec(chunks, attestation,
                                              verify_signature=lambda a: True)
        assert len(allowed) == 2
        assert denied == []


class TestECClassifiedChunks:
    def test_chunk_allowed_when_nationality_matches(self):
        from axiom.extensions.builtins.classroom.ec_gate import filter_chunks_by_ec

        chunks = [
            {"id": "c1", "text": "EC content",
             "ec_classification": "EC",
             "allowed_nationalities": ["US", "CA"]},
        ]
        attestation = {"student_id": "s1", "nationality": "US"}
        allowed, denied = filter_chunks_by_ec(chunks, attestation,
                                              verify_signature=lambda a: True)
        assert len(allowed) == 1
        assert denied == []

    def test_chunk_denied_when_nationality_mismatch(self):
        from axiom.extensions.builtins.classroom.ec_gate import filter_chunks_by_ec

        chunks = [
            {"id": "c1", "text": "EC content", "ec_classification": "EC",
             "allowed_nationalities": ["US"]},
        ]
        attestation = {"student_id": "s1", "nationality": "RU"}
        allowed, denied = filter_chunks_by_ec(chunks, attestation,
                                              verify_signature=lambda a: True)
        assert allowed == []
        assert len(denied) == 1
        assert denied[0]["chunk_id"] == "c1"
        assert denied[0]["reason"] == "nationality_not_allowed"


class TestUnattested:
    def test_no_attestation_denies_all_ec_content(self):
        from axiom.extensions.builtins.classroom.ec_gate import filter_chunks_by_ec

        chunks = [
            {"id": "c1", "text": "public"},
            {"id": "c2", "text": "EC", "ec_classification": "EC",
             "allowed_nationalities": ["US"]},
        ]
        # Student has no attestation at all
        allowed, denied = filter_chunks_by_ec(chunks, attestation=None,
                                              verify_signature=lambda a: True)
        # Public still passes; EC denied
        assert len(allowed) == 1
        assert allowed[0]["id"] == "c1"
        assert len(denied) == 1
        assert denied[0]["reason"] == "no_attestation"

    def test_attestation_with_no_nationality_denies_ec(self):
        from axiom.extensions.builtins.classroom.ec_gate import filter_chunks_by_ec

        chunks = [
            {"id": "c1", "text": "EC", "ec_classification": "EC",
             "allowed_nationalities": ["US"]},
        ]
        attestation = {"student_id": "s1"}  # nationality missing
        allowed, denied = filter_chunks_by_ec(chunks, attestation,
                                              verify_signature=lambda a: True)
        assert allowed == []
        assert denied[0]["reason"] == "no_attestation"


class TestSignatureVerification:
    def test_unverified_signature_denies_ec(self):
        """Critical path: cross-node attestation claim must be signed + verified."""
        from axiom.extensions.builtins.classroom.ec_gate import filter_chunks_by_ec

        chunks = [
            {"id": "c1", "text": "EC", "ec_classification": "EC",
             "allowed_nationalities": ["US"]},
        ]
        attestation = {"student_id": "s1", "nationality": "US",
                       "signature": "forged"}

        allowed, denied = filter_chunks_by_ec(
            chunks, attestation,
            verify_signature=lambda a: False,  # federation rejects
        )
        assert allowed == []
        assert denied[0]["reason"] == "attestation_not_verified"


class TestDeniedAuditLog:
    def test_write_denied_access_log(self, tmp_path):
        from axiom.extensions.builtins.classroom.ec_gate import (
            filter_chunks_by_ec,
            log_denied_accesses,
        )

        chunks = [
            {"id": "c1", "text": "EC", "ec_classification": "EC",
             "allowed_nationalities": ["US"]},
        ]
        attestation = {"student_id": "s1", "nationality": "RU"}
        _, denied = filter_chunks_by_ec(chunks, attestation,
                                        verify_signature=lambda a: True)
        log_path = tmp_path / "ec_audit.jsonl"
        log_denied_accesses(denied=denied, student_id="s1",
                            classroom_id="cr", log_path=log_path)

        assert log_path.exists()
        content = log_path.read_text()
        assert "c1" in content
        assert "s1" in content
        assert "nationality_not_allowed" in content


class TestAttestationClaimRoundTrip:
    def test_build_signed_attestation_claim(self):
        from axiom.extensions.builtins.classroom.ec_gate import (
            build_attestation_claim,
        )

        claim = build_attestation_claim(
            student_id="s1", nationality="US",
            signer_node="example-host.axiom.example-org", classroom_id="cr",
        )
        assert claim["student_id"] == "s1"
        assert claim["nationality"] == "US"
        assert claim["signer_node"] == "example-host.axiom.example-org"
        assert claim["classroom_id"] == "cr"
        assert "issued_at" in claim
        assert "signature" in claim  # federation fills in
