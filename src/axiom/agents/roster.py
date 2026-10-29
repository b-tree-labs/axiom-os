# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The agent roster: one declaration, every surface derived from it.

A person who writes "does RIVET use KEEP for expiring keys?" expects the
assistant to know already that RIVET is the release engineer and KEEP the
secret steward. That knowledge used to exist only as bare names, so a harness
knew THAT an agent existed and nothing about what it was for.

Each agent now declares itself once, in its own extension manifest, on the
``kind = "agent"`` provides entry that already named its persona::

    role = "secret steward"
    domain = "secrets, credentials, capability tokens and their expiry"
    namespaces = ["vault", "secrets"]      # the skill namespaces it owns
    also_owns = ["secrets"]                # extensions beyond its own
    asks = ["which keys expire soon", "rotate this credential"]

Everything else reads that record: ``agents.roster`` and ``axi agents roster``,
the MCP handshake's ``instructions``, the ``[KEEP]`` tag on every tool
description, the ``axiom://agents/roster`` resource, the delegated toolset, the
chat identity layer's internal-name list, and the instruction-file write-back
for a harness whose MCP server is down. Adding an agent is one manifest edit.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Tag on a tool no agent owns: a platform primitive any agent may use.
PLATFORM_TAG = "[platform]"

#: The MCP resource that serves the full records.
ROSTER_RESOURCE_URI = "axiom://agents/roster"

_MCP_PREFIX = "axiom_"


@dataclass(frozen=True)
class AgentRecord:
    """Who an agent is, what it owns, and how to reach it."""

    handle: str  # "keep" — what a person types after `@`
    display: str  # "KEEP" — how it is written in prose and in tool tags
    role: str  # "secret steward"
    domain: str  # "secrets, credentials, capability tokens and their expiry"
    extension: str  # the extension hosting the persona ("vault")
    extensions: tuple[str, ...]  # every extension it owns, own first
    namespaces: tuple[str, ...]  # every skill namespace it owns, own first
    asks: tuple[str, ...]  # example requests
    description: str = ""

    @property
    def tag(self) -> str:
        return f"[{self.display}]"

    @property
    def tool_prefixes(self) -> tuple[str, ...]:
        return tuple(f"{_MCP_PREFIX}{ns}__" for ns in self.namespaces)

    def to_dict(self, tools: list[str] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "handle": self.handle,
            "address": f"@{self.handle}",
            "display": self.display,
            "role": self.role,
            "domain": self.domain,
            "extension": self.extension,
            "extensions": list(self.extensions),
            "namespaces": list(self.namespaces),
            "asks": list(self.asks),
            "tag": self.tag,
            "description": self.description,
        }
        if tools is not None:
            out["tools"] = sorted(tools)
        return out


def _builtins_root() -> Path:
    return Path(__file__).resolve().parents[1] / "extensions" / "builtins"


def _record(spec: Any) -> AgentRecord:
    decl = dict(getattr(spec, "declaration", ()) or ())
    own = str(getattr(spec, "extension", "") or "")
    extensions = [own] if own else []
    for ext in decl.get("also_owns") or ():
        if str(ext) not in extensions:
            extensions.append(str(ext))
    return AgentRecord(
        handle=spec.name,
        display=spec.display,
        role=str(decl.get("role") or "").strip(),
        domain=str(decl.get("domain") or "").strip(),
        extension=own,
        extensions=tuple(extensions),
        namespaces=tuple(getattr(spec, "namespaces", ()) or (spec.namespace,)),
        asks=tuple(str(a) for a in (decl.get("asks") or ())),
        description=str(decl.get("description") or "").strip(),
    )


def load_roster(*, builtins_only: bool = False, agents: dict | None = None) -> list[AgentRecord]:
    """Every addressable agent's record, sorted by handle.

    ``builtins_only`` reads the platform's own tree and nothing installed on
    this node: the answer is fixed for a given release, which is what a
    byte-stable surface (the handshake string, the write-back block) needs.
    """
    from axiom.extensions.builtins.connect.agent_router import discover_agents

    if agents is None:
        agents = discover_agents(root=_builtins_root()) if builtins_only else discover_agents()
    return [_record(agents[k]) for k in sorted(agents)]


@functools.lru_cache(maxsize=1)
def builtin_roster() -> tuple[AgentRecord, ...]:
    """The platform's own roster, read once per process."""
    try:
        return tuple(load_roster(builtins_only=True))
    except Exception:  # noqa: BLE001 — a roster never blocks the surface that shows it
        return ()


# ---------------------------------------------------------------------------
# Ownership: which agent a tool belongs to
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerIndex:
    by_namespace: dict[str, AgentRecord]
    by_extension: dict[str, AgentRecord]

    def owner_of(self, tool_name: str, *, extension: str | None = None) -> AgentRecord | None:
        """The agent that owns ``tool_name``, or None for a platform tool.

        The declared namespace decides first (``axiom_vault__resolve`` is
        KEEP's because KEEP declares ``vault``); the contributing extension
        decides a tool whose namespace no agent declared
        (``axiom_hygiene_ext__status`` comes from TIDY's extension).
        """
        core = tool_name[len(_MCP_PREFIX):] if tool_name.startswith(_MCP_PREFIX) else tool_name
        ns = core.split("__", 1)[0].split(".", 1)[0].lower()
        rec = self.by_namespace.get(ns)
        if rec is None and extension:
            rec = self.by_extension.get(extension)
        return rec


def owner_index(records: list[AgentRecord] | tuple[AgentRecord, ...]) -> OwnerIndex:
    by_ns: dict[str, AgentRecord] = {}
    by_ext: dict[str, AgentRecord] = {}
    for r in records:
        for ns in r.namespaces:
            by_ns.setdefault(ns, r)
        for ext in r.extensions:
            by_ext.setdefault(ext, r)
    return OwnerIndex(by_namespace=by_ns, by_extension=by_ext)


def tag_description(description: str, tag: str) -> str:
    """``"[KEEP] <description>"``. Idempotent: an already-tagged one is kept."""
    text = (description or "").strip()
    if text.startswith(tag + " ") or text == tag:
        return text
    return f"{tag} {text}".rstrip()


# ---------------------------------------------------------------------------
# Renderings
# ---------------------------------------------------------------------------


def roster_lines(records: list[AgentRecord] | tuple[AgentRecord, ...]) -> list[str]:
    """``KEEP (secret steward): secrets, …`` — one compact line per agent."""
    return [f"{r.display} ({r.role}): {r.domain}" for r in records if r.role or r.domain]


def render_markdown_roster(records: list[AgentRecord] | tuple[AgentRecord, ...]) -> str:
    """The short roster section for the instruction-file write-back.

    No timestamps and no install-specific content, so a re-render is
    byte-identical and a no-op sync writes nothing.
    """
    lines = roster_lines(records)
    if not lines:
        return ""
    return "\n".join(
        [
            "### Axiom agents",
            "",
            "Named specialists. When a person names one (`KEEP`, `@rivet`), it means "
            "this agent: consult it with `axiom_agents__ask`, or pass an `@handle` "
            "message verbatim to `axiom_agents__address`. Its tools are tagged with "
            "its name.",
            "",
            *(f"- {line}" for line in lines),
        ]
    )


__all__ = [
    "AgentRecord",
    "OwnerIndex",
    "PLATFORM_TAG",
    "ROSTER_RESOURCE_URI",
    "builtin_roster",
    "load_roster",
    "owner_index",
    "render_markdown_roster",
    "roster_lines",
    "tag_description",
]
