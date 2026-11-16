# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The shipped quick default must be a model the Ollama ensure list pulls.

ADR-054's `simple` tier is `gemma2:2b`; ADR-140 (addendum 2026-10-16) points
the quick role at it. A default that names a model the node never pulls
resolves to "model not loaded" on a fresh node, which is exactly what the
ensure list exists to prevent. Both roles must be in it.
"""

from __future__ import annotations

from axiom.llm import local_models
from axiom.llm.local_models import OLLAMA_ENSURE_MODELS, QUICK, REASONING, resolve


def test_the_quick_default_is_gemma2_2b_and_reasoning_stays_qwen():
    assert QUICK == "gemma2:2b"
    assert REASONING == "qwen2.5:7b"


def test_the_ensure_list_pulls_the_shipped_quick_default():
    assert QUICK in OLLAMA_ENSURE_MODELS


def test_the_ensure_list_pulls_every_role_default():
    for role in ("quick", "reasoning"):
        assert resolve(role) in OLLAMA_ENSURE_MODELS


def test_the_ensure_list_has_no_duplicates_and_is_immutable():
    assert len(set(OLLAMA_ENSURE_MODELS)) == len(OLLAMA_ENSURE_MODELS)
    assert isinstance(OLLAMA_ENSURE_MODELS, tuple)


def test_the_ensure_list_is_exported():
    assert "OLLAMA_ENSURE_MODELS" in local_models.__all__
