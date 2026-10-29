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
    namespace: str     # primary skill namespace (== name)
    #: Every skill namespace the agent's toolset is bounded to: its own name
    #: plus the ``namespaces`` its manifest declares. KEEP's skills are
    #: ``vault.*`` and ``secrets.*``; bounding KEEP to ``keep.*`` alone handed
    #: it an empty toolset, so asking KEEP anything ran a persona that could
    #: not look.
    namespaces: tuple[str, ...] = ()
    #: The manifest's ``kind = "agent"`` declaration (role, domain, asks, …),
    #: read once here so every surface derives from the same record. Empty
    #: for a persona-only agent that declares nothing.
    declaration: tuple[tuple[str, Any], ...] = ()
    extension: str = ""  # the extension directory that hosts the persona


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
    """Discover agents by their ``<ext>/agents/<name>/persona.md`` convention.

    With no ``root`` the roster is the builtins tree PLUS every extension the
    node discovers (project, user and installed-package extensions). An
    extension outside the builtins tree declares its agent with
    ``[[extension.provides]] kind = "agent"`` and a ``persona`` path relative
    to the extension root; the ``agents/<name>/persona.md`` convention is
    honoured under the extension root as well. Builtins win on a name clash.
    """
    explicit = root is not None
    root = root or _builtins_root()
    found: dict[str, AgentSpec] = {}
    for ext_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        _scan_ext_dir(ext_dir, found)
    if not explicit:
        for ext_root in _external_extension_roots():
            _scan_ext_dir(ext_root, found, declared=True)
    return found


def discover_agents_in(ext_roots: list[Path | None]) -> dict[str, AgentSpec]:
    """The builtins roster plus agents declared by the given extension roots.

    For a caller that already holds its extension list (the MCP aggregator),
    so the roster describes the same install without a second node scan.
    """
    found = discover_agents(root=_builtins_root())
    builtins = _builtins_root().resolve()
    for root in ext_roots:
        if root is None:
            continue
        root = Path(root)
        if root.is_dir() and builtins not in root.resolve().parents:
            _scan_ext_dir(root, found, declared=True)
    return found


def _scan_ext_dir(ext_dir: Path, found: dict[str, AgentSpec], *, declared: bool = False) -> None:
    agents_dir = ext_dir / "agents"
    if agents_dir.is_dir():
        for agent_dir in sorted(p for p in agents_dir.iterdir() if p.is_dir()):
            if (agent_dir / "persona.md").exists():
                _add(found, agent_dir.name.lower(), agent_dir)
    if not declared:
        return
    manifest = ext_dir / "axiom-extension.toml"
    if not manifest.is_file():
        return
    try:
        from axiom.infra.toml_compat import tomllib

        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a bad manifest is reported by lint, not by the roster
        return
    for prov in (data.get("extension") or {}).get("provides") or []:
        if prov.get("kind") != "agent" or not prov.get("name") or not prov.get("persona"):
            continue
        persona = (ext_dir / str(prov["persona"])).resolve()
        if persona.is_file() and ext_dir.resolve() in persona.parents:
            _add(found, str(prov["name"]).lower(), persona.parent)


def _add(found: dict[str, AgentSpec], name: str, persona_dir: Path) -> None:
    if name in found:
        return
    ext_dir = _ext_dir_of(persona_dir)
    decl = _declaration(ext_dir, name) if ext_dir is not None else {}
    namespaces = [name]
    for ns in decl.get("namespaces") or ():
        ns = str(ns).strip().lower()
        if ns and ns not in namespaces:
            namespaces.append(ns)
    found[name] = AgentSpec(
        name=name,
        display=_DISPLAY_OVERRIDES.get(name, name.upper()),
        persona_dir=persona_dir,
        namespace=name,
        namespaces=tuple(namespaces),
        declaration=tuple(sorted(decl.items())),
        extension=ext_dir.name if ext_dir is not None else "",
    )


def _ext_dir_of(persona_dir: Path) -> Path | None:
    """The extension root above a persona dir (the one holding the manifest)."""
    for parent in (persona_dir, *persona_dir.parents):
        if (parent / "axiom-extension.toml").is_file():
            return parent
    return None


def _declaration(ext_dir: Path, name: str) -> dict[str, Any]:
    """The ``kind = "agent"`` provides entry named ``name`` in ``ext_dir``."""
    try:
        from axiom.infra.toml_compat import tomllib

        data = tomllib.loads((ext_dir / "axiom-extension.toml").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a bad manifest is reported by lint, not by the roster
        return {}
    for prov in (data.get("extension") or {}).get("provides") or []:
        if prov.get("kind") == "agent" and str(prov.get("name", "")).lower() == name:
            return {k: (tuple(v) if isinstance(v, list) else v) for k, v in prov.items()}
    return {}


def _external_extension_roots() -> list[Path]:
    """Roots of discovered extensions that do not live in the builtins tree."""
    try:
        from axiom.extensions.discovery import discover_extensions

        builtins = _builtins_root().resolve()
        return [e.root for e in discover_extensions() if builtins not in e.root.resolve().parents]
    except Exception:  # noqa: BLE001 - discovery must never break routing
        return []


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

        for ns in spec.namespaces or (spec.namespace,):
            tools.extend(skills_to_tool_definitions(registry, namespace=ns))
    return ResolvedAgent(spec=spec, persona=persona, tools=tools)


__all__ = [
    "AgentSpec",
    "ResolvedAgent",
    "discover_agents",
    "discover_agents_in",
    "parse_addressee",
    "suggest",
    "resolve_agent",
]
