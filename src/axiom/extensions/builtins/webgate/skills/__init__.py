# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""webgate skills — account + API-key administration for the gate.

Each skill is a plain function ``run(params, ctx) -> SkillResult``, registered
under the ``gate`` namespace (the CLI noun). ``axi gate adduser`` → ``gate.add
user``, and the same functions are reachable from any agent persona or MCP
client. Per ADR-056, the CLI verbs in ``cli.py`` are 1:1 thin wrappers.

Two credential families, one admin surface: password accounts for humans
(``adduser`` / ``resetpw``) and bearer API keys for NON-human API principals
(``issue`` / ``revoke``); ``list`` covers both via its resource positional.
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import adduser, invite, issue_key, list_users, redeem, resetpw, revoke_key

_NAMESPACE = "gate"

_SKILLS = {
    "adduser": adduser.run,
    "resetpw": resetpw.run,
    "list": list_users.run,
    "issue": issue_key.run,
    "revoke": revoke_key.run,
    # An administrator approves who may have a key; the holder mints it. See
    # `invite.py` for why the administrator should not be the one holding a
    # plaintext key for somebody else.
    "invite": invite.run,
    "redeem": redeem.run,
}

#: Read-only verbs — declared non-mutating so the universal action audit
#: (``axiom.infra.audit_trail``) skips them. Everything else is audited.
_READ_ONLY = frozenset({"list"})


#: ADR-063 metadata for the verbs that have it. A capability with no SkillSpec
#: carries no description and declares no surfaces, so nothing can find it — the
#: older gate verbs are in that state and the repo ratchets the count down rather
#: than all at once. New verbs arrive with their metadata so the count never
#: goes up, which is what caught these two.
_SPECS = {
    "invite": SkillSpec(
        name="gate.invite",
        fn=invite.run,
        description=(
            "Approve a key for somebody without minting it. Prints a single-use "
            "code they redeem themselves, so the key is never in your terminal."
        ),
        inputs={
            "principal": "str",
            "role": "list[str]?",
            "scope": "list[str]?",
            "expires": "str?",
            "name": "str?",
            "invited_by": "str?",
            "site": "str?",
        },
        side_effects=True,
        idempotent=False,
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
    ),
    "redeem": SkillSpec(
        name="gate.redeem",
        fn=redeem.run,
        description=(
            "Spend an invitation you were sent and mint your own API key. The "
            "plaintext exists once, on your machine."
        ),
        inputs={"code": "str"},
        side_effects=True,
        idempotent=False,
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
    ),
}


def bind(registry: SkillRegistry) -> None:
    """Register every webgate skill into ``registry`` (idempotent)."""
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        spec = _SPECS.get(verb)
        if spec is not None:
            registry.register_skill(spec, mutating=verb not in _READ_ONLY)
        else:
            registry.register(name, fn, mutating=verb not in _READ_ONLY)


def bind_default() -> SkillRegistry:
    """Bind into the process-local default registry; idempotent."""
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    """The verb names (no namespace prefix). Used by the CLI parser."""
    return list(_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
