# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for CohortRegistry (#48).

Per ADR-027: single-coordinator-per-cohort with read-cache + failover
election shape. Not a DHT — shardable centralized registry, many
cohorts, no global point of failure.

Reads: served from local cache even if coordinator down.
Writes: require coordinator; queue during failover.
"""

from __future__ import annotations


class TestRegistration:
    def test_register_fragment_location(self):
        from axiom.memory.cohort_registry import CohortRegistry

        r = CohortRegistry(classroom_id="cr-1", coordinator_node="example-host")
        r = r.register("frag-1", held_by="example-host")
        assert r.locate("frag-1") == frozenset({"example-host"})

    def test_register_adds_replica(self):
        from axiom.memory.cohort_registry import CohortRegistry

        r = CohortRegistry(classroom_id="cr-1", coordinator_node="example-host")
        r = r.register("frag-1", held_by="example-host")
        r = r.register("frag-1", held_by="prague.eu")
        assert r.locate("frag-1") == frozenset({"example-host", "prague.eu"})

    def test_deregister_replica(self):
        from axiom.memory.cohort_registry import CohortRegistry

        r = CohortRegistry(classroom_id="cr-1", coordinator_node="example-host")
        r = r.register("frag-1", held_by="example-host")
        r = r.register("frag-1", held_by="prague.eu")
        r = r.deregister("frag-1", held_by="prague.eu")
        assert r.locate("frag-1") == frozenset({"example-host"})


class TestReadCache:
    def test_read_cache_snapshot(self):
        """Every member caches the registry locally for reads that
        survive coordinator outages."""
        from axiom.memory.cohort_registry import CohortRegistry

        r = CohortRegistry(classroom_id="cr-1", coordinator_node="example-host")
        r = r.register("frag-1", held_by="example-host")
        r = r.register("frag-2", held_by="prague.eu")

        snapshot = r.snapshot()
        assert snapshot["classroom_id"] == "cr-1"
        assert snapshot["coordinator_node"] == "example-host"
        assert set(snapshot["index"]["frag-1"]) == {"example-host"}
        assert set(snapshot["index"]["frag-2"]) == {"prague.eu"}

    def test_restore_from_snapshot(self):
        from axiom.memory.cohort_registry import CohortRegistry

        r1 = CohortRegistry(classroom_id="cr-1", coordinator_node="example-host")
        r1 = r1.register("frag-1", held_by="example-host")

        snap = r1.snapshot()
        r2 = CohortRegistry.from_snapshot(snap)
        assert r2.locate("frag-1") == frozenset({"example-host"})


class TestPropagationMode:
    """Auto-scale by cohort size. Ben's call: small=push, medium=pull,
    large=gossip. Operator can override."""

    def test_small_cohort_defaults_push(self):
        from axiom.memory.cohort_registry import propagation_mode_for_size

        assert propagation_mode_for_size(12) == "push"
        assert propagation_mode_for_size(99) == "push"

    def test_medium_cohort_defaults_pull(self):
        from axiom.memory.cohort_registry import propagation_mode_for_size

        assert propagation_mode_for_size(100) == "pull"
        assert propagation_mode_for_size(5000) == "pull"

    def test_large_cohort_defaults_gossip(self):
        from axiom.memory.cohort_registry import propagation_mode_for_size

        assert propagation_mode_for_size(10_000) == "gossip"
        assert propagation_mode_for_size(100_000) == "gossip"


class TestFailoverShape:
    """Read cache lets members serve reads during failover.
    Writes queue until a new coordinator is elected."""

    def test_reads_work_during_failover(self):
        from axiom.memory.cohort_registry import CohortRegistry

        r = CohortRegistry(classroom_id="cr-1", coordinator_node="example-host")
        r = r.register("frag-1", held_by="example-host")

        # Simulate coordinator failure — read cache still serves
        r = r.mark_coordinator_unreachable()
        assert r.coordinator_reachable is False
        # Reads still work from cache
        assert r.locate("frag-1") == frozenset({"example-host"})

    def test_writes_queue_during_failover(self):
        from axiom.memory.cohort_registry import CohortRegistry

        r = CohortRegistry(classroom_id="cr-1", coordinator_node="example-host")
        r = r.mark_coordinator_unreachable()

        # Write while unreachable → queued
        r = r.register("frag-new", held_by="prague.eu")
        assert len(r.pending_writes) == 1
        # The fragment is NOT in the main index yet
        assert r.locate("frag-new") == frozenset()

    def test_failover_election_flushes_queue(self):
        from axiom.memory.cohort_registry import CohortRegistry

        r = CohortRegistry(classroom_id="cr-1", coordinator_node="example-host")
        r = r.mark_coordinator_unreachable()
        r = r.register("frag-new", held_by="prague.eu")
        assert len(r.pending_writes) == 1

        # A new coordinator is elected → drain queue into index
        r = r.elect_coordinator("osu.edu")
        assert r.coordinator_node == "osu.edu"
        assert r.coordinator_reachable is True
        assert r.locate("frag-new") == frozenset({"prague.eu"})
        assert r.pending_writes == ()


class TestExplicitOverride:
    def test_coordinator_can_override_propagation_mode(self):
        from axiom.memory.cohort_registry import CohortRegistry

        r = CohortRegistry(
            classroom_id="cr-1",
            coordinator_node="example-host",
            propagation_mode="gossip",  # override for testing at small scale
        )
        assert r.propagation_mode == "gossip"
