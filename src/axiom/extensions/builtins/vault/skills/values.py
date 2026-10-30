# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The vault's value and setup verbs, as KEEP's skills (ADR-218).

Each is a thin entry over its implementation in the storage module
(``axiom.extensions.builtins.secrets``), which keeps the mechanism: providers,
the value store, rotation, discovery. Registering them from here is what makes
them the vault's verbs rather than another extension's.
"""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.secrets.skills import annotate as _annotate
from axiom.extensions.builtins.secrets.skills import diagnose as _diagnose
from axiom.extensions.builtins.secrets.skills import discover as _discover
from axiom.extensions.builtins.secrets.skills import exposed as _exposed
from axiom.extensions.builtins.secrets.skills import get as _get
from axiom.extensions.builtins.secrets.skills import git_credential as _git_credential
from axiom.extensions.builtins.secrets.skills import list_creds as _list
from axiom.extensions.builtins.secrets.skills import reindex as _reindex
from axiom.extensions.builtins.secrets.skills import rm as _rm
from axiom.extensions.builtins.secrets.skills import rotate as _rotate
from axiom.extensions.builtins.secrets.skills import set as _set
from axiom.extensions.builtins.secrets.skills import vault as _where_setup
from axiom.extensions.builtins.secrets.skills import wire_git as _wire_git
from axiom.infra.skills import SkillContext, SkillResult

Params = dict[str, Any]


def set_credential(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _set.run(params, ctx)


def get_credential(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _get.run(params, ctx)


def list_credentials(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _list.run(params, ctx)


def remove_credential(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _rm.run(params, ctx)


def rotate(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _rotate.run(params, ctx)


def exposed(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _exposed.run(params, ctx)


def reindex(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _reindex.run(params, ctx)


def annotate(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _annotate.run(params, ctx)


def where(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _where_setup.where(params, ctx)


def setup(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _where_setup.setup(params, ctx)


def discover(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _discover.run(params, ctx)


def diagnose(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _diagnose.run(params, ctx)


def git_credential(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _git_credential.run(params, ctx)


def wire_git(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _wire_git.run(params, ctx)


__all__ = [
    "annotate", "diagnose", "discover", "exposed", "get_credential", "git_credential",
    "list_credentials", "reindex", "remove_credential", "rotate", "set_credential", "setup",
    "where", "wire_git",
]
