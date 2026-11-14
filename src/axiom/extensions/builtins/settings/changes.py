# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The change log: every settings, flag, role, key and membership change, newest first.

A projection of the one action chain (``axiom.infra.audit_trail``), never a
second store. A write that said what it changed (``note_change``) carries
``change = {kind, subject, old, new}`` in its audit record; this module reads
those records back as rows of who, what, old → new and when (MVP-08-A2). The
chain beneath is what Settings › Change log › Audit verifies (MVP-13-A2).

A write in one of the change namespaces that noted nothing (an older verb) is
still listed, with its kind from the verb and old/new unknown, so nothing that
changed the site is missing from the log.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from axiom.infra import audit_trail

#: The verbs whose successful runs are changes to the site or a person's access.
CHANGE_NAMESPACES: tuple[str, ...] = ("settings.", "flags.", "gate.")

#: A verb's kind when its record carries no change note.
_KIND_BY_SKILL: dict[str, str] = {
    "settings.set": "setting",
    "settings.reset": "setting",
    "flags.set": "flag",
    "flags.clear": "flag",
    "flags.cohort_set": "cohort",
    "flags.cohort_delete": "cohort",
    "flags.rollout_set": "rollout",
    "flags.kill": "kill-switch",
    "flags.cohort_import": "cohort",
    "flags.cohort_refresh": "cohort",
    "flags.stage_plan_set": "stage-plan",
    "flags.stage_plan_halt": "stage-plan",
    "flags.approve": "approval",
    "flags.change_cancel": "scheduled-change",
    # The same writes under their names before the flags noun took them (records keep them).
    "features.cohort_set": "cohort",
    "features.cohort_delete": "cohort",
    "features.rollout_set": "rollout",
    "features.kill": "kill-switch",
    "features.cohort_import": "cohort",
    "features.cohort_refresh": "cohort",
    "features.stage_plan_set": "stage-plan",
    "features.stage_plan_halt": "stage-plan",
    "features.approve": "approval",
    "features.change_cancel": "scheduled-change",
    "gate.role": "role",
    "gate.adduser": "account",
    "gate.resetpw": "account",
    "gate.suspend": "membership",
    "gate.reinstate": "membership",
    "gate.issue": "api-key",
    "gate.rescope": "api-key",
    "gate.revoke": "api-key",
    "gate.redeem": "api-key",
    "gate.invite": "invitation",
}

#: Kinds a person can filter by.
KINDS: tuple[str, ...] = (
    "setting", "flag", "extension", "function", "role", "account", "membership", "api-key", "invitation",
    "cohort", "rollout", "kill-switch", "stage-plan", "scheduled-change", "approval",
)


def _row(rec: dict[str, Any]) -> dict[str, Any]:
    change = rec.get(audit_trail.CHANGE_FIELD) or {}
    skill = str(rec.get("skill") or "")
    return {
        "seq": rec.get("seq"),
        "when": rec.get("ts"),
        "who": rec.get("principal"),
        "kind": change.get("kind") or _KIND_BY_SKILL.get(skill, skill.split(".", 1)[0]),
        "what": change.get("subject") or skill,
        "old": change.get("old"),
        "new": change.get("new"),
        "detail": bool(change),
        "skill": skill,
        "site": rec.get("site"),
    }


def _is_change(rec: dict[str, Any]) -> bool:
    if rec.get(audit_trail.CHANGE_FIELD):
        return True
    skill = str(rec.get("skill") or "")
    return rec.get("outcome") == "ok" and skill in _KIND_BY_SKILL


def _parse_since(since: str | None) -> str | None:
    if not since:
        return None
    try:
        return datetime.fromisoformat(str(since).replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def read_changes(
    state_dir: Path,
    *,
    limit: int = 50,
    kind: str | None = None,
    principal: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """The change rows, newest first, filtered."""
    floor = _parse_since(since)
    rows: list[dict[str, Any]] = []
    for rec in reversed(audit_trail.read_chain(audit_trail.audit_path(state_dir))):
        if not _is_change(rec):
            continue
        row = _row(rec)
        if kind and row["kind"] != kind:
            continue
        if principal and row["who"] != principal:
            continue
        if floor and str(row["when"] or "") < floor:
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def audit_rows(state_dir: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    """The chain itself, newest first: every audited write, denials included."""
    out = []
    for rec in reversed(audit_trail.read_chain(audit_trail.audit_path(state_dir))):
        out.append({
            "seq": rec.get("seq"),
            "when": rec.get("ts"),
            "who": rec.get("principal"),
            "skill": rec.get("skill"),
            "outcome": rec.get("outcome"),
            "site": rec.get("site"),
            "change": rec.get(audit_trail.CHANGE_FIELD),
            "hash": str(rec.get("hash") or "")[:12],
        })
        if len(out) >= limit:
            break
    return out


def verify(state_dir: Path) -> dict[str, Any]:
    """Recompute every link of the chain under the change log."""
    return audit_trail.verify_report(audit_trail.audit_path(state_dir))


__all__ = ["CHANGE_NAMESPACES", "KINDS", "audit_rows", "read_changes", "verify"]
