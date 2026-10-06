# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tool ID resolution against installed AEOS extensions — ADR-034 §D4.

Plans reference tool IDs (`PlanStep.tool_id`). At plan-validation time, every
referenced tool_id must resolve to a capability provided by an installed,
AEOS-conformant extension. If the tool is not installed — or is installed but
incompatible with the input classification — the plan fails closed with a
structured `ToolValidationIssue`. This is the seam that lets a Plan be
self-checking against the runtime environment.

Surface (Phase 1)
-----------------

- `ToolDescriptor` — frozen, immutable record of a resolved tool capability.
- `ToolResolutionError` — raised when a tool_id cannot be resolved.
- `ToolResolver` — Protocol; production resolvers + the in-memory test
  resolver both satisfy it.
- `StaticToolResolver` — in-memory dict-backed resolver. Tests pass
  descriptors directly; production code constructs via
  `discover_installed_tools()` and wraps the result.
- `discover_installed_tools` — walks AEOS manifests on disk, extracting every
  `[[extension.provides]]` block whose kind is in {"tool", "skill", "cmd"}
  into a `ToolDescriptor`.
- `validate_plan_tools` — walks a plan's steps, returns a tuple of
  `ToolValidationIssue` records (empty tuple = clean).

Classification semantics (Phase 1)
----------------------------------

A tool's `classification_required` is the *minimum* input classification the
tool understands. If `None` or `"unclassified"`, the tool can run on inputs
of any classification level. If `"cui"`, the tool requires inputs at CUI
or higher. Mapping uses the same level ordering as
`vega.federation.policy._LEVEL_OUTFLOW_CEILING`. Phase 2 will introduce
ceiling semantics ("this tool refuses to handle classified data"); for now
the floor semantic is sufficient — any tool that cannot safely run on
unclassified data declares its floor and the resolver enforces it.

This module performs **no I/O at import time.** `discover_installed_tools`
is the only filesystem-reading path.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Literal,
    Protocol,
    runtime_checkable,
)

from axiom.infra.toml_compat import load_toml
from axiom.vega.federation.policy import ClassificationStamp

if TYPE_CHECKING:
    from axiom.agents.pipeline.plan import Plan


# ---------------------------------------------------------------------------
# Capability-kind taxonomy (mirrors spec-aeos-0.1 §4)
# ---------------------------------------------------------------------------

ToolKind = Literal["tool", "skill", "agent", "cmd", "service", "adapter", "hook"]

# These are the kinds that participate in plan-step resolution. Agents,
# services, adapters, hooks have their own resolution paths and do not
# appear as a step's `tool_id`.
_TOOL_INVOKABLE_KINDS: frozenset[str] = frozenset({"tool", "skill", "cmd"})

# Classification level → integer rank for the floor predicate. Mirrors the
# implicit ordering already used in vega.federation.policy.
_LEVEL_RANK: dict[str, int] = {
    "unclassified": 0,
    "cui": 1,
    "secret": 2,
    "top_secret": 3,
}


def _level_rank(level: str) -> int:
    return _LEVEL_RANK.get(level, 0)


# ---------------------------------------------------------------------------
# ToolDescriptor / errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolDescriptor:
    """A resolved tool from an installed AEOS extension.

    Equality is by field-tuple so descriptors hash + compare structurally;
    the `tool_id` alone is the canonical identifier in the resolver index.
    """

    tool_id: str
    extension_name: str
    extension_version: str
    kind: ToolKind
    classification_required: ClassificationStamp | None = None
    description: str = ""


class ToolResolutionError(ValueError):
    """A tool_id could not be resolved to an installed extension."""


# ---------------------------------------------------------------------------
# Resolver protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ToolResolver(Protocol):
    """Anything that can resolve a tool_id and answer compatibility queries."""

    def resolve(self, tool_id: str) -> ToolDescriptor: ...

    def list_tools(self) -> Sequence[ToolDescriptor]: ...

    def is_compatible(
        self, tool: ToolDescriptor, classification: ClassificationStamp
    ) -> bool: ...


