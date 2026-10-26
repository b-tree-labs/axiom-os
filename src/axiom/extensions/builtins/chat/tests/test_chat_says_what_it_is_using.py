# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Chat says at the start which model it is using, where it runs, and how to switch.

A new user's laptop answered questions about a remote installation with a
small model running on the laptop and no data from that installation. The
banner said ``ollama-qwen35-9b (qwen3.5-9b-16k)`` and nothing else: not that
the model was local, not what it was connected to, not how to change either.
A person cannot correct a default nobody told them they were on.

The platform knows the model. Only a product knows what else matters to its
users at the start of a session (which site, say), so branding contributes
those lines and the platform prints them.
"""

from __future__ import annotations

from axiom.extensions.builtins.chat.start_context import start_context_lines
from axiom.infra.branding import BrandingConfig
from axiom.llm.gateway import LLMProvider


def _provider(endpoint: str) -> LLMProvider:
    return LLMProvider(name="local-small", endpoint=endpoint, model="tiny-9b", api_key_env="")


def test_a_local_model_is_named_as_local_with_the_switch():
    lines = start_context_lines(_provider("http://localhost:11434/v1"), brand=BrandingConfig())
    model_line = lines[0]
    assert "tiny-9b" in model_line
    assert "this machine" in model_line
    assert "/model" in model_line


def test_a_remote_model_is_not_called_local():
    lines = start_context_lines(_provider("https://llm.example.org/v1"), brand=BrandingConfig())
    assert "this machine" not in lines[0]
    assert "llm.example.org" in lines[0]


def test_no_model_says_how_to_configure_one():
    lines = start_context_lines(None, brand=BrandingConfig(cli_name="acme"))
    assert "acme config" in lines[0]


def test_a_product_adds_its_own_lines():
    brand = BrandingConfig(chat_context_fn=lambda: ["Site: alpha (bound). Switch: acme site use"])
    lines = start_context_lines(_provider("http://127.0.0.1:8080/v1"), brand=brand)
    assert "Site: alpha (bound). Switch: acme site use" in lines


def test_a_product_hook_that_raises_costs_only_its_lines():
    def broken():
        raise RuntimeError("no site file")

    lines = start_context_lines(
        _provider("http://127.0.0.1:8080/v1"), brand=BrandingConfig(chat_context_fn=broken)
    )
    assert len(lines) == 1 and "tiny-9b" in lines[0]
