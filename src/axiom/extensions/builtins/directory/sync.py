# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The cadenced projection — directory groups → relationship tuples.

ADR-103 decision 7: reconciler cadence is the real freshness dial. This is
the run one heartbeat performs: for each configured group, enumerate its
members from the directory and reconcile the store to exactly that set
(``MembershipReconciler`` — a diff, so removals happen), recording removals
in the revoked set so they take effect on the next resolution.

Three refusals are the point of the module:

* a provider that cannot **enumerate** (``oidc_claims``) cannot sync — the
  run reports that instead of quietly doing nothing;
* a group whose enumeration **fails** is reported as failed, never treated as
  empty — an empty desired set removes everyone, which is exactly what a
  directory outage must not be allowed to do (the reconciler's
  ``StaleProjectionError`` rule, applied to enumeration);
* ``dry_run`` computes and reports the diff without a single write.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from axiom.extensions.builtins.directory.protocol import DirectoryCapability, GroupRef
from axiom.extensions.builtins.directory.reconcile import (
    GROUP_PREFIX,
    MEMBER_RELATION,
    USER_PREFIX,
    MembershipReconciler,
    TupleStore,
)
from axiom.extensions.builtins.directory.revoked import RevokedSet


@dataclass(frozen=True)
class GroupSyncResult:
    group_id: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    error: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


@dataclass
class SyncReport:
    provider: str
    dry_run: bool
    started_at: float
    groups: list[GroupSyncResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors and all(g.error is None for g in self.groups)

    @property
    def added(self) -> int:
        return sum(len(g.added) for g in self.groups)

    @property
    def removed(self) -> int:
        return sum(len(g.removed) for g in self.groups)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 3),
            "added": self.added,
            "removed": self.removed,
            "groups": [asdict(g) for g in self.groups],
            "errors": list(self.errors),
        }


def _diff(store: TupleStore, group: GroupRef, desired: set[str]) -> GroupSyncResult:
    obj = f"{GROUP_PREFIX}{group.id}"
    current = {
        u[len(USER_PREFIX) :] if u.startswith(USER_PREFIX) else u
        for u in store.list_members(obj, MEMBER_RELATION)
    }
    return GroupSyncResult(
        group_id=group.id,
        added=tuple(sorted(desired - current)),
        removed=tuple(sorted(current - desired)),
        unchanged=tuple(sorted(desired & current)),
    )


def run_sync(
    *,
    provider: Any,
    provider_name: str,
    groups: Sequence[GroupRef],
    store: TupleStore | None,
    revoked: RevokedSet | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> SyncReport:
    started = time.time() if now is None else now
    report = SyncReport(provider=provider_name, dry_run=dry_run, started_at=started)

    caps = getattr(provider, "capabilities", frozenset()) if provider is not None else frozenset()
    if provider is None or DirectoryCapability.ENUMERATE not in caps:
        report.errors.append(
            f"provider {provider_name!r} cannot enumerate group members "
            "(needs the ENUMERATE capability: local or entra)"
        )
    elif store is None:
        report.errors.append("no tuple store configured (AXIOM_DIRECTORY_TUPLE_STORE)")
    elif not groups:
        report.errors.append("no groups to sync (AXIOM_DIRECTORY_SYNC_GROUPS or a role map)")

    if report.errors:
        report.duration_s = time.time() - started if now is None else 0.0
        return report

    reconciler = MembershipReconciler(store=store, revoked=revoked, split_writes=True)
    for group in groups:
        try:
            desired = {str(p.subject) for p in provider.members_of(group)}
        except Exception as exc:  # noqa: BLE001 — a failed enumeration is NOT an empty group
            report.groups.append(
                GroupSyncResult(group_id=group.id, error=f"enumeration failed: {exc}")
            )
            continue
        try:
            if dry_run:
                report.groups.append(_diff(store, group, desired))
            else:
                r = reconciler.reconcile(group, desired_subjects=desired, now=started)
                report.groups.append(
                    GroupSyncResult(
                        group_id=group.id, added=r.added, removed=r.removed, unchanged=r.unchanged
                    )
                )
        except Exception as exc:  # noqa: BLE001 — one bad group must not stop the rest
            report.groups.append(
                GroupSyncResult(group_id=group.id, error=f"reconcile failed: {exc}")
            )
    report.duration_s = time.time() - started if now is None else 0.0
    return report


__all__ = ["GroupSyncResult", "SyncReport", "run_sync"]
