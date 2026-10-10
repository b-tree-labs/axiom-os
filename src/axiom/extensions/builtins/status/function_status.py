# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""How each platform function on a role node is doing: one honest section each.

Registered in the ``axiom.node_status`` group beside a consumer's own sections.
Each row carries a state (``ok``, ``info``, ``warn``, ``fail``), one plain
sentence, and the command that fixes it when there is one. A question this
node cannot answer says so; nothing is reported as fine because it was not
measured.

* **Sharing upstream**: where the forwarder is sending (the intake, Box, or
  waiting), why, since when, and how many outbox batches it is behind.
* **Updates**: the policy in force and the last update's outcome.
* **Local archive**: the outbox and how full the disk holding it is.
* **Conform**: the last pass over each bronze root, loud when a root is empty.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Above this share of the disk used, the archive section turns amber.
DISK_WARN = 0.80
#: Above this, red: the next large backlog will not fit.
DISK_FAIL = 0.95
UPDATE_ROOT_ENV = "AXIOM_UPDATE_ROOT"
UPDATE_POLICY_ENV = "AXIOM_UPDATE_POLICY"


def _row(label: str, value: str, state: str = "info", fix: str = "") -> dict[str, Any]:
    return {"label": label, "value": value, "state": state, "fix": fix}


def _state_dir() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir())


def _cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name or "axi"
    except Exception:  # noqa: BLE001
        return "axi"


def _since(iso: str) -> str:
    try:
        then = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    s = int(max(0, (datetime.now(UTC) - then).total_seconds()))
    if s < 5400:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} days"


def _sharing(outbox_dir: Path | None) -> dict[str, Any] | None:
    status_path = _state_dir() / "forward" / "status.json"
    if not status_path.is_file():
        return None
    try:
        st = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "title": "Sharing upstream",
            "rows": [
                _row(
                    "sending to",
                    "the forwarder's status file could not be read",
                    "warn",
                    f"{_cli()} data forward --once  # rewrites it",
                )
            ],
        }
    current = str(st.get("current") or "waiting")
    reason = str(st.get("reason") or "")
    if current == "intake":
        row = _row("sending to", f"the UT data intake, for {_since(st.get('since', ''))}", "ok")
    elif current == "box":
        row = _row(
            "sending to",
            f"Box, because the intake is not available ({reason})",
            "info",
            "nothing to do: it moves to the intake by itself when the intake is healthy",
        )
    else:
        row = _row(
            "sending to",
            f"waiting: nothing can be sent right now ({reason})",
            "warn",
            f"check the network and the intake key; {_cli()} data forward --once shows why",
        )
    rows = [row]
    switches = list(st.get("switches") or [])
    if switches:
        last = switches[-1]
        rows.append(
            _row(
                "last switch",
                f"{last.get('from')} → {last.get('to')} at {last.get('at')} ({last.get('reason')})",
            )
        )
    after = int(st.get("after") or 0)
    head = None
    if outbox_dir is not None and Path(outbox_dir).is_dir():
        try:
            from axiom.extensions.builtins.data_platform.ingest_sink.edge import EdgeOutbox

            head = EdgeOutbox(outbox_dir).last_seq()
        except Exception:  # noqa: BLE001
            head = None
    if head is None:
        rows.append(
            _row(
                "behind",
                "unknown: this node's outbox is not configured here",
                "info",
                "set AXIOM_INGEST_OUTBOX_DIR",
            )
        )
    else:
        behind = max(0, head - after)
        rows.append(
            _row(
                "behind",
                f"{behind} batches not yet delivered upstream",
                "ok" if behind == 0 else ("info" if current != "waiting" else "warn"),
            )
        )
    return {"title": "Sharing upstream", "rows": rows}


def _updates(update_root: Path | None, policy: str) -> dict[str, Any] | None:
    if update_root is None and not policy:
        return None
    rows = [
        _row(
            "policy",
            policy or "not set (an update waits for a person)",
            "info" if policy else "warn",
            "" if policy else f"{_cli()} features --set update_policy=auto-patch",
        )
    ]
    if update_root is None or not Path(update_root).is_dir():
        rows.append(_row("last update", "none recorded on this node", "info"))
        return {"title": "Updates", "rows": rows}
    from axiom.extensions.builtins.update.swap import current_version, last_outcome

    cur = current_version(Path(update_root))
    if cur:
        rows.append(_row("running", cur, "info"))
    last = last_outcome(Path(update_root)) or {}
    status = str(last.get("status") or "")
    words = {
        "updated": ("ok", f"updated {last.get('from_version')} → {last.get('to_version')}"),
        "unchanged": ("ok", "already current"),
        "rolled_back": (
            "warn",
            f"rolled back from {last.get('to_version')} to {last.get('from_version')}: {last.get('detail')}",
        ),
        "rejected_before_switch": (
            "warn",
            f"{last.get('to_version')} failed its checks and was not switched to: {last.get('detail')}",
        ),
        "install_failed": (
            "warn",
            f"{last.get('to_version')} could not be installed: {last.get('detail')}",
        ),
    }
    if status:
        state, text = words.get(status, ("info", status))
        rows.append(
            _row(
                "last update",
                f"{text} ({last.get('at', '')})",
                state,
                ""
                if state == "ok"
                else f"{_cli()} update --check; send the support bundle if it repeats",
            )
        )
    else:
        rows.append(_row("last update", "none recorded on this node", "info"))
    return {"title": "Updates", "rows": rows}


