# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""data_platform skills — invocable through the platform SkillRegistry.

Each skill is a plain Python function::

    def run(params: dict, ctx: SkillContext) -> SkillResult: ...

Registration into the default registry happens at first-use via
:func:`bind_default`. Tests build a clean ``SkillRegistry()`` directly
and call :func:`bind` against it; the CLI module's main() calls
:func:`bind_default` once before dispatching.

Naming: skills are namespaced under ``data`` (the extension's CLI
noun). So ``data.install``, ``data.diagnose``, ``data.ingest``, etc.

A skill maps to its CLI verb 1:1 — that's the principle ADR-056
locks in. ``axi data install`` → ``data.install``; same skill called
from any agent persona.
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import (
    activity,
    backfill_urls,
    backup,
    backup_validate,
    diagnose,
    enroll,
    ingest,
    ingest_push,
    install,
    preflight,
    refresh,
    register,
    troubleshoot,
    unregister,
)
from . import (
    list_connectors as _list_mod,
)

_NAMESPACE = "data"

_SKILLS = {
    "install": install.run,
    "diagnose": diagnose.run,
    "ingest": ingest.run,
    "refresh": refresh.run,
    "enroll": enroll.run,
    "register": register.run,
    "unregister": unregister.run,
    "list": _list_mod.run,
    "troubleshoot": troubleshoot.run,
    "preflight": preflight.run,
    "activity": activity.run,
    "backfill-urls": backfill_urls.run,
    "backup": backup.run,
    "backup_validate": backup_validate.run,
}

# Skills exposed via MCP / HTTP but NOT as a CLI verb. ``data.ingest_push``
# is the push front door (inline bytes from an egress agent / MCP client),
# the peer of the HTTP ``POST /ingest`` endpoint — it shares the IngestSink
# core. It is kept out of ``verbs()`` because a CLI verb taking inline
# content on the command line is not a usable surface; the pull-side
# ``data.ingest`` verb covers the CLI.
_NON_CLI_SKILLS = {
    "ingest_push": ingest_push.run,
}


#: Declarative metadata per verb (ADR-063). Registering with a spec is what
#: makes a capability projectable: one definition becomes the CLI verb, the
#: MCP tool, the agent-facing function and the generated SKILL.md
#: (capability_projection, ADR-072). A verb registered without one exists at
#: the terminal and nowhere else — which also means it cannot be filtered per
#: tenant, since a surface cannot scope what it cannot see.
#:
#: ``inputs`` is shape-only and documentation-facing; a trailing ``!`` marks a
#: required input.
_SPECS: dict[str, tuple[str, dict[str, str]]] = {
    "install": (
        "Provision the data platform on Kubernetes.",
        {"namespace": "str", "release": "str"},
    ),
    "diagnose": (
        "Post-install health checks for the data platform.",
        {"connector": "str"},
    ),
    "ingest": (
        "Run one connector's source → bronze → RAG pass.",
        {"connector": "str!", "since": "str"},
    ),
    "refresh": (
        "Re-run a connector's pass over changes since its watermark.",
        {"connector": "str!"},
    ),
    "register": (
        "Register a connector — the landing zone a producer pushes into.",
        {"name": "str!", "kind": "str!", "bronze_root": "Path!"},
    ),
    "enroll": (
        "Enroll a source from its manifest: register the connector, record the "
        "site its rows belong to, and name the credential to issue.",
        {"manifest": "Path!", "bronze_root": "Path!"},
    ),
    "unregister": ("Remove a connector.", {"name": "str!"}),
    "list": (
        "List registered connectors, source kinds, or store backends.",
        {"resource": "str"},
    ),
    "troubleshoot": (
        "Explain why a connector is failing, with the fix.",
        {"connector": "str"},
    ),
    "preflight": (
        "Check a connector can reach its source and its bronze root.",
        {"connector": "str!"},
    ),
    "activity": (
        "Recent ingest activity — what landed, what was refused.",
        {"connector": "str", "limit": "int"},
    ),
    "backfill-urls": (
        "Backfill source URLs onto rows that predate provenance capture.",
        {"connector": "str!"},
    ),
    "backup": ("Back up the data platform's stores.", {"target": "Path"}),
    "backup_validate": ("Verify a backup restores.", {"target": "Path!"}),
    "ingest_push": (
        "Land pushed rows for a connector (the push front door's peer).",
        {"connector": "str!", "rows": "list!"},
    ),
}


def bind(registry: SkillRegistry) -> None:
    """Register every data_platform skill into ``registry``, with its spec."""
    for verb, fn in {**_SKILLS, **_NON_CLI_SKILLS}.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        description, inputs = _SPECS.get(verb, ("", {}))
        registry.register_skill(SkillSpec(name=name, fn=fn, description=description, inputs=inputs))


def bind_default() -> SkillRegistry:
    """Bind into the process-local default registry; idempotent."""
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    """Return the verb names (no namespace prefix). Used by the CLI parser."""
    return list(_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
