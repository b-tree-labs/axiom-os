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

from axiom.infra.skills import SkillRegistry, default_registry

from . import adduser, issue_key, list_users, resetpw, revoke_key

_NAMESPACE = "gate"

_SKILLS = {
    "adduser": adduser.run,
    "resetpw": resetpw.run,
    "list": list_users.run,
    "issue": issue_key.run,
    "revoke": revoke_key.run,
}


def bind(registry: SkillRegistry) -> None:
    """Register every webgate skill into ``registry`` (idempotent)."""
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
    """The verb names (no namespace prefix). Used by the CLI parser."""
    return list(_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
