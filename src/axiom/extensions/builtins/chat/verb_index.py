# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Verb discovery index for neut chat (the companion to AEOS §4.9.4 scoping).

The chat loop's tool list is a *bounded* projection: only the capability
namespaces named in ``chat.tool_namespaces`` become tools, never "all
skills". That guard keeps the tool list small, but it also means the model
cannot see what else exists. This module is the discovery side of that
bargain: a cheap, embedding-free index over every registered capability plus
every ``[[extension.provides]]`` entry in the surfaced extensions' manifests,
searchable by name, namespace and description, reporting for each hit
whether it is already loaded, loadable (and how), or manifest-only.

It is read-only. Nothing here changes exposure; it only tells the model and
the operator what exists and which setting would expose it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from axiom.infra.capability_projection import (
    capability_to_surface_name,
    inputs_to_json_schema,
)

logger = logging.getLogger(__name__)

# The setting an operator edits to load a namespace (see tools.py).
NAMESPACES_SETTING = "chat.tool_namespaces"

KIND_CAPABILITY = "capability"
SOURCE_REGISTRY = "registry"
MANIFEST_PREFIX = "ext"

EXPOSURE_LOADED = "loaded"
EXPOSURE_AVAILABLE = "available"
EXPOSURE_MANIFEST_ONLY = "manifest-only"

DEFAULT_LIMIT = 10

_TOKEN_RE = re.compile(r"[^0-9a-z]+")

# Ranking weights: exact name > namespace > token in name > token in description.
_W_EXACT = 1000
_W_NAMESPACE = 100
_W_NAME_TOKEN = 10
_W_DESC_TOKEN = 1


@dataclass(frozen=True)
class VerbEntry:
    """One discoverable verb: a registered capability or a manifest-provided item.

    ``name`` is the capability's dotted name (``press.draft``) or, for items
    that only exist in a manifest, ``ext:<kind>:<name>``. ``tool_name`` is the
    chat/LLM surface name a capability projects to (``press__draft``); it is
    ``None`` for manifest-only items, which are not chat tools.
    """

    name: str
    namespace: str
    kind: str
    description: str
    surfaces: tuple[str, ...] = ()
    side_effects: bool | None = None
    inputs: dict[str, Any] = field(default_factory=dict, hash=False)
    source: str = SOURCE_REGISTRY
    tool_name: str | None = None

    @property
    def leaf(self) -> str:
        """The bare verb name without the ``ext:<kind>:`` prefix."""
        return self.name.rsplit(":", 1)[-1]


# ---------------------------------------------------------------------------
# Index construction
# ---------------------------------------------------------------------------


def _default_registry():
    """The process-wide SkillRegistry (what CLI verbs and MCP dispatch use)."""
    from axiom.infra.skills import default_registry

    return default_registry()


def _default_extensions() -> list:
    """The brand-scoped listing view of discovered extensions (ADR-048)."""
    from axiom.extensions.discovery import surfaced_extensions

    return surfaced_extensions()


def _registry_specs(registry) -> dict[str, Any]:
    try:
        if registry is None:
            registry = _default_registry()
        return dict(registry.specs())
    except Exception as e:
        logger.debug("Verb index: skill registry unavailable: %s", e)
        return {}


def _namespace_of(name: str) -> str:
    return name.split(".", 1)[0]


def _spec_entry(name: str, spec) -> VerbEntry:
    return VerbEntry(
        name=name,
        namespace=_namespace_of(name),
        kind=KIND_CAPABILITY,
        description=getattr(spec, "description", "") or "",
        surfaces=tuple(str(s) for s in (getattr(spec, "surfaces", None) or ())),
        side_effects=getattr(spec, "side_effects", None),
        inputs=inputs_to_json_schema(getattr(spec, "inputs", None) or {}),
        source=SOURCE_REGISTRY,
        tool_name=capability_to_surface_name(name),
    )


def _manifest_provides(ext) -> list[dict[str, Any]]:
    """The raw ``[[extension.provides]]`` entries of one extension's manifest.

    ``Extension`` keeps only typed projections (cmd/hook/safety_check), so
    the full list is re-read from the manifest, the way ``ext show`` does.
    ``load_toml`` returns ``{}`` for a missing or malformed file.
    """
    from axiom.infra.toml_compat import load_toml

    path = getattr(ext, "manifest_path", None)
    if path is None:
        return []
    section = load_toml(path).get("extension", {}) or {}
    raw = section.get("provides", []) or []
    return [prov for prov in raw if isinstance(prov, dict)]


def _coerce_side_effects(value: Any) -> bool | None:
    """Manifests declare ``side_effects`` as a bool or a comma list of effects."""
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    return text not in ("none", "false", "no")


def _manifest_entry(ext_name: str, prov: dict[str, Any]) -> tuple[str, VerbEntry] | None:
    """``(raw_name, entry)`` for a provides block, or ``None`` if it has no verb name."""
    kind = str(prov.get("kind", "") or "").strip()
    raw_name = str(prov.get("name") or prov.get("noun") or "").strip()
    if not kind or not raw_name:
        return None
    namespace = (
        _namespace_of(raw_name) if "." in raw_name else (raw_name if kind == "cmd" else ext_name)
    )
    surfaces = prov.get("surfaces") or ()
    if isinstance(surfaces, str):
        surfaces = (surfaces,)
    return raw_name, VerbEntry(
        name=f"{MANIFEST_PREFIX}:{kind}:{raw_name}",
        namespace=namespace,
        kind=kind,
        description=str(prov.get("description", "") or ""),
        surfaces=tuple(str(s) for s in surfaces),
        side_effects=_coerce_side_effects(prov.get("side_effects")),
        inputs={},
        source=ext_name,
        tool_name=None,
    )


