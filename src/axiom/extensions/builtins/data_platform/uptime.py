# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Uptime per installation, each downtime attributed to one cause (ADR-182 D5a).

Two observers per node: its **heartbeat** ("I am up", from the inside) and the
front door's **probe** ("I can reach you", from the outside). A downtime
interval is any span in which either observer saw the node down, and it keeps
both readings, so "down" and "up but unreachable" stay distinct.

Each interval gets exactly one cause, checked in this order:

* **ours** (deploy, update, migration, config): it overlaps a change record
  Axiom wrote before acting.
* **outside**, only on positive evidence: a recorded device fault (hardware);
  a fresh boot with no clean-shutdown record (power); the probe failing while
  the heartbeat kept going (network).
* **unattributed**: no evidence either way. Never counted as outside: a claim
  that held because evidence was missing would not be a claim.

A site has shown "never down from our own causes" for a period only when both
its our-cause and its unattributed downtime are zero.

Evidence comes in as plain lists, so this module decides nothing about where
it is stored. :func:`site_report` reads what the platform holds today (the
heartbeat history) and names the evidence it could not find.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from axiom.infra.topology import ONLINE_WITHIN_S

CAUSES = ("ours", "outside", "unattributed")
OURS_KINDS = ("deploy", "update", "migration", "config")
OUTSIDE_KINDS = ("power", "network", "hardware")


@dataclass(frozen=True)
class Interval:
    node: str
    start: float
    end: float
    heartbeat_down: bool
    probe_down: bool
    cause: str
    kind: str
    evidence: str

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)


def _spans_from_beats(beats: list[float], *, now: float, gap_s: float) -> list[tuple[float, float]]:
    """Down spans: a gap longer than ``gap_s`` between beats, or silence until now."""
    beats = sorted(beats)
    out = []
    for a, b in zip(beats, beats[1:]):
        if b - a > gap_s:
            out.append((a, b))
    if beats and now - beats[-1] > gap_s:
        out.append((beats[-1], now))
    return out


def _spans_from_probe(probe: list[tuple[float, bool]], *, now: float) -> list[tuple[float, float]]:
    """Down spans: from a failed probe until the next one that succeeds (or now)."""
    out, start = [], None
    for at, ok in sorted(probe):
        if not ok and start is None:
            start = at
        elif ok and start is not None:
            out.append((start, at))
            start = None
    if start is not None:
        out.append((start, now))
    return out


def _overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _merge(
    hb: list[tuple[float, float]], pr: list[tuple[float, float]]
) -> list[tuple[float, float, bool, bool]]:
    tagged = sorted([(s, e, "hb") for s, e in hb] + [(s, e, "pr") for s, e in pr])
    merged: list[list] = []
    for s, e, who in tagged:
        if merged and s <= merged[-1][1]:
            m = merged[-1]
            m[1] = max(m[1], e)
            m[2] |= who == "hb"
            m[3] |= who == "pr"
        else:
            merged.append([s, e, who == "hb", who == "pr"])
    return [tuple(m) for m in merged]


