# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PRESS skills — ADR-056 thin wrappers around the publishing engine."""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import (
    detect_version,
    do_standard,
    draft,
    mirror_sync,
    next_filename,
    publish,
    scope_for_source,
    standards,
)


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"

_NAMESPACE = "press"

# Legacy short-form registrations (PR-2 will lift these into SkillSpec).
_SKILLS = {
    "scope_for_source": scope_for_source.run,
    "next_filename":    next_filename.run,
    "detect_version":   detect_version.run,
    "do_standard":      do_standard.run,
}

# ADR-063 exemplars — three PRESS skills get the full SkillSpec metadata
# treatment in PR-1 so the SKILL.md generator has something to chew on.
_SPECS: tuple[SkillSpec, ...] = (
    SkillSpec(
        name="press.draft",
        fn=draft.run,
        description="Render a draft artifact locally (no upload).",
        long_description=(
            "Resolves the source path, instantiates the PublisherEngine, "
            "and generates the draft output in the source's scope. No "
            "upload to OneDrive or other providers happens here — `press.draft` "
            "is the dry-run companion to `press.publish` and is what authors "
            "iterate against locally before flipping the publish bit."
        ),
        inputs={"source": "Path"},
        allowed_tools=(),
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=True,  # writes a draft artifact to disk
        idempotent=True,
    ),
    SkillSpec(
        name="press.publish",
        fn=publish.run,
        description="Draft + upload + notify via the event bus.",
        long_description=(
            "End-to-end publish: builds the artifact, uploads to the "
            "configured provider, and emits an event on the platform "
            "EventBus so HERALD (and any other agent_bridge consumer) "
            "can broadcast the announcement. Per ADR-060 this skill never "
            "imports a NotificationProvider directly — routing happens "
            "through the bus."
        ),
        inputs={"source": "Path", "scope": "str | None"},
        allowed_tools=(),
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=True,  # uploads + emits announcement on the bus
    ),
    SkillSpec(
        name="press.mirror_add",
        fn=mirror_sync.add,
        description="Register a local mirror of a remote-editor document.",
        long_description=(
            "Names a document that lives in a remote web editor and is "
            "mirrored to a local file. The registry entry carries the local "
            "path, the share URL, and whether sync events are annotated as "
            "git commits when the mirror lives inside a repository."
        ),
        inputs={"local": "Path", "url": "str", "name": "str | None",
                "git_annotate": "bool | None"},
        allowed_tools=(),
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=True,  # writes the mirror registry
        idempotent=True,
    ),
    SkillSpec(
        name="press.mirror_sync",
        fn=mirror_sync.sync,
        description="Reconcile registered mirrors with their remote editors.",
        long_description=(
            "One directional pass per mirror: the remote editor is canonical, "
            "the local file follows, and local edits push with the version "
            "they expect. With watch=true the pass also detects a remote "
            "save that matches an old base — a stale editor session flushing "
            "its buffer — and repairs it instead of following it."
        ),
        inputs={"name": "str | None", "watch": "bool | None"},
        allowed_tools=(),
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=True,  # reads and writes the remote document + mirror
    ),
    SkillSpec(
        name="press.mirror_status",
        fn=mirror_sync.status,
        description="Show registered mirrors and their last-known state.",
        long_description=(
            "Reads the mirror registry and each mirror's persisted sync "
            "state — no remote call is made."
        ),
        inputs={},
        allowed_tools=(),
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=False,  # read-only → auto-approve
        idempotent=True,
    ),
    SkillSpec(
        name="press.standards",
        fn=standards.run,
        description="List the registered PRESS standards bundles.",
        long_description=(
            "Returns the standards bundles available to PRESS — name, "
            "description, category, version, tags, and the underlying "
            f"skill steps. Used by `{_brand_cli()} publish standards list` and by "
            "agent reasoning loops that need to enumerate which "
            "publishing recipes are installed."
        ),
        inputs={"category": "str | None"},
        allowed_tools=(),
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=False,  # read-only enumeration → auto-approve
        idempotent=True,
    ),
)


def bind(registry: SkillRegistry) -> None:
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register(name, fn)
    for spec in _SPECS:
        if registry.has(spec.name):
            continue
        registry.register_skill(spec)


def bind_default() -> SkillRegistry:
    reg = default_registry()
    bind(reg)
    return reg


__all__ = ["bind", "bind_default"]