def build_verb_index(registry=None, extensions: Iterable | None = None) -> list[VerbEntry]:
    """Every discoverable verb: registry capabilities first (sorted), then manifests.

    A capability that also appears as a manifest entry keeps the registry
    record. Never raises: an unreadable registry yields no capabilities and a
    bad extension is logged and skipped.
    """
    entries: list[VerbEntry] = []
    seen: set[str] = set()

    specs = _registry_specs(registry)
    for name in sorted(specs):
        entries.append(_spec_entry(name, specs[name]))
        seen.add(name)

    if extensions is None:
        try:
            extensions = _default_extensions()
        except Exception as e:
            logger.debug("Verb index: extension discovery unavailable: %s", e)
            extensions = []

    for ext in extensions:
        ext_name = str(getattr(ext, "name", "") or "?")
        try:
            provides = _manifest_provides(ext)
        except Exception as e:
            logger.debug("Verb index: skipping extension %s: %s", ext_name, e)
            continue
        for prov in provides:
            parsed = _manifest_entry(ext_name, prov)
            if parsed is None:
                continue
            raw_name, entry = parsed
            if raw_name in seen or entry.name in seen:
                continue
            entries.append(entry)
            seen.add(entry.name)

    return entries


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.split(text.lower()) if t]


def _score(entry: VerbEntry, query: str, tokens: list[str]) -> int:
    leaf = entry.leaf.lower()
    score = 0
    if query in (leaf, entry.name.lower(), (entry.tool_name or "").lower()):
        score += _W_EXACT
    namespace = entry.namespace.lower()
    if any(t == namespace for t in tokens):
        score += _W_NAMESPACE
    score += _W_NAME_TOKEN * sum(1 for t in tokens if t in leaf)
    desc_tokens = set(_tokens(entry.description))
    score += _W_DESC_TOKEN * sum(1 for t in tokens if t in desc_tokens)
    return score


def search_verbs(
    index: Iterable[VerbEntry], query: str, *, limit: int = DEFAULT_LIMIT
) -> list[VerbEntry]:
    """Rank ``index`` against ``query`` without embeddings.

    Exact name (dotted or surface form) outranks a namespace hit, which
    outranks a token appearing in the name, which outranks a token appearing
    in the description. Ties keep index order; only positive scores match.
    """
    query = (query or "").strip().lower()
    tokens = _tokens(query)
    if not tokens or limit <= 0:
        return []
    scored: list[tuple[int, int, VerbEntry]] = []
    for position, entry in enumerate(index):
        score = _score(entry, query, tokens)
        if score > 0:
            scored.append((-score, position, entry))
    scored.sort(key=lambda item: item[:2])
    return [entry for _, _, entry in scored[:limit]]


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------


def exposure(entry: VerbEntry, namespaces: Iterable[str]) -> str:
    """``loaded`` (already a chat tool), ``available`` (a capability whose
    namespace is not configured), or ``manifest-only`` (not a capability)."""
    if entry.kind != KIND_CAPABILITY or entry.source != SOURCE_REGISTRY:
        return EXPOSURE_MANIFEST_ONLY
    loaded = {str(ns).strip() for ns in (namespaces or ())}
    return EXPOSURE_LOADED if entry.namespace in loaded else EXPOSURE_AVAILABLE


def how_to_load(entry: VerbEntry, namespaces: Iterable[str]) -> str | None:
    """Operator-facing hint for an entry that is not a loaded chat tool."""
    state = exposure(entry, namespaces)
    if state == EXPOSURE_LOADED:
        return None
    if state == EXPOSURE_AVAILABLE:
        return (
            f'Add "{entry.namespace}" to the {NAMESPACES_SETTING} setting; '
            f"it then appears as the chat tool {entry.tool_name}."
        )
    return (
        f"Not a registered capability: declared by extension {entry.source} "
        f"as kind={entry.kind}. Reach it through that extension's own surface "
        "(for example its CLI noun); it cannot be loaded as a chat tool."
    )


def describe(entry: VerbEntry, namespaces: Iterable[str]) -> dict[str, Any]:
    """The tool-result row for one entry."""
    return {
        "name": entry.name,
        "namespace": entry.namespace,
        "kind": entry.kind,
        "description": entry.description,
        "tool_name": entry.tool_name,
        "exposure": exposure(entry, namespaces),
        "how_to_load": how_to_load(entry, namespaces),
    }


__all__ = [
    "DEFAULT_LIMIT",
    "EXPOSURE_AVAILABLE",
    "EXPOSURE_LOADED",
    "EXPOSURE_MANIFEST_ONLY",
    "NAMESPACES_SETTING",
    "VerbEntry",
    "build_verb_index",
    "describe",
    "exposure",
    "how_to_load",
    "search_verbs",
]
