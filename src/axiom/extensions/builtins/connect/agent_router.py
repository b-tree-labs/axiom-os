# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Address any agent or subagent by name in a comms channel (ADR-074, C1).

A human in a channel should be able to talk to *any* of their agents — "TIDY:
prune the journal", "@Axiom TRIAGE what's degraded?", or just "Axi …" (default).
Each agent is a ``persona.md`` + a **skill namespace** (the bounded toolset it's
authorized to call — `skill_tools` insists "never all skills"). This module is
the addressee parser + per-agent resolver the channel runner uses to swap
persona/toolset per message; the actual run (agent mode) lands in C2, the
control verbs (status/stop/redirect) in C3.

Vendor-neutral: works over any ``InteractiveChannel`` (the runner passes the
text); subagents/runs are addressed by id through the same parser (C3 binds the
id → run).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Display casing: agents are ALL-CAPS personas (TIDY, TRIAGE…); the chat
# protagonist is branded "Axi" per the owner-possessive rule.
_DISPLAY_OVERRIDES = {"axi": "Axi"}
_ADDRESSEE_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_-]*)\b[:,]?\s*(.*)$", re.DOTALL)


@dataclass(frozen=True)
class AgentSpec:
    """A resolvable agent: its key, display name, persona dir, skill namespace."""

    name: str          # lowercase key, e.g. "tidy"
    display: str       # channel-facing, e.g. "TIDY" / "Axi"
    persona_dir: Path
    namespace: str     # skill namespace (== name); bounds the agent's toolset


@dataclass
class ResolvedAgent:
    """An addressed agent ready to seed a turn as: persona + bounded tools."""

    spec: AgentSpec
    persona: str = ""
    tools: list = field(default_factory=list)


def _builtins_root() -> Path:
    # .../extensions/builtins/connect/agent_router.py → .../extensions/builtins
    return Path(__file__).resolve().parents[1]


def discover_agents(root: Path | None = None) -> dict[str, AgentSpec]:
    """Discover agents by their ``<ext>/agents/<name>/persona.md`` convention."""
    root = root or _builtins_root()
    found: dict[str, AgentSpec] = {}
    for ext_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        agents_dir = ext_dir / "agents"
        if not agents_dir.is_dir():
            continue
        for agent_dir in sorted(p for p in agents_dir.iterdir() if p.is_dir()):
            if not (agent_dir / "persona.md").exists():
                continue
            name = agent_dir.name.lower()
            found[name] = AgentSpec(
                name=name,
                display=_DISPLAY_OVERRIDES.get(name, name.upper()),
                persona_dir=agent_dir,
                namespace=name,
            )
    return found


def parse_addressee(text: str, known: set[str]) -> tuple[str | None, str]:
    """Split a leading agent name off a message.

    "TIDY: prune" → ("tidy", "prune"); "Axi hi" → ("axi", "hi"); a message that
    doesn't start with a known agent name → (None, original) so it routes to the
    default agent. The leading ``@<bot>`` mention is already stripped upstream."""
    stripped = text.strip()
    m = _ADDRESSEE_RE.match(stripped)
    if m:
        cand = m.group(1).lower()
        if cand in known:
            rest = m.group(2).strip()
            return cand, rest or stripped
    return None, stripped


def suggest(name: str, known: set[str]) -> list[str]:
    """Did-you-mean for an unknown addressee."""
    return difflib.get_close_matches(name.lower(), sorted(known), n=3, cutoff=0.6)


def resolve_agent(
    name: str | None,
    agents: dict[str, AgentSpec] | None = None,
    *,
    default: str = "axi",
    registry: Any | None = None,
) -> ResolvedAgent:
    """Resolve an addressee to its persona + (namespaced) toolset.

    ``name=None`` → the default agent (the owner's Axi). Unknown name raises
    ``KeyError`` (callers use ``suggest`` for did-you-mean). ``registry`` (a
    SkillRegistry) scopes tools to the agent's namespace; omitted → no tools
    (C1 routes; C2 wires the run)."""
    agents = agents if agents is not None else discover_agents()
    key = (name or default).lower()
    spec = agents[key]  # KeyError on unknown — caller handles via suggest()

    from axiom.agents.persona_loader import load_agent_persona

    persona = load_agent_persona(spec.persona_dir)
    tools: list = []
    if registry is not None:
        from axiom.extensions.builtins.chat.skill_tools import skills_to_tool_definitions

        tools = skills_to_tool_definitions(registry, namespace=spec.namespace)
    return ResolvedAgent(spec=spec, persona=persona, tools=tools)


__all__ = [
    "AgentSpec",
    "ResolvedAgent",
    "discover_agents",
    "parse_addressee",
    "suggest",
    "resolve_agent",
]
