# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The assistant knows today's date, so "last week" means last week.

Asked on 2026-11-10 how the last week went, a chat queried 2026-03-08 to
03-15: nothing in its prompt said what day it was, so it resolved "last week"
against a year its training suggested, and reported that week as last week.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from axiom.extensions.builtins.chat import agent as agent_mod
from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.infra.bus import EventBus
from axiom.infra.gateway import Gateway
from axiom.infra.orchestrator.session import Session

_TUESDAY = datetime(2026, 11, 10, 21, 0, tzinfo=UTC)


def _agent(tmp_path) -> ChatAgent:
    return ChatAgent(
        gateway=MagicMock(spec=Gateway),  # the LLM boundary; not called
        bus=EventBus(log_path=tmp_path / "events.jsonl"),
        session=Session(),
    )


def test_the_prompt_states_the_current_date(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "_wall_clock", lambda: _TUESDAY)
    prompt = _agent(tmp_path)._build_system_prompt()

    assert "Tuesday 2026-11-10 21:00 UTC" in prompt
    assert "last week" in prompt


def test_the_date_sits_after_the_cache_boundary(tmp_path, monkeypatch):
    """The cached prefix must stay byte-stable across turns and days."""
    from axiom.extensions.builtins.chat.agent import _active_scope

    monkeypatch.setattr(agent_mod, "_wall_clock", lambda: _TUESDAY)
    a = _agent(tmp_path)
    a._build_system_prompt()
    blocks = _active_scope(a, None).last_composer.render_blocks()

    cached = [b["text"] for b in blocks if "cache_control" in b]
    fresh = [b["text"] for b in blocks if "cache_control" not in b]
    assert not any("Current date and time" in t for t in cached)
    assert any("Current date and time" in t for t in fresh)


def test_the_line_names_local_time_too():
    line = agent_mod.current_time_line(_TUESDAY)
    assert "UTC" in line and "local time" in line
