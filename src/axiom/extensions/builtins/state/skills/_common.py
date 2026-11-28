# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Shared plumbing for the ``state.*`` skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra import state_snapshots as ss
from axiom.infra.skills import SkillContext, ensure_context


def cli() -> str:
    return ss._brand_cli()


def resolve(params: dict[str, Any], ctx: SkillContext) -> tuple[Path, Path, ss.SnapshotPolicy, ss.Store]:
    """``(state_dir, snapshot_root, policy, store)`` for this invocation.

    ``params["to"]`` / ``params["from"]`` point the store at another directory
    (typically another disk); the state dir is always the context's.
    """
    state_dir = Path(ctx.state_dir)
    root_arg = params.get("to") or params.get("from_dir") or params.get("snapshots")
    root = Path(root_arg).expanduser() if root_arg else ss.default_snapshot_root(state_dir)
    policy = ss.load_policy(state_dir)
    return state_dir, root, policy, ss.open_store(state_dir, root)


def coverage_lines(root: Path, state_dir: Path) -> list[str]:
    """What is and is not covered -- stated in every report that creates or shows one."""
    c = cli()
    lines = [
        "COVERED:     file-based harness state under the state dir (critical / useful tiers).",
        "NOT COVERED: databases (Postgres stores), credential/keychain/vault VALUES, "
        "caches and large regenerable data (listed in the manifest with reasons).",
        f"             Database: `{c} state snapshot --with-db --db-url <url>` or `{c} data backup`.",
        f"             Credential values: `{c} vault backup` (encrypted; see `{c} vault key-info`).",
    ]
    if ss.same_device(root, state_dir):
        lines.append(
            "WARNING:     snapshots are on the same device as the state: a disk loss takes both. "
            f"Add a second copy: `{c} state snapshot --to /path/on/another/disk`."
        )
    return lines


# ---------------------------------------------------------------- chaining preconditions (ADR-185)


def _quick_ok(store: ss.Store, sid: str) -> bool:
    return ss.verify_snapshot(store, sid, deep=False).ok


def snapshot_exists(params: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    """The snapshot named by ``id`` (default: latest) is in the store. Reads the id list only."""
    try:
        _sd, _root, _policy, store = resolve(params, ensure_context(ctx))
        ss.resolve_id(store, params.get("id") or "latest")
    except ss.SnapshotError as exc:
        return False, str(exc).splitlines()[0]
    except (OSError, AttributeError, TypeError) as exc:
        return False, f"the snapshot store could not be read ({type(exc).__name__})"
    return True, ""


def verified_snapshot(params: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    """A snapshot that passes the quick integrity check exists to restore from.

    ``id`` names one (exact, prefix or ``latest``). Without ``id``, or with ``latest``, any intact
    snapshot will do, because restoring ``latest`` passes over a damaged newer one. ``force`` relaxes
    this to "the snapshot exists", which is what the restore's own override means. Cheap: manifests and
    blob sizes; no blob is re-hashed and nothing is written.
    """
    try:
        _sd, _root, _policy, store = resolve(params, ensure_context(ctx))
        ref = params.get("id") or "latest"
        if ref == "latest":
            ids = ss.restorable_order(store)
            if not ids:
                ss.resolve_id(store, "latest")  # raises with the "no snapshots" explanation
            if params.get("force"):
                return True, ""
            results = [ss.verify_snapshot(store, i, deep=False) for i in ids]
            if any(r.ok for r in results):
                return True, ""
            # Say which and why (ids and the first problem each; never file contents), as the restore itself does.
            detail = "; ".join(f"{r.id}: {r.problems[0] if r.problems else 'fails verification'}" for r in results[:5])
            return False, f"no snapshot in the store passes the integrity check ({detail})"
        sid = ss.resolve_id(store, ref)
        if params.get("force") or _quick_ok(store, sid):
            return True, ""
        return False, f"snapshot {sid} fails its integrity check"
    except ss.SnapshotError as exc:
        return False, str(exc).splitlines()[0]
    except (OSError, AttributeError, TypeError) as exc:
        return False, f"the snapshot store could not be read ({type(exc).__name__})"
