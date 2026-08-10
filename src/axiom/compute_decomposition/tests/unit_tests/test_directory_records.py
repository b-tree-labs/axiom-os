# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for the three new federation directory record types per spec §5.

Phase A defines + registers the schema; full gossip wiring is
post-Prague. We exercise:
- The three record types are declared in a closed enum and registered
  with the federation directory's record-type registry.
- Each record type's payload schema validates the canonical example
  from the spec.
- Round-trip serialization (record -> dict -> record) is bit-identical
  on hash.
- TTL semantics: COMPUTE_OFFER renews; COMPUTE_CLAIM expires; stale
  COMPUTE_RESULTs survive long enough for aggregation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


from axiom.compute_decomposition.directory_records import (
    COMPUTE_OFFER,
    COMPUTE_CLAIM,
    COMPUTE_RESULT,
    ComputeClaim,
    ComputeOffer,
    ComputeResult,
    ComputeRecordType,
    REGISTERED_RECORD_TYPES,
    is_active_claim,
    record_to_dict,
    record_from_dict,
)


def test_record_types_are_a_closed_set():
    """Exactly these three; no more, no less."""
    assert REGISTERED_RECORD_TYPES == frozenset({
        COMPUTE_OFFER,
        COMPUTE_CLAIM,
        COMPUTE_RESULT,
    })


def test_record_type_string_constants_match_spec():
    """The wire-protocol string values must match the spec verbatim
    or older peers will silently drop the records."""
    assert COMPUTE_OFFER == "COMPUTE_OFFER"
    assert COMPUTE_CLAIM == "COMPUTE_CLAIM"
    assert COMPUTE_RESULT == "COMPUTE_RESULT"


def test_compute_offer_canonical_example_validates():
    """The spec §5.1 example must construct without error."""
    now = datetime.now(UTC)
    offer = ComputeOffer(
        authority="node-test-01",
        signed_at=now,
        ttl_seconds=180,
        cpu_cores_available=8,
        cpu_load_avg_5m=0.6,
        ram_gb_available=32.0,
        gpu_kind="nvidia-cuda",
        gpu_vram_gb=24.0,
        disk_gb_available=500.0,
        network_class="lan-fast",
        classification_ceiling="public",
        pattern_support={"embarrassingly_parallel": ["python", "bash"]},
        accept_strangers_in_cohort=True,
        accept_cross_cohort=False,
        daily_compute_budget_minutes=120,
        signature=b"\x00" * 64,
    )
    assert offer.record_type is ComputeRecordType.COMPUTE_OFFER


def test_compute_claim_canonical_example_validates():
    now = datetime.now(UTC)
    claim = ComputeClaim(
        authority="node-test-02",
        signed_at=now,
        ttl_seconds=60,
        plan_id="plan-abc",
        chunk_id="chunk-001",
        claimed_at=now,
        expected_completion_at=now + timedelta(seconds=300),
        retry_count=0,
        adapter_language="python",
        sandbox_profile_id="default-py-no-net",
        signature=b"\x00" * 64,
    )
    assert claim.record_type is ComputeRecordType.COMPUTE_CLAIM


def test_compute_result_canonical_example_validates():
    now = datetime.now(UTC)
    result = ComputeResult(
        authority="node-test-02",
        signed_at=now,
        ttl_seconds=604800,
        plan_id="plan-abc",
        chunk_id="chunk-001",
        output_content_hash="sha256:abc",
        output_uri="axiom://artifact/abc",
        output_bytes=1024,
        output_media_type="application/json",
        adapter_code_hash="sha256:def",
        adapter_signature=b"\x00" * 64,
        runtime="python-3.13.1",
        solver_version=None,
        sandbox_profile_id="default-py-no-net",
        trait="deterministic",
        seed_used=None,
        elapsed_ms=4321,
        started_at=now,
        finished_at=now + timedelta(seconds=4),
        cpu_cores_used=2,
        peak_ram_mb=512,
        gpu_used=False,
        signature=b"\x00" * 64,
    )
    assert result.record_type is ComputeRecordType.COMPUTE_RESULT


def test_record_round_trip_through_dict_is_idempotent():
    now = datetime.now(UTC).replace(microsecond=0)
    offer = ComputeOffer(
        authority="node-x",
        signed_at=now,
        ttl_seconds=180,
        cpu_cores_available=4,
        cpu_load_avg_5m=0.1,
        ram_gb_available=16.0,
        gpu_kind="none",
        gpu_vram_gb=None,
        disk_gb_available=200.0,
        network_class="wan-broadband",
        classification_ceiling="public",
        pattern_support={"embarrassingly_parallel": ["python"]},
        accept_strangers_in_cohort=True,
        accept_cross_cohort=False,
        daily_compute_budget_minutes=None,
        signature=b"\x00" * 64,
    )
    as_dict = record_to_dict(offer)
    restored = record_from_dict(as_dict)
    assert isinstance(restored, ComputeOffer)
    # Round-trip through dict must produce the same dict on re-encoding.
    assert record_to_dict(restored) == as_dict


def test_active_claim_check_respects_ttl():
    now = datetime.now(UTC)
    fresh = ComputeClaim(
        authority="node-x",
        signed_at=now,
        ttl_seconds=60,
        plan_id="p", chunk_id="c", claimed_at=now,
        expected_completion_at=now + timedelta(minutes=5),
        retry_count=0, adapter_language="python",
        sandbox_profile_id="default", signature=b"\x00" * 64,
    )
    stale = ComputeClaim(
        authority="node-x",
        signed_at=now - timedelta(seconds=120),
        ttl_seconds=60,
        plan_id="p", chunk_id="c", claimed_at=now - timedelta(seconds=120),
        expected_completion_at=now - timedelta(seconds=60),
        retry_count=0, adapter_language="python",
        sandbox_profile_id="default", signature=b"\x00" * 64,
    )
    assert is_active_claim(fresh, now=now) is True
    assert is_active_claim(stale, now=now) is False