@dataclass
class StaticToolResolver:
    """Dict-backed resolver, populated from a list of `ToolDescriptor`.

    Tests pass descriptors directly. Production constructs the dict from
    `discover_installed_tools()` and wraps:

        resolver = StaticToolResolver.from_descriptors(discover_installed_tools())
    """

    tools_by_id: dict[str, ToolDescriptor] = field(default_factory=dict)

    @classmethod
    def from_descriptors(
        cls, descriptors: Iterable[ToolDescriptor]
    ) -> StaticToolResolver:
        index: dict[str, ToolDescriptor] = {}
        for d in descriptors:
            if d.tool_id in index:
                raise ValueError(
                    f"duplicate tool_id in descriptor list: {d.tool_id!r} "
                    f"(first from extension {index[d.tool_id].extension_name}, "
                    f"second from {d.extension_name})"
                )
            index[d.tool_id] = d
        return cls(tools_by_id=index)

    def resolve(self, tool_id: str) -> ToolDescriptor:
        try:
            return self.tools_by_id[tool_id]
        except KeyError as exc:
            raise ToolResolutionError(
                f"tool_id {tool_id!r} is not provided by any installed extension"
            ) from exc

    def list_tools(self) -> Sequence[ToolDescriptor]:
        return tuple(self.tools_by_id.values())

    def is_compatible(
        self, tool: ToolDescriptor, classification: ClassificationStamp
    ) -> bool:
        """Phase-1: tool's classification_required is a *floor* on input level.

        - None or "unclassified" → accepts any input.
        - "cui" → requires input at CUI or higher.
        - "secret" / "top_secret" → require input at that level or higher.
        """
        required = tool.classification_required
        if required is None:
            return True
        return _level_rank(classification.level) >= _level_rank(required.level)


# ---------------------------------------------------------------------------
# Plan-level validation
# ---------------------------------------------------------------------------


ToolValidationIssueKind = Literal[
    "unresolved",
    "classification_incompatible",
    "extension_unsigned",
]


@dataclass(frozen=True)
class ToolValidationIssue:
    """A single problem found while validating a plan's tool references.

    The triplet (step_id, tool_id, issue) is sufficient for downstream UX
    rendering. `message` is a human-readable rendering for logs and CLI
    error display.
    """

    step_id: str
    tool_id: str | None
    issue: ToolValidationIssueKind
    message: str


def validate_plan_tools(
    plan: Plan,
    resolver: ToolResolver,
    classification: ClassificationStamp,
) -> tuple[ToolValidationIssue, ...]:
    """Validate every step's tool_id resolves and is classification-compatible.

    Returns an empty tuple if the plan is clean. Otherwise returns one
    issue per failing step (in step order). A step with `tool_id is None`
    is skipped — not every step invokes a tool.

    The `classification` argument is the *input* classification the plan
    will run against (typically `plan.classification`); it is the data the
    tool will see. Per Phase-1 floor semantics, the tool's
    `classification_required.level` must be `<=` the input level.
    """
    issues: list[ToolValidationIssue] = []
    for step in plan.steps:
        if step.tool_id is None:
            continue
        try:
            tool = resolver.resolve(step.tool_id)
        except ToolResolutionError as exc:
            issues.append(
                ToolValidationIssue(
                    step_id=step.step_id,
                    tool_id=step.tool_id,
                    issue="unresolved",
                    message=str(exc),
                )
            )
            continue
        if not resolver.is_compatible(tool, classification):
            required = tool.classification_required
            required_level = required.level if required is not None else "unclassified"
            issues.append(
                ToolValidationIssue(
                    step_id=step.step_id,
                    tool_id=step.tool_id,
                    issue="classification_incompatible",
                    message=(
                        f"tool {step.tool_id!r} requires classification "
                        f"{required_level!r}; input classification is "
                        f"{classification.level!r}"
                    ),
                )
            )
    return tuple(issues)


# ---------------------------------------------------------------------------
# Manifest walk — production-time descriptor construction
# ---------------------------------------------------------------------------


_MANIFEST_FILENAME = "axiom-extension.toml"


