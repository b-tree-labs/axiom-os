# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.backup`` — policy-driven database backup with retention + off-box leg.

The productized answer to the bare-backup posture found on production
nodes: one skill that (a) dumps the database in pg_dump custom format
(structurally verifiable via ``pg_restore --list``), scoped to the
policy's schemas, (b) prunes the artifact directory to the policy's
``retention_count``, and (c) optionally replicates the artifact off-box
through the :class:`BackupUploader` seam (Box today).

Invocable three ways per ADR-056: ``axi data backup`` (CLI), any agent
persona via the SkillRegistry, and PULSE's scheduled dispatch (the
orchestrator registers a nightly cadence when the policy is enabled).

On failure the skill publishes the ``data.backup.failed`` HERALD event —
a backup that fails silently is the exact posture this productization
exists to end.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from axiom.governance.classification import Classification
from axiom.infra.backup import create_backup, list_backups
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz, _herald
from ..database.backup_policy import (
    BackupPolicy,
    load_backup_policy,
    validate_policy,
)

_DEFAULT_BOX_SECRET_REF = "env://BOX_JWT_CONFIG"


def _resolve_dsn(params: dict[str, Any]) -> str | None:
    """Explicit param first, then the platform DSN envs (refresh.py pattern)."""
    return params.get("dsn") or os.environ.get("DP1_RAG_DSN") or os.environ.get("DATABASE_URL")


def _db_label(dsn: str) -> str:
    """A short, secret-free database label for the authz resource URI."""
    try:
        name = urlsplit(dsn).path.lstrip("/")
        return name or "postgres"
    except ValueError:
        return "postgres"


def _prune_backups(target_root: Path, keep: int) -> int:
    """Count-based retention: keep the newest ``keep`` artifacts.

    TIDY's retention engine (``hygiene/retention.py``) is age-based
    (days-past-mtime policies over STATE_LOCATIONS) with no
    keep-newest-N mode, so backups carry this small count-based prune
    instead of importing it.
    """
    doomed = list_backups(target_root)[keep:]
    for stale in doomed:
        stale.backup_path.unlink(missing_ok=True)
    return len(doomed)


def _make_uploader(policy: BackupPolicy, params: dict[str, Any]):
    """Build the off-box uploader for the policy. Monkeypatched in tests."""
    from ..database.backup_uploader import BoxBackupUploader, resolve_box_auth

    secret_ref = params.get("box_secret_ref") or _DEFAULT_BOX_SECRET_REF
    return BoxBackupUploader(resolve_box_auth(secret_ref))


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    policy = load_backup_policy(state_dir=ctx.state_dir) or BackupPolicy()
    policy_errors = validate_policy(policy)
    if policy_errors:
        return SkillResult(
            ok=False,
            errors=[f"backup policy invalid: {e}" for e in policy_errors],
        )

    dsn = _resolve_dsn(params)
    if not dsn:
        return SkillResult(
            ok=False,
            errors=["no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL"],
        )

    target_root = Path(params.get("target_root") or policy.resolved_target_root()).expanduser()
    label = params.get("label", "")
    dry_run = bool(params.get("_dry_run", False))

    actions: list[str] = []
    with _authz.action(
        verb="backup",
        resource=f"data-platform://database/{_db_label(dsn)}",
        classification=Classification.INTERNAL,
        actor=params.get("actor"),
    ) as act:
        actions.append(f"audit-receipt: {act.receipt_id}")

        result = create_backup(
            dsn,
            output_dir=target_root,
            label=label,
            schemas=policy.schemas,
            fmt="custom",
            _dry_run=dry_run,
        )

        if not result.success:
            _herald.publish_event(
                "data.backup.failed",
                f"database backup FAILED: {result.error}",
                payload={"target_root": str(target_root), "error": result.error},
            )
            return SkillResult(
                ok=False,
                value={"artifact": str(result.backup_path)},
                actions_taken=actions + ["published HERALD event: data.backup.failed"],
                errors=[f"pg_dump failed: {result.error}"],
            )

        actions.append(f"backup created: {result.backup_path} ({result.size_bytes} bytes)")

        pruned = _prune_backups(target_root, policy.retention_count)
        if pruned:
            actions.append(
                f"pruned {pruned} artifact(s) past retention_count={policy.retention_count}"
            )

        offbox_receipt = None
        if policy.offbox == "box":
            uploader = _make_uploader(policy, params)
            offbox_receipt = uploader.upload(
                result.backup_path, folder_id=policy.box_folder_id or ""
            )
            if offbox_receipt.ok:
                actions.append(
                    f"off-box replica → box folder {policy.box_folder_id} "
                    f"(file {offbox_receipt.remote_id}; {offbox_receipt.detail})"
                )
            else:
                # The local artifact is good; the replica is not. Fail closed —
                # operators must learn the off-box leg is down NOW, not at
                # restore time.
                _herald.publish_event(
                    "data.backup.failed",
                    f"backup off-box replication FAILED: {offbox_receipt.detail}",
                    payload={
                        "artifact": str(result.backup_path),
                        "leg": "offbox",
                        "detail": offbox_receipt.detail,
                    },
                )
                actions.append("published HERALD event: data.backup.failed (offbox leg)")
                return SkillResult(
                    ok=False,
                    value={
                        "artifact": str(result.backup_path),
                        "size_bytes": result.size_bytes,
                        "pruned": pruned,
                        "offbox_ok": False,
                    },
                    actions_taken=actions,
                    errors=[f"off-box upload failed: {offbox_receipt.detail}"],
                )

    return SkillResult(
        ok=True,
        value={
            "artifact": str(result.backup_path),
            "size_bytes": result.size_bytes,
            "created_at": result.created_at,
            "fmt": "custom",
            "schemas": policy.schemas,
            "pruned": pruned,
            "receipt": act.receipt_id,
            "offbox_ok": offbox_receipt.ok if offbox_receipt else None,
            "offbox_remote_id": offbox_receipt.remote_id if offbox_receipt else None,
        },
        actions_taken=actions,
    )
