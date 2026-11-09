# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The storage module's skill functions, registered by the vault (ADR-218).

These functions (``set``, ``get``, ``list``, ``rotate``, ``exposed``, the setup
and diagnosis verbs...) are KEEP's value-handling verbs. They are registered
under their canonical ``vault.*`` names by
:mod:`axiom.extensions.builtins.vault.skills`, which also registers the
deprecated ``secrets.*`` names as aliases for one release.

``bind`` here is kept for callers that bound this package directly: it binds
the whole vault surface, aliases included.
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, default_registry

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

#: The deprecated ``secrets`` verbs, each still reachable under its old name.
_OLD_VERBS = (
    "diagnose", "discover", "exposed", "rotate", "set", "list", "rm", "get", "audit",
    "reindex", "git_credential", "wire_git", "annotate", "vault",
)


def bind(registry: SkillRegistry) -> None:
    """Register the vault's skills (canonical and deprecated names). Idempotent."""
    from axiom.extensions.builtins.vault import skills as vault_skills

    vault_skills.bind(registry)


def bind_default() -> SkillRegistry:
    """Bind into the process-local default registry; idempotent."""
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    """The deprecated ``secrets`` verb names (no namespace prefix)."""
    return list(_OLD_VERBS)


__all__ = [
    "annotate", "audit", "bind", "bind_default", "diagnose", "discover", "exposed", "get",
    "git_credential", "list_creds", "reindex", "rm", "rotate", "set_skill", "vault", "verbs",
    "wire_git",
]
