# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the /skills and /agents tests: a real ChatAgent behind the real registry."""

from __future__ import annotations

from unittest.mock import MagicMock

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.infra.bus import EventBus
from axiom.infra.gateway import Gateway
from axiom.infra.orchestrator.session import Session


def make_agent() -> ChatAgent:
    """A real agent; only the model gateway (the true external boundary) is a stub."""
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    return ChatAgent(gateway=gw, bus=EventBus(), session=Session())


def run(line: str, agent: ChatAgent | None = None) -> str:
    """Dispatch a slash line through the real registry, as every surface does."""
    ensure_loaded()
    out = REGISTRY.dispatch(line, CommandContext(agent=agent or make_agent(), store=None))
    return out if isinstance(out, str) else repr(out)