def _archive(outbox_dir: Path | None) -> dict[str, Any] | None:
    from axiom.extensions.builtins.data_platform.ingest_sink.headroom import watched

    rows = []
    if outbox_dir is not None and Path(outbox_dir).is_dir():
        try:
            usage = shutil.disk_usage(outbox_dir)
        except OSError:
            usage = None
        if usage is not None:
            used = usage.used / usage.total if usage.total else 0.0
            free_gb = usage.free / 1e9
            state = "fail" if used >= DISK_FAIL else ("warn" if used >= DISK_WARN else "ok")
            fix = (
                ""
                if state == "ok"
                else "free space on that disk, or shorten the archive's retention"
            )
            rows.append(
                _row(
                    "disk",
                    f"{used:.0%} used, {free_gb:.1f} GB free where the archive is kept",
                    state,
                    fix,
                )
            )
    for d in watched():
        rows.append(_watched_row(d))
    reclaim = _reclaim_row()
    if reclaim is not None:
        rows.append(reclaim)
    return {"title": "Local archive", "rows": rows} if rows else None


def _watched_row(d: dict[str, Any]) -> dict[str, Any]:
    """One watched volume (AXIOM_DISK_WATCH), e.g. the local database's.

    Amber at the alarm floor (5 GiB or 10% free unless set), which comes well
    before full; red at the refusal floor. A database whose volume fills stops,
    so the warning has to come while there is still time to act.
    """
    label = f"{d['label']} disk"
    if d.get("error"):
        return _row(
            label,
            f"cannot read {d.get('path')}: {d['error']}",
            "fail",
            "check that the volume is mounted where the node watches it",
        )
    total, free = d["total_bytes"], d["free_bytes"]
    used = (total - free) / total if total else 0.0
    state = (
        "fail"
        if not d["ok"] or used >= DISK_FAIL
        else ("warn" if d["alarm"] or used >= DISK_WARN else "ok")
    )
    fix = (
        ""
        if state == "ok"
        else (
            f"free space on the {d['label']} volume or grow it; below "
            f"{d['floor_bytes'] / 1e9:.1f} GB free it stops accepting writes"
        )
    )
    return _row(label, f"{used:.0%} used, {free / 1e9:.1f} GB free", state, fix)


def _reclaim_row() -> dict[str, Any] | None:
    """The archive's last retention pass (``data archive-reclaim``), if one ran."""
    path = _state_dir() / "archive" / "reclaim.json"
    if not path.is_file():
        return None
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _row("retention", "the last retention pass could not be read", "warn",
                    f"{_cli()} data archive-reclaim  # rewrites it")
    when = _since(r.get("at", ""))
    freed = (f"rotated out {r.get('deleted_batches', 0)} delivered batch(es) and "
             f"{len(r.get('dropped_chunks') or [])} silver chunk(s)")
    kept = f"{r.get('kept_undelivered', 0)} not yet delivered upstream are kept"
    if r.get("alarm"):
        reasons = "; ".join((r.get("after") or {}).get("reasons") or [])
        return _row("retention", f"still under pressure {when} ago ({reasons}); {freed}; {kept}",
                    "fail", "free space, raise the budget, or shorten AXIOM_ARCHIVE_KEEP_DAYS")
    if (r.get("before") or {}).get("alarm"):
        return _row("retention", f"pressure cleared {when} ago: {freed}; {kept}", "ok")
    return _row("retention", f"no pressure at the last pass ({when} ago)", "ok")


def _conform() -> dict[str, Any] | None:
    """The last conform pass over each bronze root. An empty root is amber and
    a missing or doubled one red: either way silver has stopped growing, which
    otherwise looks exactly like a quiet day (C-51)."""
    path = _state_dir() / "conform" / "status.json"
    if not path.is_file():
        return None
    try:
        roots = dict(json.loads(path.read_text(encoding="utf-8")).get("roots") or {})
    except (OSError, ValueError):
        return {
            "title": "Conform",
            "rows": [_row("last pass", "the conform status file could not be read", "warn",
                          f"{_cli()} data conform-run  # rewrites it")],
        }
    rows = []
    for root, st in sorted(roots.items()):
        when = _since(st.get("at", ""))
        if st.get("config_error"):
            text = next((m for m in st.get("messages") or [] if "bronze root" in m), "")
            rows.append(_row(root, f"{text.removeprefix('⚠️').strip()} ({when} ago)", "fail",
                             "fix the configured bronze root, then rerun conform"))
        elif st.get("empty"):
            rows.append(_row(root, f"holds no rows: the last pass conformed nothing ({when} ago)",
                             "warn", "if this site is producing, the bronze root is wrong"))
        else:
            rows.append(_row(root, f"{st.get('rows_out', 0)} rows conformed from "
                                   f"{st.get('rows_in', 0)} ({when} ago)",
                             "ok" if st.get("ok") else "warn"))
    return {"title": "Conform", "rows": rows} if rows else None


def sections(cfg, *, outbox_dir=None, update_root=None, update_policy: str | None = None):
    """The ``axiom.node_status`` provider. ``None`` on a node without a role."""
    if not getattr(cfg, "confined", False):
        return None
    settings = dict(getattr(cfg, "settings", {}) or {})
    if outbox_dir is None:
        raw = os.environ.get("AXIOM_INGEST_OUTBOX_DIR") or settings.get("outbox_dir")
        outbox_dir = Path(raw) if raw else None
    if update_root is None:
        raw = os.environ.get(UPDATE_ROOT_ENV) or settings.get("update_root")
        update_root = Path(raw) if raw else None
    if update_policy is None:
        update_policy = str(
            os.environ.get(UPDATE_POLICY_ENV) or settings.get("update_policy") or ""
        )
    out = [
        s
        for s in (
            _sharing(outbox_dir),
            _updates(update_root, update_policy),
            _archive(outbox_dir),
            _conform(),
        )
        if s
    ]
    return out


__all__ = ["DISK_FAIL", "DISK_WARN", "sections"]
