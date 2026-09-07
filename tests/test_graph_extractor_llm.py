# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for #86: graph LLM extractor migration to structured_output.

The extractor previously called an OpenAI-compatible endpoint via
urllib and parsed JSON from the text response. The new Gateway path
uses ``structured_output`` for schema-validated entity + relationship
extraction. The urllib path remains as a backward-compat fallback for
callers using ``AXIOM_LLM_URL`` / ``AXIOM_LLM_MODEL`` env vars.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.graph.extractors.llm import extract_from_section
from axiom.graph.schema import EntityTypeRegistry
from axiom.infra.gateway import CompletionResponse, ToolUseBlock


def _gateway_returning(entities: list, relationships: list) -> MagicMock:
    gw = MagicMock()
    gw.complete_with_tools.return_value = CompletionResponse(
        tool_use=[ToolUseBlock(
            tool_id="t1", name="emit_graph_extraction",
            input={"entities": entities, "relationships": relationships},
        )],
        success=True,
    )
    return gw


# ---------------------------------------------------------------------------
# Gateway path (new)
# ---------------------------------------------------------------------------


class TestGatewayPath:
    def test_returns_entities_and_edges(self):
        gw = _gateway_returning(
            entities=[
                {"label": "Concept", "name": "neutron",
                 "properties": {}, "confidence": 0.9},
                {"label": "Concept", "name": "fission",
                 "properties": {}, "confidence": 0.85},
            ],
            relationships=[
                {"type": "RELATES_TO", "from_name": "neutron",
                 "from_label": "Concept", "to_name": "fission",
                 "to_label": "Concept", "confidence": 0.8},
            ],
        )
        entities, edges = extract_from_section(
            text="Neutrons induce fission.",
            source_path="phys/intro.md",
            gateway=gw,
        )
        assert len(entities) == 2
        assert entities[0].name == "neutron"
        assert entities[0].confidence == pytest.approx(0.9)
        assert entities[0].source_path == "phys/intro.md"
        assert entities[0].provenance == "llm_extracted"
        assert len(edges) == 1
        assert edges[0].rel_type == "RELATES_TO"
        assert edges[0].from_name == "neutron"
        assert edges[0].to_name == "fission"

    def test_empty_text_short_circuits(self):
        gw = MagicMock()
        entities, edges = extract_from_section(
            text="   ", source_path="x.md", gateway=gw,
        )
        assert entities == []
        assert edges == []
        gw.complete_with_tools.assert_not_called()

    def test_empty_extraction_returns_empty_lists(self):
        gw = _gateway_returning(entities=[], relationships=[])
        entities, edges = extract_from_section(
            text="Some text.", source_path="a.md", gateway=gw,
        )
        assert entities == []
        assert edges == []

    def test_uses_emit_graph_extraction_tool(self):
        gw = _gateway_returning(entities=[], relationships=[])
        extract_from_section(
            text="x", source_path="a.md", gateway=gw,
        )
        kwargs = gw.complete_with_tools.call_args.kwargs
        assert len(kwargs["tools"]) == 1
        assert kwargs["tools"][0]["name"] == "emit_graph_extraction"

    def test_registry_types_flow_into_prompt(self):
        gw = _gateway_returning(entities=[], relationships=[])
        registry = EntityTypeRegistry()
        extract_from_section(
            text="x", source_path="a.md",
            registry=registry, gateway=gw,
        )
        prompt = gw.complete_with_tools.call_args.kwargs["messages"][0]["content"]
        # Expected entity labels (from default registry) should appear in prompt.
        for label in registry.entity_labels():
            assert label in prompt

    def test_malformed_confidence_clamped_or_skipped(self):
        """Entities with non-numeric confidence shouldn't crash extraction."""
        gw = _gateway_returning(
            entities=[
                {"label": "Concept", "name": "valid",
                 "properties": {}, "confidence": 0.9},
                {"label": "Concept", "name": "invalid",
                 "properties": {}, "confidence": "not_a_number"},
            ],
            relationships=[],
        )
        entities, _ = extract_from_section(
            text="x", source_path="a.md", gateway=gw,
        )
        # The valid entity must come through. The invalid one may be
        # skipped or its confidence defaulted — either is acceptable,
        # but the call must not raise.
        assert any(e.name == "valid" for e in entities)


class TestProviderFailure:
    def test_schema_validation_error_returns_empty(self):
        gw = MagicMock()
        gw.complete_with_tools.return_value = CompletionResponse(
            tool_use=[], success=False, error="no provider",
        )
        entities, edges = extract_from_section(
            text="x", source_path="a.md", gateway=gw,
        )
        assert entities == []
        assert edges == []

    def test_gateway_exception_returns_empty(self):
        gw = MagicMock()
        gw.complete_with_tools.side_effect = RuntimeError("boom")
        entities, edges = extract_from_section(
            text="x", source_path="a.md", gateway=gw,
        )
        assert entities == []
        assert edges == []


# ---------------------------------------------------------------------------
# Legacy urllib path (unchanged)
# ---------------------------------------------------------------------------


class TestLegacyPath:
    def test_no_gateway_and_no_env_returns_empty(self, monkeypatch):
        """Without a Gateway and without AXIOM_LLM_URL, the extractor
        returns empty lists rather than raising — unchanged from pre-#86."""
        monkeypatch.delenv("AXIOM_LLM_URL", raising=False)
        entities, edges = extract_from_section(
            text="some text", source_path="a.md",
        )
        assert entities == []
        assert edges == []
