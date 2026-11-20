# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for dynamic LLM parameter tuning."""

from axiom.infra.llm_params import (
    LLMParams,
    extract_answer_from_thinking,
    get_params_for_intent,
    is_thinking_model,
)


class TestLLMParams:
    def test_factual_is_deterministic(self):
        p = get_params_for_intent("factual_retrieval")
        assert p.temperature == 0.0
        assert p.top_p == 0.1

    def test_creative_is_high_temp(self):
        p = get_params_for_intent("creative")
        assert p.temperature >= 0.5

    def test_rag_grounded_is_low_temp(self):
        p = get_params_for_intent("rag_grounded")
        assert p.temperature <= 0.2

    def test_thinking_model_gets_more_tokens(self):
        normal = get_params_for_intent("factual_retrieval", model_name="llama3")
        thinking = get_params_for_intent("factual_retrieval", model_name="Qwen3.5-122B")
        assert thinking.max_tokens > normal.max_tokens

    def test_is_thinking_model(self):
        assert is_thinking_model("Qwen3.5-122B-A10B") is True
        assert is_thinking_model("o1-preview") is True
        assert is_thinking_model("llama3-70b") is False
        assert is_thinking_model("deepseek-r1") is True

    def test_override(self):
        p = get_params_for_intent("factual_retrieval", override={"temperature": 0.5})
        assert p.temperature == 0.5

    def test_unknown_intent_uses_fallback(self):
        p = get_params_for_intent("nonexistent_intent")
        assert p.temperature == 0.3  # fallback default

    def test_extract_answer_from_content(self):
        resp = {"content": "The answer is 42.", "reasoning_content": "Let me think..."}
        assert extract_answer_from_thinking(resp) == "The answer is 42."

    def test_extract_answer_from_reasoning(self):
        resp = {"content": "", "reasoning_content": "Thinking...\n\nFinal answer: The MSRE used LiF-BeF2."}
        assert "LiF-BeF2" in extract_answer_from_thinking(resp)

    def test_extract_answer_empty(self):
        resp = {"content": "", "reasoning_content": ""}
        assert extract_answer_from_thinking(resp) == ""

    def test_to_dict(self):
        p = LLMParams(temperature=0.1, max_tokens=500)
        d = p.to_dict()
        assert d["temperature"] == 0.1
        assert d["max_tokens"] == 500
