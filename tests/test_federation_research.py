# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for Call to Research — distributed research coordination."""

import pytest

from axiom.vega.federation.knowledge_metrics import (
    KnowledgeMetricsService,
)
from axiom.vega.federation.research import (
    CallLevel,
    CallStatus,
    PartStatus,
    ResearchService,
)
from axiom.vega.federation.wasm_sandbox import (
    ExecutionResult,
    SandboxConfig,
    is_wasmtime_available,
    validate_wasm_module,
)


class TestCallToResearch:
    """Core data model tests."""

    def test_create_call(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call(
            title="UZrH thermal conductivity at 800K",
            description="What is the thermal conductivity?",
            caller_node_id="node-abc",
            caller_name="Ben Collins",
            level=1,
        )
        assert call.call_id.startswith("ctr-")
        assert call.status == CallStatus.DRAFT
        assert call.level == CallLevel.FACT_RETRIEVAL

    def test_to_dict_serialization(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call("Test", "Desc", "n1", "Alice", level=2)
        d = call.to_dict()
        assert d["level"] == 2
        assert d["status"] == "draft"
        assert "call_id" in d

    def test_persistence_round_trip(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call(
            "Test", "Desc", "n1", "Alice", level=3, tags=["triga"]
        )
        loaded = svc.get(call.call_id)
        assert loaded is not None
        assert loaded.title == "Test"
        assert loaded.level == CallLevel.COMPUTATIONAL
        assert loaded.tags == ["triga"]


class TestCallLifecycle:
    """Full lifecycle: draft -> open -> in_progress -> assembling -> published."""

    def test_open_call(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call("Test", "Desc", "n1", "Alice", level=1)
        opened = svc.open_call(call.call_id)
        assert opened.status == CallStatus.OPEN

    def test_cannot_open_non_draft(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call("Test", "Desc", "n1", "Alice", level=1)
        svc.open_call(call.call_id)
        with pytest.raises(ValueError, match="only open draft"):
            svc.open_call(call.call_id)

    def test_add_parts(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call(
            "Survey", "Literature survey", "n1", "Alice", level=2
        )
        p1 = svc.add_part(call.call_id, "Mechanistic models", "literature_survey")
        svc.add_part(
            call.call_id, "Empirical correlations", "literature_survey"
        )
        loaded = svc.get(call.call_id)
        assert len(loaded.parts) == 2
        assert loaded.parts[0].part_id == p1.part_id

    def test_claim_part(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call("Test", "Desc", "n1", "Alice", level=2)
        part = svc.add_part(call.call_id, "Part A", "literature_survey")
        claimed = svc.claim_part(
            call.call_id, part.part_id, "node-osu", "OSU Researcher"
        )
        assert claimed.status == PartStatus.CLAIMED
        assert claimed.assigned_to == "node-osu"

    def test_submit_response(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call("Test", "Desc", "n1", "Alice", level=1)
        part = svc.add_part(call.call_id, "Find data", "fact_retrieval")
        svc.open_call(call.call_id)
        svc.claim_part(call.call_id, part.part_id, "n2", "Bob")
        resp = svc.submit_response(
            call.call_id,
            part.part_id,
            content={"value": "18.2 W/m-K", "temperature": "800K"},
            provenance=["GA-A13603"],
        )
        assert resp.responder_node_id == "n2"
        loaded = svc.get(call.call_id)
        assert loaded.status == CallStatus.IN_PROGRESS

    def test_accept_response(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call("Test", "Desc", "n1", "Alice", level=1)
        part = svc.add_part(call.call_id, "Find data", "fact_retrieval")
        svc.open_call(call.call_id)
        svc.claim_part(call.call_id, part.part_id, "n2", "Bob")
        svc.submit_response(
            call.call_id, part.part_id, content={"answer": "42"}
        )
        svc.accept_response(call.call_id, part.part_id)
        loaded = svc.get(call.call_id)
        assert loaded.parts[0].status == PartStatus.ACCEPTED
        assert loaded.status == CallStatus.ASSEMBLING  # all parts done

    def test_reject_and_reclaim(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call("Test", "Desc", "n1", "Alice", level=1)
        part = svc.add_part(call.call_id, "Find data", "fact_retrieval")
        svc.open_call(call.call_id)
        svc.claim_part(call.call_id, part.part_id, "n2", "Bob")
        svc.submit_response(
            call.call_id, part.part_id, content={"answer": "wrong"}
        )
        svc.reject_response(call.call_id, part.part_id)
        loaded = svc.get(call.call_id)
        assert loaded.parts[0].status == PartStatus.REJECTED
        assert loaded.parts[0].assigned_to == ""
        # Can be reclaimed by someone else
        svc.claim_part(call.call_id, part.part_id, "n3", "Carol")

    def test_request_revision(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call("Test", "Desc", "n1", "Alice", level=1)
        part = svc.add_part(call.call_id, "Find data", "fact_retrieval")
        svc.open_call(call.call_id)
        svc.claim_part(call.call_id, part.part_id, "n2", "Bob")
        svc.submit_response(
            call.call_id,
            part.part_id,
            content={"answer": "needs more detail"},
        )
        svc.request_revision(call.call_id, part.part_id)
        loaded = svc.get(call.call_id)
        assert loaded.parts[0].status == PartStatus.REVISION_REQUESTED

    def test_publish_synthesis(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call("Test", "Desc", "n1", "Alice", level=1)
        part = svc.add_part(call.call_id, "Find data", "fact_retrieval")
        svc.open_call(call.call_id)
        svc.claim_part(call.call_id, part.part_id, "n2", "Bob")
        svc.submit_response(
            call.call_id, part.part_id, content={"answer": "42"}
        )
        svc.accept_response(call.call_id, part.part_id)
        published = svc.publish_synthesis(
            call.call_id,
            "The answer is 42.",
            publication={"doi": "10.1234/test"},
        )
        assert published.status == CallStatus.PUBLISHED
        assert published.synthesis == "The answer is 42."
        assert published.publication["doi"] == "10.1234/test"

    def test_list_calls_filter(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        svc.create_call("A", "Desc", "n1", "Alice", level=1)
        call2 = svc.create_call("B", "Desc", "n1", "Alice", level=3)
        svc.open_call(call2.call_id)

        drafts = svc.list_calls(status="draft")
        assert len(drafts) == 1
        assert drafts[0].title == "A"

        level3 = svc.list_calls(level=3)
        assert len(level3) == 1
        assert level3[0].title == "B"


class TestLevel1FactRetrieval:
    """Level 1: Simple fact retrieval across federation."""

    def test_thermal_conductivity_query(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call(
            title="UZrH thermal conductivity at 800K",
            description="What is the thermal conductivity of UZrH fuel at 800K?",
            caller_node_id="example-node",
            caller_name="Ben Collins",
            level=1,
            tags=["triga", "fuel", "thermal-properties"],
        )
        # No decomposition needed for L1
        part = svc.add_part(
            call.call_id, "Report any UZrH thermal data above 600K"
        )
        svc.open_call(call.call_id)

        # OSU responds
        svc.claim_part(call.call_id, part.part_id, "osu-node", "OSU Researcher")
        svc.submit_response(
            call.call_id,
            part.part_id,
            content={
                "value": "18.2 W/m-K",
                "temperature_k": 800,
                "material": "UZrH-20",
                "conditions": "steady-state",
            },
            provenance=["GA-A13603", "doi:10.1016/j.jnucmat.2019.03.012"],
        )
        svc.accept_response(call.call_id, part.part_id)

        result = svc.get(call.call_id)
        assert result.status == CallStatus.ASSEMBLING
        assert len(result.responses) == 1
        assert result.responses[0].provenance[0] == "GA-A13603"


class TestLevel3Computational:
    """Level 3: Computational benchmark — specs only, never code."""

    def test_benchmark_call(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call(
            title="TRIGA cross-section library comparison",
            description="Run MCNP k-eff calculation with your local XS library",
            caller_node_id="example-node",
            caller_name="Cole Gentry",
            level=3,
            tags=["benchmark", "triga", "cross-sections"],
        )
        svc.add_part(
            call.call_id, "ENDF/B-VII.1 continuous-energy", "computational"
        )
        svc.add_part(
            call.call_id, "ENDF/B-VIII.0 continuous-energy", "computational"
        )
        svc.add_part(
            call.call_id, "ENDF/B-VII.1 multigroup", "computational"
        )

        svc.open_call(call.call_id)
        loaded = svc.get(call.call_id)
        assert loaded.level == CallLevel.COMPUTATIONAL
        assert len(loaded.parts) == 3
        # Verify no executable code in the call
        assert "exec" not in loaded.description.lower()
        assert "import" not in loaded.description.lower()


class TestLevel5Synthesis:
    """Level 5: Multi-institution paper assembly."""

    def test_paper_coordination(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call(
            title="TRIGA fuel performance review across facilities",
            description="Multi-facility review for Annals of Nuclear Energy",
            caller_node_id="example-node",
            caller_name="Ben Collins",
            level=5,
            license="co-authorship",
            tags=["publication", "triga", "fuel-performance"],
        )
        svc.add_part(
            call.call_id, "Steady-state fuel temperature data", "analytical"
        )
        svc.add_part(call.call_id, "Pulse performance data", "analytical")
        svc.add_part(call.call_id, "Burnup analysis", "analytical")
        svc.add_part(
            call.call_id,
            "Regulatory comparison (NUREG-1282)",
            "literature_survey",
        )
        svc.add_part(
            call.call_id, "Conclusions and recommendations", "synthesis"
        )

        svc.open_call(call.call_id)
        loaded = svc.get(call.call_id)
        assert loaded.level == CallLevel.SYNTHESIS
        assert loaded.license == "co-authorship"
        assert len(loaded.parts) == 5


class TestKnowledgeMetrics:
    """Knowledge observatory metrics."""

    def test_record_and_compute_velocity(self, tmp_path):
        svc = KnowledgeMetricsService(logs_dir=tmp_path)
        for i in range(10):
            svc.record_event(
                "fact_added", fact_id=f"f{i}", source="local", domain="materials"
            )
        for i in range(3):
            svc.record_event("fact_promoted", fact_id=f"f{i}")

        v = svc.compute_velocity(period_days=1)
        assert v.facts_per_day == 10.0
        assert v.facts_by_source["local"] == 10
        assert v.promotion_rate == 0.3

    def test_compute_accumulation(self, tmp_path):
        svc = KnowledgeMetricsService(logs_dir=tmp_path)
        for i in range(5):
            svc.record_event(
                "fact_added", fact_id=f"f{i}", maturity=1, domain="materials"
            )
        for i in range(3):
            svc.record_event(
                "fact_added", fact_id=f"g{i}", maturity=3, domain="safety"
            )

        acc = svc.compute_accumulation()
        assert acc.total_facts == 8
        assert acc.by_maturity["1"] == 5
        assert acc.by_maturity["3"] == 3

    def test_compute_impact(self, tmp_path):
        svc = KnowledgeMetricsService(logs_dir=tmp_path)
        for i in range(20):
            svc.record_event(
                "fact_retrieved",
                fact_id=f"f{i % 5}",
                source_type="local",
                answered=True,
            )
        for i in range(5):
            svc.record_event(
                "fact_retrieved",
                fact_id=f"fed{i}",
                source_type="federation",
                answered=True,
                federation_only=True,
                query=f"q{i}",
            )

        imp = svc.compute_impact(period_days=1)
        assert imp.retrievals_per_day == 25.0
        assert imp.federation_facts_retrieved == 5
        assert imp.federation_unique_answers == 5  # THE killer metric

    def test_generate_report(self, tmp_path):
        svc = KnowledgeMetricsService(logs_dir=tmp_path)
        svc.record_event(
            "fact_added",
            fact_id="f1",
            source="local",
            domain="materials",
            maturity=1,
        )
        svc.record_event(
            "fact_retrieved", fact_id="f1", source_type="local", answered=True
        )

        report = svc.generate_report(node_id="test-node")
        assert report.node_id == "test-node"
        d = report.to_dict()
        assert "velocity" in d
        assert "accumulation" in d
        assert "impact" in d

    def test_empty_logs(self, tmp_path):
        svc = KnowledgeMetricsService(logs_dir=tmp_path)
        v = svc.compute_velocity()
        assert v.facts_per_day == 0
        acc = svc.compute_accumulation()
        assert acc.total_facts == 0


class TestComposableChains:
    """Composable research chains: Call A -> Call B -> Call C."""

    def test_link_calls_bidirectional(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        a = svc.create_call("Call A", "First", "n1", "Alice", level=1)
        b = svc.create_call("Call B", "Second", "n1", "Alice", level=2)

        svc.link_calls(a.call_id, b.call_id)

        parent = svc.get(a.call_id)
        child = svc.get(b.call_id)
        assert b.call_id in parent.output_to
        assert a.call_id in child.input_from

    def test_get_research_chain_three_calls(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        a = svc.create_call("Call A", "Root", "n1", "Alice", level=1)
        b = svc.create_call("Call B", "Middle", "n1", "Alice", level=2)
        c = svc.create_call("Call C", "Leaf", "n1", "Alice", level=3)

        svc.link_calls(a.call_id, b.call_id)
        svc.link_calls(b.call_id, c.call_id)

        chain = svc.get_research_chain(c.call_id)
        assert len(chain) == 3
        assert chain[0].call_id == a.call_id
        assert chain[1].call_id == b.call_id
        assert chain[2].call_id == c.call_id

    def test_get_research_chain_from_middle(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        a = svc.create_call("Call A", "Root", "n1", "Alice", level=1)
        b = svc.create_call("Call B", "Middle", "n1", "Alice", level=2)
        c = svc.create_call("Call C", "Leaf", "n1", "Alice", level=3)

        svc.link_calls(a.call_id, b.call_id)
        svc.link_calls(b.call_id, c.call_id)

        chain = svc.get_research_chain(b.call_id)
        assert len(chain) == 3
        assert chain[0].call_id == a.call_id

    def test_input_from_output_to_serialization(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        a = svc.create_call("Call A", "First", "n1", "Alice", level=1)
        b = svc.create_call("Call B", "Second", "n1", "Alice", level=2)
        svc.link_calls(a.call_id, b.call_id)

        d = svc.get(a.call_id).to_dict()
        assert "input_from" in d
        assert "output_to" in d
        assert b.call_id in d["output_to"]


class TestWasmParts:
    """WASM executable part type and validation."""

    def test_wasm_part_requires_operator_approval(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call("Test", "Desc", "n1", "Alice", level=3)
        with pytest.raises(ValueError, match="operator approval"):
            svc.add_part(call.call_id, "Run simulation", "wasm_executable")

    def test_wasm_part_with_approval(self, tmp_path):
        svc = ResearchService(storage_dir=tmp_path)
        call = svc.create_call("Test", "Desc", "n1", "Alice", level=3)
        part = svc.add_part(
            call.call_id, "Run simulation", "wasm_executable", operator_approved=True
        )
        assert part.part_type == "wasm_executable"


class TestWasmSandbox:
    """WASM sandbox utilities."""

    def test_sandbox_config_defaults(self):
        cfg = SandboxConfig()
        assert cfg.max_memory_mb == 256
        assert cfg.max_cpu_seconds == 300
        assert cfg.allow_filesystem is False
        assert cfg.allow_network is False

    def test_execution_result_dataclass(self):
        r = ExecutionResult(success=True, output={"key": "val"})
        assert r.success is True
        assert r.output == {"key": "val"}
        assert r.error == ""
        assert r.runtime_seconds == 0

    def test_is_wasmtime_available_returns_bool(self):
        result = is_wasmtime_available()
        assert isinstance(result, bool)

    def test_validate_wasm_missing_file(self, tmp_path):
        result = validate_wasm_module(tmp_path / "nonexistent.wasm")
        assert result["valid"] is False
        assert "not found" in result["error"].lower()

    def test_validate_wasm_wrong_extension(self, tmp_path):
        bad = tmp_path / "module.txt"
        bad.write_bytes(b"\x00asm" + b"\x00" * 100)
        result = validate_wasm_module(bad)
        assert result["valid"] is False
        assert "Not a .wasm file" in result["error"]

    def test_validate_wasm_bad_magic(self, tmp_path):
        bad = tmp_path / "module.wasm"
        bad.write_bytes(b"notw" + b"\x00" * 100)
        result = validate_wasm_module(bad)
        assert result["valid"] is False
        assert "magic bytes" in result["error"].lower()

    def test_validate_wasm_valid(self, tmp_path):
        good = tmp_path / "module.wasm"
        good.write_bytes(b"\x00asm" + b"\x00" * 100)
        result = validate_wasm_module(good)
        assert result["valid"] is True
        assert "size_mb" in result
