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

from axiom.infra.skill_declarations import NextStep, Precondition
from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import (
    annotate,
    audit,
    diagnose,
    discover,
    exposed,
    get,
    git_credential,
    list_creds,
    reindex,
    rm,
    rotate,
    vault,
    wire_git,
)
from . import (
    set as set_skill,
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
    "annotate": annotate.run,
    "vault": vault.run,
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
    # Metadata only. Still CLI-only: an agent must not rewrite expiry on a
    # credential a person is accountable for.
    "annotate": (True, True, ("cli",)),
    # Where the vault is (backend, URL, the token's FILE). CLI-only: it decides which vault every
    # process on this machine trusts, which is a person's call, not an agent's.
    "vault": (True, True, ("cli",)),
}


def _has(param: str, why: str):
    """A cheap presence check for a required parameter, with the reason it failed."""

    def check(params, ctx):
        return (bool((params or {}).get(param)), why)

    return check


#: Chaining declarations (ADR-185): risk, requires, produces, next_steps, recipes.
#:
#: Two decisions worth keeping in view. ``exposed`` is ``reversible-write`` and
#: NOT ``destructive``: ADR-155 makes it reachable by an agent on purpose
#: because the clock matters more than the ceremony, and a plan / approve step
#: in front of it would put a person in the way of closing a leak. It records
#: the exposure first and rotates second, and the rotation issues a new version
#: rather than deleting the old one. ``rm`` and ``set`` are ``destructive``
#: (they destroy or replace the only copy of a value), and both are CLI-only,
#: where a person is already at the verb.
_CHAIN: dict[str, dict] = {
    "diagnose": dict(risk="read", requires=(), produces=(), next_steps=()),
    "discover": dict(risk="read", requires=(), produces=(), next_steps=()),
    "list": dict(risk="read", requires=(), produces=(), next_steps=()),
    "audit": dict(risk="read", requires=(), produces=(), next_steps=()),
    "get": dict(risk="read", requires=(), produces=(), next_steps=()),
    "git_credential": dict(risk="read", requires=(), produces=(), next_steps=()),
    "rotate": dict(risk="reversible-write", requires=(), produces=(), next_steps=()),
    "exposed": dict(
        risk="reversible-write",
        requires=(
            Precondition(
                "ref",
                "the secret that leaked, as a reference (never the value)",
                check=_has("ref", "no ref was given"),
                fix="pass ref=<scheme>://<path> or a stored name; list stored names with 'axi secrets list'",
            ),
            Precondition(
                "where",
                "the surface the credential appeared on",
                check=_has("where", "no surface was given"),
                fix="pass where=<transcript|log|chat|url|...>, the place the value was seen",
            ),
        ),
        produces=(),
        next_steps=(
            NextStep("vault.reconcile", "ok", "confirm the issuer now treats the old value as dead"),
        ),
        recipes=("credential-leak-response",),
    ),
    "reindex": dict(risk="reversible-write", requires=(), produces=(), next_steps=()),
    "set": dict(risk="destructive", requires=(), produces=(), next_steps=()),
    "rm": dict(risk="destructive", requires=(), produces=(), next_steps=()),
    "wire_git": dict(risk="reversible-write", requires=(), produces=(), next_steps=()),
    "annotate": dict(risk="reversible-write", requires=(), produces=(), next_steps=()),
    "vault": dict(risk="reversible-write", requires=(), produces=(), next_steps=()),
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
                **_CHAIN[verb],
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
