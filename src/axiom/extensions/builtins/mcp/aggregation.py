# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Aggregation registry for the built-in root MCP server.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §6.

The registry walks the discovered extension list, parses every
``[extension.mcp]`` block, merges per-extension contributions with the
always-on ``PlatformPrimitives``, and produces a deterministic
``MCPSurface``. The surface's ``content_hash`` is the drift signal:
identical inputs always produce the same hash; M-O monitors this for
"surface stale" alerts.

Collision rules (spec §6.4):
- Platform-tool names always win — extension entries are dropped with a warning.
- Two extensions colliding on the same ``mcp_name`` resolve
  lexicographic-first (deterministic via ``sorted(extensions, key=name)``);
  the loser is dropped with a warning.
"""

from __future__ import annotations

import hashlib
import json
import logging
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mcp.types import Prompt, Resource, Tool

from axiom.extensions.builtins.mcp.manifest_schema import (
    MCPExtensionConfig,
    MCPToolDecl,
    parse_mcp_block,
)
from axiom.extensions.builtins.mcp.platform_primitives import (
    PLATFORM_TOOL_NAMES,
    PlatformPrimitives,
)
from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
from axiom.extensions.contracts import Extension
from axiom.infra.skills import SkillContext, SkillRegistry, default_registry

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provenance + contribution dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContributionSource:
    """Provenance: which contributor produced which entries."""

    kind: str              # "platform" | "extension"
    name: str              # "platform" or extension name
    tool_names: tuple[str, ...] = ()
    resource_names: tuple[str, ...] = ()
    prompt_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtensionContribution:
    """One extension's parsed contribution to the surface."""

    extension_name: str
    config: MCPExtensionConfig
    tools: list[Tool]
    resources: list[Resource]
    prompts: list[Prompt]
    dispatch: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = field(
        default_factory=dict
    )


# ---------------------------------------------------------------------------
# MCPSurface — the immutable, content-hashed merged surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCPSurface:
    """Immutable, content-addressed merged MCP surface."""

    tools: list[Tool]
    resources: list[Resource]
    prompts: list[Prompt]
    dispatch: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]
    content_hash: str
    generated_at: datetime
    sources: list[ContributionSource]

    def handler_source(self, tool_name: str) -> str | None:
        """Return the contributor name that owns the given tool's handler."""
        for src in self.sources:
            if tool_name in src.tool_names:
                return "platform" if src.kind == "platform" else src.name
        return None

    def to_dict(self) -> dict[str, Any]:
        """Render to a plain dict for cache persistence."""
        return {
            "content_hash": self.content_hash,
            "generated_at": self.generated_at.isoformat(),
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": getattr(t, "inputSchema", None),
                }
                for t in self.tools
            ],
            "resources": [
                {"name": r.name, "uri": str(r.uri), "mime_type": r.mimeType}
                for r in self.resources
            ],
            "prompts": [
                {"name": p.name, "description": p.description}
                for p in self.prompts
            ],
            "sources": [
                {
                    "kind": s.kind,
                    "name": s.name,
                    "tool_names": list(s.tool_names),
                    "resource_names": list(s.resource_names),
                    "prompt_names": list(s.prompt_names),
                }
                for s in self.sources
            ],
        }


# ---------------------------------------------------------------------------
# AggregationRegistry
# ---------------------------------------------------------------------------


