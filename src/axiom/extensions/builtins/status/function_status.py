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
    catch = _catch_up_row(st.get("catch_up") or {})
    if catch is not None:
        rows.append(catch)
    return {"title": "Sharing upstream", "rows": rows}


def _catch_up_row(cu: dict[str, Any]) -> dict[str, Any] | None:
    """After an outage: how much backlog is left, the measured rate and ETA (W16).

    Live data goes first; the backlog drains behind it, paced by the intake's
    budgets. The ETA is the forwarder's own measurement over its last pass, so
    a row never shows a time nobody measured: before the rate exists it says so.
    """
    if not cu.get("active"):
        return None
    total = int(cu.get("backlog_total") or 0)
    remaining = int(cu.get("remaining") or 0)
    text = f"{remaining} of {total} batches still to send, live first"
    rate, eta = cu.get("records_per_s"), cu.get("eta_s")
    if rate and eta is not None:
        text += f"; {float(rate):.1f} batches/s, ETA {_duration(float(eta))}"
    else:
        text += "; no rate measured yet"
    if cu.get("oldest_pending_at"):
        text += f"; oldest waiting since {str(cu['oldest_pending_at'])[:16]}"
    now = datetime.now(UTC).timestamp()
    for key, what in (("backlog_wait_until", "the backlog lane"), ("wait_until", "everything")):
        until = cu.get(key)
        if until and float(until) > now:
            refused = []
            if cu.get("refused_429"):
                refused.append(f"{cu['refused_429']} × 429 over budget")
            if cu.get("refused_507"):
                refused.append(f"{cu['refused_507']} × 507 intake short of disk")
            why = ", ".join(refused) or "the intake asked"
            return _row(
                "catching up",
                f"paused: {what} waits {_duration(float(until) - now)} more ({why}); {text}",
                "warn",
                "nothing to do here: the intake sets the pace; it resumes by itself",
            )
    return _row("catching up", text, "info")


def _duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} days"


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
    rows.append(_retention_policy_row())
    rows.extend(_what_the_archive_holds(outbox_dir))
    return {"title": "Local archive", "rows": rows} if rows else None


def _retention_policy_row() -> dict[str, Any]:
    """The retention declared for this archive. None declared is said, not assumed:
    an archive that never deletes fills its disk, and the reclaim pass then
    has nothing it is allowed to remove."""
    from axiom.extensions.builtins.data_platform.archive_space import (
        BUDGET_ENV,
        KEEP_DAYS_ENV,
        ArchivePolicy,
    )

    try:
        policy = ArchivePolicy.from_env()
    except ValueError as exc:
        return _row("retention", f"the declared policy is invalid: {exc}", "fail", f"fix {KEEP_DAYS_ENV}")
    parts = []
    if policy.keep_days is not None:
        parts.append(f"keeps at least {policy.keep_days:g} days")
    if policy.budget_bytes is not None:
        parts.append(f"holds at most {policy.budget_bytes / 1e9:.1f} GB")
    if not parts:
        return _row(
            "retention",
            "none declared: nothing is ever deleted, so a full disk stops the archive",
            "warn",
            f"set {KEEP_DAYS_ENV} (days kept) and optionally {BUDGET_ENV}",
        )
    return _row("retention", "; ".join(parts) + "; oldest first under pressure", "ok")


def _what_the_archive_holds(outbox_dir: Path | None) -> list[dict[str, Any]]:
    """Compression and the oldest day, read from the archive itself.

    Silver is asked for its compression policy, how many of its chunks are
    compressed, and its oldest reading; bronze for its oldest batch still in
    the outbox. A database that cannot be reached is reported as exactly that.
    """
    rows: list[dict[str, Any]] = []
    oldest: list[str] = []
    try:
        import psycopg

        from axiom.extensions.builtins.data_platform._dsn import resolve_dsn

        with psycopg.connect(resolve_dsn(), connect_timeout=5, autocommit=True) as conn:
            rows.append(_compression_row(conn))
            cur = conn.execute("SELECT to_regclass('silver.signals')")
            if cur.fetchone()[0] is not None:
                # ADR-128 E5 diagnostic read of silver: the oldest day the archive holds.
                first = conn.execute("SELECT min(ts) FROM silver.signals").fetchone()[0]
                if first is not None:
                    oldest.append(f"silver from {first.strftime('%Y-%m-%d')}")
                else:
                    oldest.append("silver holds no readings yet")
            else:
                oldest.append("silver is not set up yet")
        db_ok = True
    except Exception as exc:  # noqa: BLE001 - say which question could not be answered
        why = f"{type(exc).__name__}: {str(exc).splitlines()[0][:80]}"
        rows.append(_row("compression", f"could not be read: the archive database did not answer ({why})",
                         "warn", f"check the local database; {_cli()} status --system shows it"))
        db_ok = False
    if outbox_dir is not None and Path(outbox_dir).is_dir():
        try:
            from axiom.extensions.builtins.data_platform.ingest_sink.edge import EdgeOutbox

            first = EdgeOutbox(outbox_dir).read(after=0, limit=1)
            if first:
                oldest.append(f"bronze batch waiting since {str(first[0].get('received_at', ''))[:10]}")
            else:
                oldest.append("no bronze batch waiting")
        except Exception:  # noqa: BLE001
            oldest.append("bronze outbox could not be read")
    if db_ok:
        rows.append(_row("oldest day", "; ".join(oldest) or "nothing held yet", "ok"))
    else:
        rows.append(_row("oldest day", "; ".join(oldest) or "unknown", "warn",
                         "the archive database did not answer"))
    return rows


def _interval_as_days(raw: str) -> str:
    """Timescale returns a day policy as ``48:00:00``. Status says ``2 days``."""
    text = raw.replace("@ ", "").strip()
    parts = text.split(":")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        hours, minutes, seconds = (int(part) for part in parts)
        if minutes == 0 and seconds == 0 and hours > 0 and hours % 24 == 0:
            days = hours // 24
            return "1 day" if days == 1 else f"{days} days"
    return text


def _compression_row(conn) -> dict[str, Any]:
    from axiom.extensions.builtins.data_platform.conformance.timeseries import is_time_partitioned

    with conn.cursor() as cur:
        if not is_time_partitioned(cur):
            return _row("compression", "silver is not time-partitioned here: nothing is compressed", "info")
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE is_compressed) FROM timescaledb_information.chunks "
            "WHERE hypertable_schema = 'silver' AND hypertable_name = 'signals'"
        )
        total, compressed = cur.fetchone()
        cur.execute(
            "SELECT config->>'compress_after' FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_compression' AND hypertable_schema = 'silver' "
            "AND hypertable_name = 'signals'"
        )
        found = cur.fetchone()
    if not found:
        return _row("compression", f"no compression policy; {compressed} of {total} chunks compressed", "warn",
                    f"{_cli()} data conform-run applies the time-series policy")
    after = _interval_as_days(str(found[0] or ""))
    return _row("compression", f"after {after}; {compressed} of {total} chunks compressed", "ok")


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
