# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for LLM-assisted entity extraction (Stage 2)."""

from __future__ import annotations

MOCK_LLM_RESPONSE = """{
  "entities": [
    {"label": "Material", "name": "LiF-BeF2-ZrF4-UF4", "properties": {"formula": "fluoride salt"}, "confidence": 0.9},
    {"label": "Component", "name": "MSRE", "properties": {}, "confidence": 0.95}
  ],
  "relationships": [
    {"type": "COMPOSED_OF", "from_name": "MSRE", "from_label": "Component", "to_name": "LiF-BeF2-ZrF4-UF4", "to_label": "Material", "confidence": 0.85}
  ]
}"""


class TestLLMExtraction:
    def test_importable(self):
        from axiom.graph.extractors.llm import extract_from_section
        assert callable(extract_from_section)

    def test_returns_empty_without_url(self):
        from axiom.graph.extractors.llm import extract_from_section

        entities, edges = extract_from_section("Some text", "doc.md")
        assert entities == []
        assert edges == []

    def test_parses_mock_response(self):
        from axiom.graph.extractors.llm import _parse_extraction_response

        entities, edges = _parse_extraction_response(MOCK_LLM_RESPONSE, "doc.md")
        assert len(entities) == 2
        assert entities[0].label == "Material"
        assert entities[0].name == "LiF-BeF2-ZrF4-UF4"
        assert len(edges) == 1
        assert edges[0].rel_type == "COMPOSED_OF"

    def test_handles_code_fence_wrapper(self):
        from axiom.graph.extractors.llm import _parse_extraction_response

        wrapped = f"```json\n{MOCK_LLM_RESPONSE}\n```"
        entities, edges = _parse_extraction_response(wrapped, "doc.md")
        assert len(entities) == 2

    def test_handles_invalid_json(self):
        from axiom.graph.extractors.llm import _parse_extraction_response

        entities, edges = _parse_extraction_response("not json at all", "doc.md")
        assert entities == []
        assert edges == []

    def test_handles_empty_response(self):
        from axiom.graph.extractors.llm import _parse_extraction_response

        entities, edges = _parse_extraction_response('{"entities": [], "relationships": []}', "doc.md")
        assert entities == []
        assert edges == []

    def test_provenance_set_to_llm(self):
        from axiom.graph.extractors.llm import _parse_extraction_response

        entities, _ = _parse_extraction_response(MOCK_LLM_RESPONSE, "doc.md")
        assert all(e.provenance == "llm_extracted" for e in entities)