class AggregationRegistry:
    """Builds an ``MCPSurface`` from platform primitives + extensions.

    Construction is cheap (just stores the inputs); the work happens in
    :meth:`build`.
    """

    def __init__(
        self,
        extensions: list[Extension] | None = None,
        *,
        registry: SkillRegistry | None = None,
        ctx_factory: Callable[[], SkillContext] | None = None,
    ) -> None:
        self._extensions = list(extensions or [])
        # Registry-driven tool surface (ADR-073). Defaults to the process
        # registry; until capabilities opt in via surfaces=["mcp"], this
        # contributes nothing, so existing manifest-only behavior is unchanged.
        self._registry = registry
        self._ctx_factory = ctx_factory

    # ------------------------------------------------------------------
    # Discovery convenience constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_node(cls) -> AggregationRegistry:
        """Build a registry from the node's discovered extensions."""
        from axiom.extensions.discovery import discover_extensions

        return cls(extensions=list(discover_extensions()))

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self) -> MCPSurface:
        platform = PlatformPrimitives.contribution()

        # Start with the platform contribution: it always sorts first
        # and cannot be shadowed by an extension.
        merged_tools: list[Tool] = list(platform.tools)
        merged_dispatch: dict[
            str, Callable[[dict[str, Any]], Awaitable[Any]]
        ] = dict(platform.dispatch)
        platform_tool_names = set(PLATFORM_TOOL_NAMES)
        seen_tool_names: set[str] = set(platform_tool_names)

        sources: list[ContributionSource] = [
            ContributionSource(
                kind="platform",
                name="platform",
                tool_names=tuple(platform_tool_names),
            )
        ]

        merged_resources: list[Resource] = []
        merged_prompts: list[Prompt] = []

        # Registry-driven contribution (ADR-073): capabilities that opt into
        # surfaces=["mcp"] become tools via the shared projector, dispatched
        # through SkillRegistry.invoke. Sorts after platform, before the legacy
        # manifest path — so a manifest tool that duplicates a registry-backed
        # capability is dropped (registry wins). Inert until a capability
        # declares surfaces=["mcp"]; platform tools still cannot be shadowed.
        registry = self._registry if self._registry is not None else default_registry()
        skill_contrib = skill_tool_contribution(
            registry, ctx_factory=self._ctx_factory or _default_ctx_factory(registry)
        )
        registry_tool_names: list[str] = []
        for tool in skill_contrib.tools:
            if tool.name in platform_tool_names:
                warnings.warn(
                    f"mcp: capability tool {tool.name!r} collides with a platform "
                    "tool; registry entry dropped (platform wins)",
                    stacklevel=2,
                )
                continue
            merged_tools.append(tool)
            merged_dispatch[tool.name] = skill_contrib.dispatch[tool.name]
            seen_tool_names.add(tool.name)
            registry_tool_names.append(tool.name)
        if registry_tool_names:
            sources.append(
                ContributionSource(
                    kind="registry",
                    name="registry",
                    tool_names=tuple(registry_tool_names),
                )
            )

        # Deterministic order regardless of input ordering.
        for ext in sorted(self._extensions, key=lambda e: e.name):
            if not getattr(ext, "enabled", True):
                continue
            try:
                cfg = parse_mcp_block(ext.manifest_path)
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("mcp: failed to parse %s: %s", ext.manifest_path, exc)
                continue
            if cfg is None or not cfg.enabled:
                continue

            ext_tools, ext_dispatch, ext_tool_names = _build_extension_tool_surface(
                ext, cfg, seen_tool_names, platform_tool_names
            )
            if not ext_tools and not cfg.resources and not cfg.prompts:
                continue

            merged_tools.extend(ext_tools)
            merged_dispatch.update(ext_dispatch)
            seen_tool_names.update(ext_tool_names)

            sources.append(
                ContributionSource(
                    kind="extension",
                    name=ext.name,
                    tool_names=tuple(ext_tool_names),
                )
            )

        content_hash = _compute_content_hash(merged_tools, merged_resources, merged_prompts)
        return MCPSurface(
            tools=merged_tools,
            resources=merged_resources,
            prompts=merged_prompts,
            dispatch=merged_dispatch,
            content_hash=content_hash,
            generated_at=datetime.now(UTC),
            sources=sources,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_ctx_factory(registry: SkillRegistry) -> Callable[[], SkillContext]:
    """Build a headless SkillContext factory for registry-backed MCP dispatch.

    Anchored at the user state dir (parity with platform primitives); no
    interactive prompt (headless) — skills must handle ``user_prompt=None``.
    """

    def factory() -> SkillContext:
        from axiom.infra.paths import get_user_state_dir

        return SkillContext(
            registry=registry,
            state_dir=get_user_state_dir(),
            logger=log,
        )

    return factory


def _build_extension_tool_surface(
    ext: Extension,
    cfg: MCPExtensionConfig,
    seen_tool_names: set[str],
    platform_tool_names: set[str],
) -> tuple[list[Tool], dict[str, Callable[[dict[str, Any]], Awaitable[Any]]], list[str]]:
    """Build this extension's tool list + dispatch, applying collision rules.

    Returns ``(tools, dispatch, ordered_tool_names)``. Collisions are
    surfaced via :func:`warnings.warn`.
    """
    tools: list[Tool] = []
    dispatch: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {}
    ordered_names: list[str] = []

    for tool_decl in cfg.tools:
        mcp_name = tool_decl.mcp_name
        if mcp_name in platform_tool_names:
            warnings.warn(
                f"mcp: extension {ext.name!r} attempted to shadow platform tool "
                f"{mcp_name!r}; entry dropped (rename mcp_name to resolve)",
                stacklevel=2,
            )
            continue
        if mcp_name in seen_tool_names:
            warnings.warn(
                f"mcp: extension {ext.name!r} declares tool {mcp_name!r} which "
                "is already provided by an earlier (lexicographic-first) extension; "
                "entry dropped",
                stacklevel=2,
            )
            continue
        handler = _resolve_handler(ext.name, tool_decl)
        if tool_decl.hidden:
            # Loaded into dispatch (still callable by name) but not advertised.
            dispatch[mcp_name] = handler
            continue
        tools.append(
            Tool(
                name=mcp_name,
                description=tool_decl.description_override
                or f"{ext.name}: {tool_decl.name}",
                inputSchema={"type": "object", "additionalProperties": True},
            )
        )
        dispatch[mcp_name] = handler
        ordered_names.append(mcp_name)
    return tools, dispatch, ordered_names


def _resolve_handler(
    extension_name: str, tool_decl: MCPToolDecl
) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    """Build a callable for ``tool_decl`` — real entry if declared, stub otherwise.

    The matching ``[[extension.provides]]`` block's ``entry`` field is a
    dotted ``module:funcname`` path. When present, the resulting handler
    imports lazily on first call (so a broken extension only breaks the
    one tool, not the whole surface), accepts a single ``args`` dict, and
    coerces sync handlers transparently. When ``entry`` is empty we
    return the Phase-1 stub so the surface stays callable on a partial
    rollout.
    """
    if tool_decl.entry and ":" in tool_decl.entry:
        return _entry_handler(extension_name, tool_decl)
    return _stub_handler(extension_name, tool_decl.name)


def _entry_handler(
    extension_name: str, tool_decl: MCPToolDecl
) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    """Lazily-resolved handler that imports and invokes ``entry`` on call."""
    import importlib

    module_path, func_name = tool_decl.entry.rsplit(":", 1)

    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            mod = importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 — surface import failure cleanly
            return {
                "error": (
                    f"could not import {module_path!r} for "
                    f"{extension_name}.{tool_decl.name}: "
                    f"{type(exc).__name__}: {exc}"
                )
            }
        try:
            func = getattr(mod, func_name)
        except AttributeError:
            return {
                "error": (
                    f"module {module_path!r} has no attribute {func_name!r} "
                    f"(declared as entry for {extension_name}.{tool_decl.name})"
                )
            }
        try:
            result = func(args)
        except Exception as exc:  # noqa: BLE001 — translate every error
            return {"error": f"{type(exc).__name__}: {exc}"}
        # Awaitable transparency: handlers may be sync OR async.
        if hasattr(result, "__await__"):
            result = await result
        if result is None:
            return {"ok": True}
        return result

    return _handler


def _stub_handler(
    extension_name: str, tool_name: str
) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    """Fallback when an extension declares a tool without an ``entry``.

    This used to be a Phase-1 stub for every extension; in Phase 2 it
    only fires when an extension declares ``[[extension.mcp.tool]]``
    without a matching ``[[extension.provides]]`` carrying an ``entry``.
    Lint catches the partial declaration; the runtime stays callable.
    """

    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "extension": extension_name,
            "tool": tool_name,
            "args": args,
            "note": (
                "stub: this tool's [[extension.provides]] entry is missing — "
                "add `entry = \"module:func\"` to the matching provides block."
            ),
        }

    return _handler


def _compute_content_hash(
    tools: list[Tool], resources: list[Resource], prompts: list[Prompt]
) -> str:
    """SHA-256 of a stable JSON projection of the surface."""
    payload = {
        "tools": sorted(
            [
                {"name": t.name, "description": t.description}
                for t in tools
            ],
            key=lambda d: d["name"],
        ),
        "resources": sorted(
            [{"name": r.name, "uri": str(r.uri)} for r in resources],
            key=lambda d: d["name"],
        ),
        "prompts": sorted(
            [{"name": p.name} for p in prompts],
            key=lambda d: d["name"],
        ),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


__all__ = [
    "AggregationRegistry",
    "ContributionSource",
    "ExtensionContribution",
    "MCPSurface",
]
