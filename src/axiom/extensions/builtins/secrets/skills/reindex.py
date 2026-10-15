# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.reindex`` — give already-stored credentials their fingerprint.

`set()` derives a fingerprint when a value is written, which means the index
fills in only as credentials are created or rotated. On a machine that
already holds twenty-two of them, the sweep stays unverifiable for as long
as it takes to rotate them all — so the feature would help new installs and
nobody else, which is the failure mode it was built to fix.

This is the one-time backfill. It is a SEPARATE, operator-invoked verb
rather than something `list()` or the sweep does silently, because it is
the only path here that opens stored values. `list()` is metadata-only and
must stay that way: the hourly sweep reads it, and a sweep that decrypts
every credential to decide whether to look at the disk is a worse trade
than the question it answers.

Values are opened, hashed, and dropped. Nothing is written but the digest,
and nothing is printed but counts.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from ..foreign.store import ForeignCredentialStore, _fingerprint_of

    store = params.get("_store") or ForeignCredentialStore(
        ctx.state_dir if ctx is not None else None
    )
    dry_run = bool(params.get("dry_run"))

    rows = store.list()
    missing = [r for r in rows if not r.get("fingerprint")]
    if not missing:
        return SkillResult(
            ok=True,
            actions_taken=[f"all {len(rows)} credential(s) already indexed"],
            value={"total": len(rows), "indexed": 0, "already": len(rows), "failed": 0},
        )

    if dry_run:
        return SkillResult(
            ok=True,
            actions_taken=[
                f"{len(missing)} of {len(rows)} credential(s) have no fingerprint; "
                "re-run without --dry-run to open and index them"
            ],
            value={"total": len(rows), "indexed": 0,
                   "already": len(rows) - len(missing), "failed": 0,
                   "names": sorted(r["name"] for r in missing)},
        )

    indexed, failed = 0, []
    for row in missing:
        name = row["name"]
        try:
            with store.get(name) as secret:
                digest = _fingerprint_of(bytes(secret.value))
            store.update_metadata(name, fingerprint=digest)
            indexed += 1
        except Exception as exc:  # noqa: BLE001 — one bad credential is not all of them
            # Named, because a credential that cannot be opened is itself
            # worth knowing about, and skipping it silently would leave the
            # index quietly incomplete again.
            failed.append(f"{name}: {type(exc).__name__}")

    return SkillResult(
        ok=not failed,
        actions_taken=[f"indexed {indexed} credential(s)"],
        errors=[f"could not index {len(failed)}: {', '.join(failed)}"] if failed else [],
        value={"total": len(rows), "indexed": indexed,
               "already": len(rows) - len(missing), "failed": len(failed)},
    )
