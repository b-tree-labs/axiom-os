# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Each partner site's health, from its nodes' heartbeats, and the alerts it raises.

The platform's view of a site it does not run: when each sending node was last
heard, how fresh its newest reading is, the state of each lane it reports,
whether its version is inside the compatibility window, its last update, and
how full its archive disk is. Every check is a state, one plain sentence, and
what to do. A site whose nodes have never reported says exactly that; it is
never shown as fine.

Three conditions need a person and become alerts:

* ``data_stopped``: no new reading within the site's window (24 h unless the
  site sets one). This is the failure a partner cannot see from their side.
* ``out_of_date``: a node older than the oldest version the intake accepts.
* ``disk_high``: an archive disk above 80%.

An alert's ``dedup_key`` names the site, node, condition and day, so a check
that runs every few minutes produces one alert per condition per day in the
inbox, not one per check.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore, liveness

#: No new reading for this long means a site's data has stopped.
STOPPED_AFTER_S = 24 * 3600
DISK_WARN = 0.80
_RANK = {"ok": 0, "info": 1, "warn": 2, "fail": 3}


def _check(state: str, text: str, fix: str = "") -> dict[str, str]:
    return {"state": state, "text": text, "fix": fix}


def _age_words(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 120:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} days"


def _ts(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _version_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in str(v).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _node_health(
    site: str, node: dict[str, Any], *, now: float, stopped_after_s: float, min_version: str
) -> dict[str, Any]:
    beat = dict(node.get("latest") or {})
    heard = float(node.get("received_at") or 0)
    checks: dict[str, dict[str, str]] = {}

    live = liveness(node, now=now)
    checks["heard"] = _check(
        {"online": "ok", "late": "warn"}.get(live, "fail"),
        f"last heard {_age_words(now - heard)} ago ({live})",
        ""
        if live == "online"
        else "the node is off, off the network, or its collector stopped; contact the site",
    )

    reading = _ts(beat.get("last_reading_at"))
    if reading is None:
        checks["freshness"] = _check("info", "no reading reported yet")
    else:
        age = now - reading
        stopped = age > stopped_after_s
        checks["freshness"] = _check(
            "fail" if stopped else ("warn" if age > stopped_after_s / 4 else "ok"),
            f"newest reading {_age_words(age)} old",
            f"data from {site} has stopped; contact the site" if stopped else "",
        )

    lanes = dict(beat.get("lanes") or {})
    rt = dict(lanes.get("realtime") or {})
    if rt:
        p95 = (
            (rt.get("latency_ms") or {}).get("p95")
            if isinstance(rt.get("latency_ms"), dict)
            else None
        )
        text = f"{int(rt.get('consumers') or 0)} consumer(s)"
        if p95 is not None:
            text += f", {float(p95):.1f} ms p95"
        checks["realtime"] = _check("ok", text)
    sf = dict(lanes.get("store_and_forward") or {})
    if sf:
        current = str(sf.get("current") or "")
        if current in ("", "intake"):
            checks["store_and_forward"] = _check(
                "ok", f"sending to the intake; {int(beat.get('pending') or 0)} waiting"
            )
        elif current == "box":
            checks["store_and_forward"] = _check(
                "info", "sending through Box: the intake was not reachable from the site"
            )
        else:
            checks["store_and_forward"] = _check(
                "warn",
                f"waiting: {sf.get('reason') or 'no transport available'}",
                "check the site's network and intake key",
            )

    version = str(beat.get("version") or "")
    compatible = None
    if version and min_version:
        compatible = _version_tuple(version) >= _version_tuple(min_version)
        checks["version"] = _check(
            "ok" if compatible else "fail",
            f"{version}"
            + ("" if compatible else f", older than {min_version}, the oldest the intake accepts"),
            ""
            if compatible
            else "the site's node needs an update; its archive keeps everything meanwhile",
        )
    elif version:
        checks["version"] = _check("info", version)

    update = dict(beat.get("update") or {})
    if update:
        last = dict(update.get("last") or {})
        status = str(last.get("status") or "")
        bad = status in ("rolled_back", "rejected_before_switch", "install_failed")
        text = f"policy {update.get('policy') or 'unset'}"
        if status:
            text += f"; last: {status.replace('_', ' ')} {last.get('to_version') or ''}".rstrip()
        if update.get("pending"):
            text += f"; {update['pending']} waiting for approval"
        checks["update"] = _check(
            "warn" if bad else "ok",
            text,
            "look at the update log in the site's support bundle" if bad else "",
        )

    disk = beat.get("disk_used")
    if disk is not None or beat.get("disk_alarm"):
        used = float(disk) if disk is not None else None
        # The node's own alarm (a floor in bytes or percent free) can come
        # before 80%: a database volume of 20 GB is in trouble at 2 GB free.
        alarmed = [d["label"] for d in beat.get("disks") or [] if d.get("alarm")]
        bad = bool(beat.get("disk_alarm")) or (used is not None and used > DISK_WARN)
        text = f"archive disk {used:.0%} used" if used is not None else "archive disk alarm"
        if alarmed:
            text += f"; low on space: {', '.join(alarmed)}"
        checks["disk"] = _check(
            "warn" if bad else "ok",
            text,
            "the site should free space or shorten retention" if bad else "",
        )

    state = max((c["state"] for c in checks.values()), key=lambda s: _RANK[s], default="info")
    return {
        "node": node.get("node"),
        "liveness": live,
        "heard_at": heard,
        "version": version,
        "compatible": compatible,
        "state": state,
        "checks": checks,
    }


def site_health(
    site: str,
    *,
    store: HeartbeatStore | None = None,
    now: float | None = None,
    stopped_after_s: float = STOPPED_AFTER_S,
    min_version: str = "",
) -> dict[str, Any]:
    """One site's health from its nodes' latest heartbeats."""
    store = store or HeartbeatStore.from_env()
    now = time.time() if now is None else now
    nodes = [
        _node_health(site, n, now=now, stopped_after_s=stopped_after_s, min_version=min_version)
        for n in store.nodes(site)
    ]
    if not nodes:
        return {
            "site": site,
            "state": "info",
            "summary": "no node has reported from this site yet",
            "nodes": [],
        }
    state = max((n["state"] for n in nodes), key=lambda s: _RANK[s])
    worst = [
        f"{n['node']}: {c['text']}"
        for n in nodes
        for c in n["checks"].values()
        if c["state"] == state
    ]
    summary = "every node reporting and current" if state == "ok" else "; ".join(worst[:3])
    return {"site": site, "state": state, "summary": summary, "nodes": nodes}


def alerts(health: dict[str, Any], *, now: float | None = None) -> list[dict[str, Any]]:
    """The conditions in ``health`` that need a person, one alert each."""
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
    site = health["site"]
    out = []
    for n in health.get("nodes", ()):
        checks = n["checks"]
        found = []
        if checks.get("freshness", {}).get("state") == "fail":
            found.append(
                (
                    "data_stopped",
                    "high",
                    f"Data from {site} has stopped: {n['node']}'s {checks['freshness']['text']}.",
                )
            )
        if n.get("compatible") is False:
            found.append(
                (
                    "out_of_date",
                    "normal",
                    f"{site}'s node {n['node']} runs {checks['version']['text']}.",
                )
            )
        if checks.get("disk", {}).get("state") == "warn":
            found.append(
                ("disk_high", "normal", f"{site}'s node {n['node']}: {checks['disk']['text']}.")
            )
        for kind, priority, summary in found:
            out.append(
                {
                    "kind": kind,
                    "site": site,
                    "node": n["node"],
                    "priority": priority,
                    "summary": summary,
                    "dedup_key": f"partner-health:{site}:{n['node']}:{kind}:{day}",
                }
            )
    return out


def deliver(found: list[dict[str, Any]], *, send: Callable[[dict[str, Any]], bool]) -> int:
    """Hand each alert to ``send`` (the inbox, which dedupes on the key). Returns how many it accepted."""
    return sum(1 for a in found if send(a))


def inbox_sender(
    recipient: str, *, actor: str = "@partner-health:platform"
) -> Callable[[dict[str, Any]], bool]:
    """A ``send`` that writes to the notifications inbox; Herald delivers from there."""

    def _send(alert: dict[str, Any]) -> bool:
        from axiom.extensions.builtins.notifications.inbox_db import DatabaseInboxStore

        DatabaseInboxStore().write_alert(
            recipient=recipient,
            summary=alert["summary"],
            priority=alert["priority"],
            classification="internal",
            actor=actor,
            link="",
            dedup_key=alert["dedup_key"],
        )
        return True

    return _send


__all__ = ["DISK_WARN", "STOPPED_AFTER_S", "alerts", "deliver", "inbox_sender", "site_health"]
