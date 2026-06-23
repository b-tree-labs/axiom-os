# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests covering all plan gaps — ensures every planned item is implemented."""

from __future__ import annotations

from unittest.mock import patch


class TestInteractionLog:
    def test_importable(self):
        from axiom.rag.interaction_log import get_interactions, log_interaction
        assert callable(log_interaction)
        assert callable(get_interactions)

    def test_entry_dataclass(self):
        from axiom.rag.interaction_log import InteractionEntry

        e = InteractionEntry(
            query_text="test", query_hash="abc", corpus="rag-community",
            generation=1, chunks_retrieved=5, top_similarity=0.8,
        )
        assert e.generation == 1

    def test_prune_function_exists(self):
        from axiom.rag.interaction_log import prune_interactions
        assert callable(prune_interactions)

    def test_ensure_schema_function(self):
        from axiom.rag.interaction_log import ensure_interaction_log
        assert callable(ensure_interaction_log)


class TestUpgradeOrchestrator:
    def test_importable(self):
        from axiom.rag.upgrade import build_generation
        assert callable(build_generation)

    def test_upgrade_stats_fields(self):
        from axiom.rag.upgrade import UpgradeStats

        s = UpgradeStats(
            corpus="rag-community", generation=2,
            chunking_tier="semantic", files_processed=100, chunks_created=5000,
        )
        assert s.generation == 2
        assert s.chunking_tier == "semantic"


class TestChunkingOptimizer:
    def test_importable(self):
        from axiom.extensions.builtins.research.chunking_optimizer import (
            propose_experiment,
        )
        assert callable(propose_experiment)

    def test_propose_experiment(self):
        from axiom.extensions.builtins.research.chunking_optimizer import propose_experiment

        exp = propose_experiment("rag-community")
        assert exp.corpus == "rag-community"
        assert exp.status == "pending"
        assert "min_chunk_size" in exp.parameters

    def test_validate_parameters_in_bounds(self):
        from axiom.extensions.builtins.research.chunking_optimizer import validate_parameters

        ok, msg = validate_parameters({"min_chunk_size": 300, "max_chunk_size": 1500})
        assert ok is True

    def test_validate_parameters_out_of_bounds(self):
        from axiom.extensions.builtins.research.chunking_optimizer import validate_parameters

        ok, msg = validate_parameters({"min_chunk_size": 50})
        assert ok is False
        assert "out of bounds" in msg

    def test_experiment_roundtrip(self):
        import tempfile
        from pathlib import Path

        from axiom.extensions.builtins.research.chunking_optimizer import (
            load_experiments,
            propose_experiment,
            save_experiment,
        )

        exp = propose_experiment("rag-community")

        with patch("axiom.extensions.builtins.research.chunking_optimizer._EXPERIMENTS_DIR",
                    Path(tempfile.mkdtemp())):
            save_experiment(exp)
            loaded = load_experiments("rag-community")
            assert len(loaded) == 1
            assert loaded[0].experiment_id == exp.experiment_id


class TestNodeManifestCompleteness:
    def test_has_compatible_format_versions(self):
        from axiom.vega.federation.identity import NodeManifest

        m = NodeManifest()
        assert hasattr(m, "compatible_format_versions")
        assert "1.0.0" in m.compatible_format_versions

    def test_manifest_serialization_includes_all_fields(self):
        from axiom.vega.federation.identity import NodeManifest

        m = NodeManifest(
            axiom_version="0.8.0",
            active_generations={"rag-community": 3},
            compatible_format_versions=["1.0.0", "2.0.0"],
        )
        d = m.to_dict()
        assert d["axiom_version"] == "0.8.0"
        assert d["active_generations"] == {"rag-community": 3}
        assert d["compatible_format_versions"] == ["1.0.0", "2.0.0"]


class TestIngestAcceptsGenerationParams:
    def test_ingest_file_accepts_chunking_tier(self):
        import inspect

        from axiom.rag.ingest import ingest_file

        sig = inspect.signature(ingest_file)
        assert "chunking_tier" in sig.parameters
        assert "corpus_generation" in sig.parameters

    def test_ingest_path_accepts_chunking_tier(self):
        import inspect

        from axiom.rag.ingest import ingest_path

        sig = inspect.signature(ingest_path)
        assert "chunking_tier" in sig.parameters
        assert "corpus_generation" in sig.parameters