def _ts(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    return datetime.fromisoformat(str(v)).timestamp()


def _attribute(
    span: tuple[float, float], hb_down: bool, pr_down: bool, *, changes, boots, faults, gap_s: float
) -> tuple[str, str, str]:
    for c in changes:
        start = _ts(c["start"])
        # A change with no end never finished (the crash case): it stays open,
        # overlapping everything after it started.
        end = _ts(c["end"]) if c.get("end") else float("inf")
        if _overlaps(span, (start, end)):
            return (
                "ours",
                str(c.get("kind") or "config"),
                f"change record: {c.get('kind')} at {c['start']}",
            )
    for f in faults:
        start = _ts(f["start"])
        if _overlaps(span, (start, _ts(f.get("end") or span[1]))):
            return "outside", "hardware", f"device fault: {f.get('what') or 'recorded'}"
    for b in boots:
        at = _ts(b["at"])
        if span[0] <= at <= span[1] + gap_s and not b.get("clean_shutdown"):
            return "outside", "power", "fresh boot with no shutdown record"
    if pr_down and not hb_down:
        return "outside", "network", "heartbeat continued while the probe failed"
    return "unattributed", "", "no evidence either way"


def intervals(
    node: str,
    *,
    heartbeat: list[float] | None = None,
    probe: list[tuple[float, bool]] | None = None,
    changes: list[dict] | None = None,
    boots: list[dict] | None = None,
    faults: list[dict] | None = None,
    now: float | None = None,
    gap_s: float = ONLINE_WITHIN_S,
) -> list[Interval]:
    """One node's downtime intervals over the evidence given, each with its cause."""
    now = time.time() if now is None else now
    hb = _spans_from_beats(list(heartbeat or ()), now=now, gap_s=gap_s)
    pr = _spans_from_probe(list(probe or ()), now=now)
    out = []
    for s, e, hb_down, pr_down in _merge(hb, pr):
        cause, kind, why = _attribute(
            (s, e),
            hb_down,
            pr_down,
            changes=list(changes or ()),
            boots=list(boots or ()),
            faults=list(faults or ()),
            gap_s=gap_s,
        )
        out.append(Interval(node, s, e, hb_down, pr_down, cause, kind, why))
    return out


def site_uptime(
    site: str, by_node: dict[str, list[Interval]], *, period: tuple[float, float]
) -> dict[str, Any]:
    """All of a site's installations, aggregated; each interval keeps its node."""
    t0, t1 = period
    span = max(1.0, t1 - t0) * max(1, len(by_node))
    clipped = []
    for ivs in by_node.values():
        for iv in ivs:
            s, e = max(iv.start, t0), min(iv.end, t1)
            if e > s:
                clipped.append((iv, e - s))
    totals = {c: sum(sec for iv, sec in clipped if iv.cause == c) for c in CAUSES}
    outside = {
        k: sum(sec for iv, sec in clipped if iv.cause == "outside" and iv.kind == k)
        for k in OUTSIDE_KINDS
    }
    ours = {
        k: sum(sec for iv, sec in clipped if iv.cause == "ours" and iv.kind == k)
        for k in OURS_KINDS
    }
    down = sum(sec for _, sec in clipped)
    return {
        "site": site,
        "period": {"from": t0, "to": t1},
        "nodes": sorted(by_node),
        "uptime": round(1 - down / span, 6),
        "ours_s": totals["ours"],
        "ours_by_kind": ours,
        "unattributed_s": totals["unattributed"],
        "outside_s": totals["outside"],
        "outside_by_kind": outside,
        # Only both zero shows the claim; an unexplained gap is not a pass.
        "demonstrated": totals["ours"] == 0 and totals["unattributed"] == 0,
        "intervals": [{**asdict(iv), "seconds": sec} for iv, sec in clipped],
    }


#: Evidence the platform does not receive yet. Named in every report, so a
#: zero is never read as more than the evidence behind it supports.
MISSING = (
    "front-door probe of each node (only the heartbeat observer exists)",
    "change records written before a deploy, update, migration or config change",
    "boot and clean-shutdown records from nodes",
    "device-fault records",
)


def site_report(
    site: str,
    *,
    store=None,
    probes=None,
    now: float | None = None,
    period_s: float = 7 * 86400,
) -> dict[str, Any]:
    """A site's uptime from what the platform holds: its nodes' heartbeat history."""
    from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore

    if store is None:
        # Production: the node's own stores. A caller passing a store (a test,
        # a replay) passes the probe readings it means too, or none.
        from axiom.extensions.builtins.data_platform.uptime_probe import ProbeStore

        store = HeartbeatStore.from_env()
        probes = probes if probes is not None else ProbeStore.from_env()
    now = time.time() if now is None else now
    by_node = {}
    earliest = now
    saw_changes = False
    saw_boots = False
    reachable: list[str] = []
    unchecked: list[str] = []
    saw_receipts = False
    # What each node says it records (its latest declaration), so a node that
    # can record deploys but has not deployed is told apart from one that cannot.
    declared: dict[str, set[str] | None] = {}
    for n in store.nodes(site):
        latest = n.get("latest") or {}
        kinds = latest.get("records_changes")
        declared[n["node"]] = {str(k) for k in kinds} if isinstance(kinds, list) else None
        hist = store.history(site, n["node"])
        beats = []
        received: list[float] = []
        shipped: dict[str, dict] = {}
        boots: dict[str, dict] = {}
        for h in hist:
            beat = h.get("beat") or {}
            if isinstance(beat.get("boots"), list):
                saw_boots = True
                for b in beat["boots"]:
                    if isinstance(b, dict) and b.get("at"):
                        boots[str(b["at"])] = b
            for missed in beat.get("undelivered_sent_at") or ():
                # Sent during an outage and never delivered: the node was up
                # (its heartbeat counts) and nothing arrived (no receipt).
                try:
                    beats.append(_ts(missed))
                except ValueError:
                    continue
            if beat.get("sent_at") and h.get("received_at"):
                # The outside observer of a push-only node (ADR-183): when its
                # beats ARRIVE, as opposed to when it says it sent them.
                received.append(float(h["received_at"]))
            if isinstance(beat.get("changes"), list):
                saw_changes = True
                # The same id arrives in several beats; its latest state wins,
                # so an intent line is replaced by its outcome once it ships.
                for c in beat["changes"]:
                    if isinstance(c, dict) and c.get("id") and c.get("started"):
                        shipped[str(c["id"])] = c
            # The node's own send time when it gives one, so a beat delivered
            # late after a network outage still counts as the node being up.
            beats.append(
                _ts(beat["sent_at"]) if beat.get("sent_at") else float(h.get("received_at") or 0)
            )
        changes = [
            {"kind": c.get("kind"), "start": c["started"], "end": c.get("ended")}
            for c in shipped.values()
        ]
        # An active check from the front door is the better outside observer;
        # arrival times stand in only for a node nobody can reach.
        checked = probes.readings(site, n["node"]) if probes is not None else []
        latest = (hist[-1].get("beat") or {}) if hist else {}
        if latest.get("probe_url"):
            reachable.append(n["node"])
            if not checked:
                unchecked.append(n["node"])
        if checked:
            probe = checked
        else:
            probe = _probe_from_receipts(received, now=now) if received else None
        saw_receipts = saw_receipts or bool(received)
        by_node[n["node"]] = intervals(
            n["node"],
            heartbeat=beats,
            probe=probe,
            changes=changes,
            boots=list(boots.values()),
            now=now,
        )
        earliest = min([earliest, *beats])
    # Only the time actually observed: counting unobserved time as up would
    # overstate uptime by exactly the evidence that is missing.
    report = site_uptime(site, by_node, period=(max(now - period_s, earliest), now))
    report["observed_from"] = report["period"]["from"]
    missing = list(MISSING)
    if any(v is not None for v in declared.values()):
        # Keyed on what every node declares it records: one node that does not
        # record a kind leaves that kind unproven for the whole site.
        gaps = {
            kind: sorted(node for node, kinds in declared.items() if not kinds or kind not in kinds)
            for kind in OURS_KINDS
        }
        gaps = {k: v for k, v in gaps.items() if v}
        if not gaps:
            missing.remove(MISSING[1])
        else:
            by_nodes: dict[tuple[str, ...], list[str]] = {}
            for kind, nodes in gaps.items():
                by_nodes.setdefault(tuple(nodes), []).append(kind)
            missing[1] = "; ".join(
                f"{', '.join(kinds)} change records from {', '.join(nodes)}"
                for nodes, kinds in by_nodes.items()
            )
    elif saw_changes:
        # Nodes ship their change ledger but do not say which kinds it holds;
        # deploys are the kind a collector-only node does not write.
        missing[1] = "deploy change records (update, migration and config changes are recorded)"
    if reachable and not unchecked and probes is not None:
        missing[0] = ""  # every reachable node is checked from outside
    elif unchecked:
        missing[0] = "active probe of " + ", ".join(sorted(unchecked)) + " (reachable, not yet checked)"
    elif saw_receipts:
        missing[0] = (
            "active probe of nodes the front door can reach (push-only nodes are "
            "observed by when their beats arrive)"
        )
    if saw_boots:
        missing.remove("boot and clean-shutdown records from nodes")
    report["evidence_missing"] = [m for m in missing if m]
    report["records_changes"] = {
        n: sorted(k) if k is not None else None for n, k in declared.items()
    }
    return report


def _probe_from_receipts(received: list[float], *, now: float, gap_s: float = ONLINE_WITHIN_S):
    """Turn arrival times into probe readings: a gap in arrivals is a failed probe.

    A node that kept sending while nothing arrived was up behind a broken link;
    paired with its own ``sent_at`` timeline, that reads as network, not down.
    """
    points: list[tuple[float, bool]] = []
    times = sorted(received)
    for a, b in zip(times, times[1:]):
        points.append((a, True))
        if b - a > gap_s:
            points.append((a + gap_s, False))
    if times:
        points.append((times[-1], True))
        if now - times[-1] > gap_s:
            points.append((times[-1] + gap_s, False))
    return points


__all__ = [
    "CAUSES",
    "Interval",
    "MISSING",
    "OURS_KINDS",
    "OUTSIDE_KINDS",
    "intervals",
    "site_report",
    "site_uptime",
]
