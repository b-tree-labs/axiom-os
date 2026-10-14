# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets`` skills — invocable through the SkillRegistry.

Per ADR-056, every ``axi secrets <verb>`` CLI surface is a thin wrapper
over a skill function with shape::

    def run(params: dict, ctx: SkillContext) -> SkillResult

Skills live under the ``secrets`` namespace (the CLI noun).
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import (
    reindex,
    audit,
    diagnose,
    discover,
    exposed,
    get,
    git_credential,
    list_creds,
    rm,
    rotate,
    set as set_skill,
    wire_git,
)

_NAMESPACE = "secrets"

_SKILLS = {
    "diagnose": diagnose.run,
    "discover": discover.run,
    "exposed": exposed.run,
    "rotate": rotate.run,
    # Foreign-credential surface (issue #667):
    "set": set_skill.run,
    "list": list_creds.run,
    "rm": rm.run,
    "get": get.run,
    "audit": audit.run,
    "reindex": reindex.run,
    "git_credential": git_credential.run,
    "wire_git": wire_git.run,
}


#: What each verb costs and who may reach it.
#:
#: `surfaces` is not documentation — ADR-155 makes it the enforcement point
#: for what an agent may do about a credential, so the tuple here is the
#: policy. Two rules are visible in it:
#:
#:   * `set` and `rm` are CLI-ONLY, at every authority level. One writes a
#:     value the caller chose, the other destroys the only copy, and neither
#:     is a thing an agent should be able to reach however much autonomy an
#:     operator has granted.
#:   * `get` is CLI-only for the same reason in reverse: it is the one verb
#:     that returns a value.
#:
#: Everything else is readable or self-describing and is safe for an agent
#: that has been granted the scope.
_SPECS: dict[str, tuple[bool, bool, tuple[str, ...]]] = {
    # verb: (idempotent, side_effects, surfaces)
    "diagnose": (True, False, ("cli", "mcp", "agent_tool", "skill_md")),
    "discover": (True, False, ("cli", "mcp", "agent_tool", "skill_md")),
    "list": (True, False, ("cli", "mcp", "agent_tool", "skill_md")),
    "audit": (True, False, ("cli", "mcp", "agent_tool", "skill_md")),
    # Rotation is not idempotent: running it twice issues two credentials
    # and retires one that something may still be holding.
    "rotate": (False, True, ("cli", "mcp", "agent_tool")),
    # Records the exposure, then rotates. Reachable by an agent on purpose
    # — it is the containment verb, and the clock matters more than the
    # ceremony (ADR-155 §3).
    "exposed": (False, True, ("cli", "mcp", "agent_tool")),
    # Opens stored values to backfill digests. Operator-invoked only.
    "reindex": (True, True, ("cli",)),
    # The three that never leave the terminal.
    "set": (False, True, ("cli",)),
    "rm": (False, True, ("cli",)),
    "get": (True, False, ("cli",)),
    # git's credential protocol speaks on stdin/stdout; there is no other
    # surface it could have.
    "git_credential": (True, False, ("cli",)),
    "wire_git": (False, True, ("cli",)),
}


def bind(registry: SkillRegistry) -> None:
    """Register every secrets skill into ``registry``. Idempotent."""
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        idempotent, side_effects, surfaces = _SPECS[verb]
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=fn,
                idempotent=idempotent,
                side_effects=side_effects,
                surfaces=surfaces,
            )
        )


def bind_default() -> SkillRegistry:
    """Bind into the process-local default registry; idempotent."""
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    """Return the verb names (no namespace prefix). Used by the CLI parser."""
    return list(_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
