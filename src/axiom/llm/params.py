# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dynamic LLM parameter tuning per prompt intent.

Adjusts temperature, max_tokens, top_p, and stop sequences based on
the classified intent of the query. Thinking models (Qwen 3.5) get
higher token budgets to accommodate reasoning chains.

Usage::

    from axiom.llm.params import get_params_for_intent

    params = get_params_for_intent("factual_retrieval", is_thinking_model=True)
    # → {"temperature": 0.0, "max_tokens": 2000, "top_p": 0.1}
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LLMParams:
    """Tuned parameters for a specific prompt intent."""

    temperature: float = 0.3
    max_tokens: int = 1000
    top_p: float = 0.9
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0
    stop: list[str] | None = None

    def to_dict(self) -> dict:
        d = {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
        }
        if self.presence_penalty:
            d["presence_penalty"] = self.presence_penalty
        if self.frequency_penalty:
            d["frequency_penalty"] = self.frequency_penalty
        if self.stop:
            d["stop"] = self.stop
        return d


# Intent → parameter presets
_INTENT_PARAMS: dict[str, LLMParams] = {
    # Factual retrieval: deterministic, precise
    "factual_retrieval": LLMParams(
        temperature=0.0,
        max_tokens=500,
        top_p=0.1,
    ),
    # RAG-grounded answer: low creativity, cite sources
    "rag_grounded": LLMParams(
        temperature=0.1,
        max_tokens=800,
        top_p=0.3,
    ),
    # Synthesis/analysis: moderate creativity
    "synthesis": LLMParams(
        temperature=0.4,
        max_tokens=1500,
        top_p=0.8,
    ),
    # Code generation: low temperature, code-specific stops
    "code_generation": LLMParams(
        temperature=0.2,
        max_tokens=2000,
        top_p=0.5,
        stop=["```\n\n", "\n\n\n"],
    ),
    # Creative writing / brainstorming
    "creative": LLMParams(
        temperature=0.7,
        max_tokens=2000,
        top_p=0.95,
    ),
    # Classification / routing (very short, deterministic)
    "classification": LLMParams(
        temperature=0.0,
        max_tokens=50,
        top_p=0.1,
    ),
    # Entity extraction (structured output)
    "extraction": LLMParams(
        temperature=0.1,
        max_tokens=2000,
        top_p=0.3,
    ),
    # Default fallback
    "fallback": LLMParams(
        temperature=0.3,
        max_tokens=1000,
        top_p=0.9,
    ),
}

# Thinking model multiplier — models like Qwen 3.5 that use chain-of-thought
# need higher token budgets because reasoning consumes tokens before the answer
_THINKING_MODEL_TOKEN_MULTIPLIER = 3
_THINKING_MODEL_PATTERNS = ["qwen3", "qwen-3", "o1", "o3", "deepseek-r1"]


def is_thinking_model(model_name: str) -> bool:
    """Check if a model uses chain-of-thought reasoning (thinking mode)."""
    name_lower = model_name.lower()
    return any(pat in name_lower for pat in _THINKING_MODEL_PATTERNS)


def get_params_for_intent(
    intent: str,
    model_name: str = "",
    override: dict | None = None,
) -> LLMParams:
    """Get tuned LLM parameters for a prompt intent.

    Args:
        intent: Query intent (factual_retrieval, rag_grounded, synthesis, etc.)
        model_name: Model name (used to detect thinking models)
        override: Optional parameter overrides

    Returns:
        LLMParams with tuned values
    """
    base = _INTENT_PARAMS.get(intent, _INTENT_PARAMS["fallback"])

    # Copy to avoid mutating the preset
    params = LLMParams(
        temperature=base.temperature,
        max_tokens=base.max_tokens,
        top_p=base.top_p,
        presence_penalty=base.presence_penalty,
        frequency_penalty=base.frequency_penalty,
        stop=list(base.stop) if base.stop else None,
    )

    # Thinking models need more tokens
    if model_name and is_thinking_model(model_name):
        params.max_tokens *= _THINKING_MODEL_TOKEN_MULTIPLIER

    # Apply overrides
    if override:
        for k, v in override.items():
            if hasattr(params, k):
                setattr(params, k, v)

    return params


def extract_answer_from_thinking(response: dict) -> str:
    """Extract the final answer from a thinking model response.

    Thinking models like Qwen 3.5 put chain-of-thought in reasoning_content
    and the final answer in content. If content is empty, extract the
    conclusion from reasoning_content.
    """
    content = response.get("content", "")
    reasoning = response.get("reasoning_content", "")

    if content and content.strip():
        return content.strip()

    if not reasoning:
        return ""

    # Try to find a conclusion/answer section in reasoning
    # Common patterns: "Answer:", "Final answer:", "Therefore,", "In conclusion,"
    import re

    for pattern in [
        r"(?:Final )?[Aa]nswer:\s*(.+?)(?:\n\n|\Z)",
        r"(?:In conclusion|Therefore|Thus|So),?\s*(.+?)(?:\n\n|\Z)",
        r"\*\*Answer\*\*:?\s*(.+?)(?:\n\n|\Z)",
    ]:
        match = re.search(pattern, reasoning, re.DOTALL)
        if match:
            return match.group(1).strip()

    # Fallback: return the full reasoning (better than nothing)
    return reasoning.strip()