def _coerce_classification(
    raw: object,
) -> ClassificationStamp | None:
    """Map a manifest's `classification = "..."` value to a ClassificationStamp.

    Phase 1 only uses the level field; export-control + proprietary regimes
    arrive in a follow-up. Unknown levels degrade to None (treated as
    unclassified-OK by the resolver).
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        level = raw.strip().lower()
        if not level or level not in _LEVEL_RANK:
            return None
        return ClassificationStamp(level=level)
    return None


def _descriptor_from_provide_block(
    extension_name: str,
    extension_version: str,
    provide: Mapping[str, object],
) -> ToolDescriptor | None:
    """Build one ToolDescriptor from a single `[[extension.provides]]` block.

    Returns None if the block's kind is not in the tool-invokable set.
    """
    kind = provide.get("kind")
    if kind not in _TOOL_INVOKABLE_KINDS:
        return None

    explicit_id = provide.get("id")
    # Local name: "name" for tool/skill, "noun" for cmd.
    local_name = provide.get("name") or provide.get("noun") or ""
    if explicit_id and isinstance(explicit_id, str):
        tool_id = explicit_id
    else:
        if not local_name or not isinstance(local_name, str):
            return None
        tool_id = f"{extension_name}.{local_name}"

    classification = _coerce_classification(provide.get("classification"))

    description = provide.get("description") or ""
    if not isinstance(description, str):
        description = ""

    # Narrow the literal type for the dataclass.
    tool_kind: ToolKind
    if kind == "tool":
        tool_kind = "tool"
    elif kind == "skill":
        tool_kind = "skill"
    elif kind == "cmd":
        tool_kind = "cmd"
    else:  # pragma: no cover — guarded above
        return None

    return ToolDescriptor(
        tool_id=tool_id,
        extension_name=extension_name,
        extension_version=extension_version,
        kind=tool_kind,
        classification_required=classification,
        description=description,
    )


def _default_extensions_root() -> Path | None:
    """Resolve the default extensions root.

    1. ``$AXIOM_HOME/extensions`` if the env var is set.
    2. Else fall back to the in-tree builtins directory
       (`src/axiom/extensions/builtins/`) so a source checkout always has
       *something* to discover.
    3. Returns None when neither path is usable.
    """
    home = os.environ.get("AXIOM_HOME")
    if home:
        candidate = Path(home).expanduser().resolve() / "extensions"
        if candidate.is_dir():
            return candidate

    builtins = Path(__file__).resolve().parents[2] / "extensions" / "builtins"
    if builtins.is_dir():
        return builtins
    return None


def discover_installed_tools(
    extensions_root: str | None = None,
) -> tuple[ToolDescriptor, ...]:
    """Walk an AEOS extensions directory and produce ToolDescriptors.

    Each immediate subdirectory of `extensions_root` is treated as an
    installed extension; the manifest at `<subdir>/axiom-extension.toml`
    is parsed. Every `[[extension.provides]]` block whose kind is in
    {"tool", "skill", "cmd"} contributes one ToolDescriptor.

    Args:
        extensions_root: Optional override. When None:
          1. `$AXIOM_HOME/extensions` if set,
          2. else the in-tree builtins directory.

    Returns:
        A flat tuple of ToolDescriptor; empty if the root is missing or
        contains no manifests. Manifests that fail to parse are skipped
        silently — discovery is best-effort and never raises on bad
        third-party data.
    """
    if extensions_root is None:
        root = _default_extensions_root()
    else:
        root = Path(extensions_root).expanduser()
    if root is None or not root.is_dir():
        return ()

    descriptors: list[ToolDescriptor] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest = child / _MANIFEST_FILENAME
        if not manifest.is_file():
            continue
        # `load_toml` returns {} on parse failure — keeps discovery
        # resilient against one-bad-extension scenarios.
        data = load_toml(manifest)
        if not data:
            continue
        ext_section = data.get("extension")
        if not isinstance(ext_section, dict):
            continue
        ext_name = ext_section.get("name")
        if not isinstance(ext_name, str) or not ext_name:
            continue
        ext_version = ext_section.get("version", "0.0.0")
        if not isinstance(ext_version, str):
            ext_version = "0.0.0"
        provides = ext_section.get("provides") or []
        if not isinstance(provides, list):
            continue
        for prov in provides:
            if not isinstance(prov, dict):
                continue
            descriptor = _descriptor_from_provide_block(
                ext_name, ext_version, prov,
            )
            if descriptor is not None:
                descriptors.append(descriptor)

    return tuple(descriptors)


__all__ = [
    "StaticToolResolver",
    "ToolDescriptor",
    "ToolKind",
    "ToolResolutionError",
    "ToolResolver",
    "ToolValidationIssue",
    "ToolValidationIssueKind",
    "discover_installed_tools",
    "validate_plan_tools",
]
