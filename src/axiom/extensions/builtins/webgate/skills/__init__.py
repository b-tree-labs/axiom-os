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

from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec, default_registry

from . import (
    adduser,
    invite,
    issue_key,
    list_users,
    redeem,
    rescope_key,
    resetpw,
    revoke_key,
    role,
)

_NAMESPACE = "gate"

_SKILLS = {
    "adduser": adduser.run,
    "resetpw": resetpw.run,
    "role": role.run,
    "list": list_users.run,
    "issue": issue_key.run,
    "revoke": revoke_key.run,
    "rescope": rescope_key.run,
    # An administrator approves who may have a key; the holder mints it. See
    # `invite.py` for why the administrator should not be the one holding a
    # plaintext key for somebody else.
    "invite": invite.run,
    "redeem": redeem.run,
}

#: Verbs whose result a person reads through a text presentation of their own.
#: The generic renderer prints ``actions_taken`` and lists; a verb whose answer
#: is a one-time secret in ``value`` says how it is shown rather than the
#: renderer learning each verb's shape. Each takes the SkillResult and returns
#: lines, or ``None`` to fall back to the generic rendering.
_PRESENTERS = {
    "invite": invite.present,
    "redeem": redeem.present,
    "list": list_users.present,
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
    "revoke": SkillSpec(
        name="gate.revoke",
        fn=revoke_key.run,
        description=(
            "Revoke an issued API key, or an invitation nobody has redeemed yet. "
            "Effective on the next request; the record is kept for the audit trail."
        ),
        inputs={
            "resource": "str",
            "key_id": "str?",
            "invitation_id": "str?",
            "keys_file": "str?",
            "invitations_file": "str?",
        },
        side_effects=True,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
    ),
    "list": SkillSpec(
        name="gate.list",
        fn=list_users.run,
        description=(
            "List gate accounts, API keys or invitations (with their status). "
            "Never prints a hash or an invitation code."
        ),
        inputs={
            "resource": "str?",
            "accounts_file": "str?",
            "keys_file": "str?",
            "invitations_file": "str?",
        },
        side_effects=False,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
    ),
    "role": SkillSpec(
        name="gate.role",
        fn=role.run,
        description=(
            "Set, add or remove roles on one account or on every account holding "
            "a role. Refuses roles the node does not define; never touches passwords."
        ),
        inputs={
            "email": "str?",
            "where_role": "str?",
            "set": "list[str]?",
            "add": "list[str]?",
            "remove": "list[str]?",
        },
        side_effects=True,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
    ),
    "rescope": SkillSpec(
        name="gate.rescope",
        fn=rescope_key.run,
        description=(
            "Change an issued API key's site or scopes in place. The token is "
            "unchanged, so its holder keeps using it under the new scope."
        ),
        inputs={
            "resource": "str?",
            "key_id": "str",
            "site": "str?",
            "add_scope": "list[str]?",
            "remove_scope": "list[str]?",
            "keys_file": "str?",
        },
        side_effects=True,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
        # Rescoping again restores the old site-less scopes; the token is kept.
        risk="reversible-write",
        requires=(),
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


def present(verb: str, result: SkillResult) -> list[str] | None:
    """The verb's own text presentation of ``result``, or ``None`` for none."""
    presenter = _PRESENTERS.get(verb)
    return presenter(result) if presenter else None


def bind_default() -> SkillRegistry:
    """Bind into the process-local default registry; idempotent."""
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    """The verb names (no namespace prefix). Used by the CLI parser."""
    return list(_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
