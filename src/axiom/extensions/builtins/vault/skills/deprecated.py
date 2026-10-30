# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The deprecated ``secrets.*`` skill names, each an alias of a ``vault.*`` skill (ADR-218).

One function per old name, so each has an importable entry
(``...vault.skills.deprecated:secrets_list``) for the manifest. They are
removed with the MCP aliases.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from . import values as _values
from .audit import run as _audit

Params = dict[str, Any]


def secrets_diagnose(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _values.diagnose(params, ctx)


def secrets_discover(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _values.discover(params, ctx)


def secrets_exposed(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _values.exposed(params, ctx)


def secrets_rotate(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _values.rotate(params, ctx)


def secrets_set(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _values.set_credential(params, ctx)


def secrets_list(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _values.list_credentials(params, ctx)


def secrets_rm(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _values.remove_credential(params, ctx)


def secrets_get(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _values.get_credential(params, ctx)


def secrets_audit(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    """The old ``secrets.audit``: the expiry report alone, every credential under ``findings``, never red."""
    result = _audit({**(params or {}), "expiry": True, "access": False, "report_only": True}, ctx)
    if isinstance(result.value, dict) and "expiry" in result.value:
        expiry = result.value["expiry"]
        result.value = {**result.value, "findings": expiry["all"], "counts": expiry["counts"]}
    return result


def secrets_reindex(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _values.reindex(params, ctx)


def secrets_git_credential(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _values.git_credential(params, ctx)


def secrets_wire_git(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _values.wire_git(params, ctx)


def secrets_annotate(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    return _values.annotate(params, ctx)


def secrets_vault(params: Params, ctx: SkillContext | None = None) -> SkillResult:
    """The old ``secrets.vault``: ``action=setup`` starts the vault, otherwise it says where it is."""
    if str((params or {}).get("action") or "").strip() == "setup":
        return _values.setup(params, ctx)
    return _values.where(params, ctx)


__all__ = [n for n in dir() if n.startswith("secrets_")]
