# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""KEEP's skill surface (ADR-056: one skill function per CLI verb)."""

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import audit, declare, reconcile, renew, resolve, sweep

_NAMESPACE = "vault"

# Declared specs (ADR-063 / ADR-072 §4.9.4). Until now KEEP registered through
# the legacy ``register(name, fn)`` form, so its verbs carried no description
# and no surfaces — which means they were invisible to MCP, to chat, to the
# agent tool loop and to the capability discovery block. An assistant asked to
# deal with an expiring credential had no way to learn these existed, and
# reached for a raw git credential helper instead.
#
# Only the READ verbs are projected. They report metadata and never a
# credential value, which is what makes them safe on a shared surface; rotation
# and sweeps stay CLI-only where a human is present.
_SPECS = (
    SkillSpec(
        name="vault.resolve",
        fn=resolve.run,
        description=(
            "Which stored credential to use for a host, and whether it is still "
            "alive. A host often has several (git-scoped, API-scoped, per-project) "
            "and the git credential helper is keyed by host alone, so it silently "
            "returns one of them. Call this before reaching for a credential "
            "helper. Returns names, purposes and expiry health — never a value."
        ),
        inputs={
            "host": "hostname, e.g. git.example.org",
            "purpose": "git|api|registry|runner (optional; omit to list every candidate)",
        },
        side_effects=False,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool"),
    ),
    SkillSpec(
        name="vault.reconcile",
        fn=reconcile.run,
        description=(
            "Compare what the credential store RECORDS against what each "
            "issuer actually says: revoked, real expiry, or a value the issuer "
            "no longer recognises. Expiry auditing only reads our own metadata, "
            "which is what we believed when we wrote it down. Metadata only, "
            "never a value."
        ),
        inputs={"apply": "record the issuer's expiry locally (metadata only)"},
        side_effects=False,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool"),
    ),
    SkillSpec(
        name="vault.declare",
        fn=declare.run,
        description=(
            "Record what can be done about a credential — self_rotatable, "
            "human_rotatable, externally_owned or non_expiring — plus a review "
            "date or owner. This is what makes an undated credential's finding "
            "clearable instead of repeating forever."
        ),
        inputs={
            "name": "credential name",
            "disposition": "self_rotatable|human_rotatable|externally_owned|non_expiring",
            "review_by": "YYYY-MM-DD, for credentials with no expiry",
            "owner": "who to contact, required for externally_owned",
        },
        # WRITES metadata, so it is declared but NOT projected to a protocol
        # surface: a write on MCP is a per-verb decision, not a default.
        side_effects=True,
        idempotent=True,
        surfaces=("cli",),
    ),
    SkillSpec(
        name="vault.audit",
        fn=audit.run,
        description=(
            "Report credentials that are expired, expiring soon, or have no "
            "recorded expiry at all. Metadata only, never a value."
        ),
        inputs={"within_days": "expiry horizon in days (default 14)"},
        side_effects=False,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool"),
    ),
)
# Every CLI verb maps 1:1 to a registered skill (ADR-056). `renew` was
# absent here, so the verb that rotates an expiring credential was a
# CLI-private function: no authority gate, no audit record, no telemetry,
# and invisible to MCP, chat and agents. It crashed for weeks and left no
# trace anywhere that anyone reads.
_SKILLS = {"sweep": sweep.run, "renew": renew.run}


def bind(registry: SkillRegistry) -> None:
    for spec in _SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=False)
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register(name, fn)


def bind_default() -> SkillRegistry:
    """Bind into the process-local default registry; idempotent."""
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    return list(_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
