# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Uptime per installation, every downtime attributed to exactly one cause (ADR-182 D5a).

Two observers per node: its heartbeat (inside) and the front door's probe
(outside). A downtime interval is any span in which either saw the node down.
"ours" needs an overlapping change record; "outside" needs positive evidence;
anything else is "unattributed", which is never counted as outside.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform import uptime as up

T0 = 1_760_000_000.0
MIN = 60.0


def _beats(start: float, end: float, *, skip: tuple[float, float] | None = None) -> list[float]:
    """One beat a minute from ``start`` to ``end``, none inside ``skip``."""
    out, t = [], start
    while t <= end:
        if not (skip and skip[0] < t < skip[1]):
            out.append(t)
        t += MIN
    return out


def _probes(
    start: float, end: float, *, down: tuple[float, float] | None = None
) -> list[tuple[float, bool]]:
    out, t = [], start
    while t <= end:
        out.append((t, not (down and down[0] <= t < down[1])))
        t += MIN
    return out


END = T0 + 120 * MIN
GAP = (T0 + 30 * MIN, T0 + 50 * MIN)


def _one(**evidence):
    found = up.intervals("node-a", now=END, **evidence)
    assert len(found) == 1, found
    return found[0]


def test_a_deploy_gap_is_ours():
    iv = _one(
        heartbeat=_beats(T0, END, skip=GAP),
        probe=_probes(T0, END, down=GAP),
        changes=[{"kind": "deploy", "start": GAP[0] - 10, "end": GAP[0] + 60}],
    )
    assert (iv.cause, iv.kind) == ("ours", "deploy")
    assert iv.heartbeat_down and iv.probe_down


def test_a_reboot_with_no_shutdown_record_is_power():
    iv = _one(
        heartbeat=_beats(T0, END, skip=GAP),
        probe=_probes(T0, END, down=GAP),
        boots=[{"at": GAP[1] - 2 * MIN, "clean_shutdown": False}],
    )
    assert (iv.cause, iv.kind) == ("outside", "power")


def test_a_failing_probe_while_the_heartbeat_continues_is_network():
    iv = _one(heartbeat=_beats(T0, END), probe=_probes(T0, END, down=GAP))
    assert (iv.cause, iv.kind) == ("outside", "network")
    assert iv.probe_down and not iv.heartbeat_down


def test_a_gap_with_no_evidence_is_unattributed_and_not_outside():
    iv = _one(heartbeat=_beats(T0, END, skip=GAP), probe=_probes(T0, END, down=GAP))
    assert iv.cause == "unattributed"
    report = up.site_uptime("site-a", {"node-a": [iv]}, period=(T0, END))
    assert report["outside_s"] == 0 and report["unattributed_s"] > 0
    assert report["demonstrated"] is False


def test_a_clean_reboot_is_not_power():
    iv = _one(
        heartbeat=_beats(T0, END, skip=GAP),
        probe=_probes(T0, END, down=GAP),
        boots=[{"at": GAP[1] - 2 * MIN, "clean_shutdown": True}],
    )
    assert iv.cause == "unattributed"


def test_two_nodes_of_one_site_aggregate_and_keep_their_installation():
    a = up.intervals(
        "node-a",
        now=END,
        heartbeat=_beats(T0, END, skip=GAP),
        probe=_probes(T0, END, down=GAP),
        changes=[{"kind": "update", "start": GAP[0], "end": GAP[1]}],
    )
    b = up.intervals("node-b", now=END, heartbeat=_beats(T0, END), probe=_probes(T0, END, down=GAP))
    report = up.site_uptime("site-a", {"node-a": a, "node-b": b}, period=(T0, END))
    assert report["ours_s"] > 0 and report["outside_by_kind"]["network"] > 0
    assert {i["node"] for i in report["intervals"]} == {"node-a", "node-b"}
    assert report["demonstrated"] is False


def test_a_site_with_no_downtime_has_demonstrated_the_claim():
    iv = up.intervals("node-a", now=END, heartbeat=_beats(T0, END), probe=_probes(T0, END))
    report = up.site_uptime("site-a", {"node-a": iv}, period=(T0, END))
    assert iv == [] and report["demonstrated"] is True and report["uptime"] == 1.0


def test_a_node_silent_until_now_is_down_until_now():
    found = up.intervals("node-a", now=END, heartbeat=_beats(T0, T0 + 60 * MIN))
    assert len(found) == 1 and found[0].end == END and found[0].cause == "unattributed"


def test_heartbeat_history_is_read_from_the_store(tmp_path):
    from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore

    store = HeartbeatStore(tmp_path)
    for t in _beats(T0, END, skip=GAP):
        store.record("site-a", {"node": "node-a"}, received_at=t)
    report = up.site_report("site-a", store=store, now=END, period_s=END - T0)
    assert report["unattributed_s"] > 15 * MIN
    assert "probe" in " ".join(report["evidence_missing"])


def test_change_records_shipped_in_heartbeats_attribute_the_gap_to_us(tmp_path):
    from datetime import UTC, datetime

    from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore

    def iso(t):
        return datetime.fromtimestamp(t, UTC).isoformat()

    store = HeartbeatStore(tmp_path)
    intent = {"id": "c1", "kind": "update", "started": iso(GAP[0] + 30), "ended": None}
    done = {**intent, "ended": iso(GAP[1] - 30), "outcome": "updated"}
    for t in _beats(T0, END, skip=GAP):
        # The node ships its recent changes in every beat until they age out;
        # the same id twice is one change, its latest state wins.
        shipped = [intent] if t < GAP[0] else [done]
        store.record("site-a", {"node": "node-a", "changes": shipped}, received_at=t)
    report = up.site_report("site-a", store=store, now=END, period_s=END - T0)
    (iv,) = report["intervals"]
    assert (iv["cause"], iv["kind"]) == ("ours", "update")
    assert report["ours_s"] > 0 and report["unattributed_s"] == 0


def test_a_change_that_never_finished_stays_open_until_now():
    iv = _one(
        heartbeat=_beats(T0, END, skip=GAP),
        probe=_probes(T0, END, down=GAP),
        changes=[{"kind": "deploy", "start": GAP[0] - 600, "end": None}],
    )
    assert (iv.cause, iv.kind) == ("ours", "deploy")


# -- Evidence a push-only node can give (ADR-182 D5a) ---------------------------
#
# A partner node is outbound-only (ADR-183): no front door can probe it. Its
# outside observer is when its beats ARRIVE. A beat carries its own sent_at, so
# beats sent steadily but received in a burst mean the node was up and the
# network was not.


def _iso(t: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(t, UTC).isoformat()


def test_beats_sent_steadily_but_received_late_are_a_network_outage(tmp_path):
    from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore

    store = HeartbeatStore(tmp_path)
    for t in _beats(T0, END):
        # Buffered during the gap, delivered together when the link returns.
        received = GAP[1] if GAP[0] < t < GAP[1] else t
        store.record("site-a", {"node": "node-a", "sent_at": _iso(t)}, received_at=received)
    report = up.site_report("site-a", store=store, now=END, period_s=END - T0)
    (iv,) = report["intervals"]
    assert (iv["cause"], iv["kind"]) == ("outside", "network"), iv
    assert iv["heartbeat_down"] is False and iv["probe_down"] is True
    assert not any("probe" in m and "push-only" not in m for m in report["evidence_missing"])


def test_a_boot_shipped_without_a_clean_stop_is_power(tmp_path):
    from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore

    store = HeartbeatStore(tmp_path)
    boot = {"at": _iso(GAP[1] - 120), "clean_shutdown": False}
    for t in _beats(T0, END, skip=GAP):
        beat = {"node": "node-a", "sent_at": _iso(t)}
        if t >= GAP[1]:
            beat["boots"] = [boot]
        store.record("site-a", beat, received_at=t)
    report = up.site_report("site-a", store=store, now=END, period_s=END - T0)
    (iv,) = report["intervals"]
    assert (iv["cause"], iv["kind"]) == ("outside", "power"), iv
    assert not any("boot" in m for m in report["evidence_missing"])


def test_a_clean_reboot_shipped_by_the_node_is_not_power(tmp_path):
    from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore

    store = HeartbeatStore(tmp_path)
    boot = {"at": _iso(GAP[1] - 120), "clean_shutdown": True}
    for t in _beats(T0, END, skip=GAP):
        beat = {"node": "node-a", "sent_at": _iso(t)}
        if t >= GAP[1]:
            beat["boots"] = [boot]
        store.record("site-a", beat, received_at=t)
    (iv,) = up.site_report("site-a", store=store, now=END, period_s=END - T0)["intervals"]
    assert iv["cause"] == "unattributed", iv


def test_beats_undelivered_during_an_outage_and_reported_later_are_network(tmp_path):
    """What the real sender does: beats fail during the outage and their send
    times ride on the first beat that gets through."""
    from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore

    store = HeartbeatStore(tmp_path)
    missed: list[str] = []
    for t in _beats(T0, END):
        if GAP[0] < t < GAP[1]:
            missed.append(_iso(t))
            continue
        beat = {"node": "node-a", "sent_at": _iso(t)}
        if missed:
            beat["undelivered_sent_at"], missed = missed, []
        store.record("site-a", beat, received_at=t)
    (iv,) = up.site_report("site-a", store=store, now=END, period_s=END - T0)["intervals"]
    assert (iv["cause"], iv["kind"]) == ("outside", "network"), iv


ALL_KINDS = ["deploy", "update", "migration", "config"]


def _declaring(tmp_path, declared: dict):
    from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore

    store = HeartbeatStore(tmp_path)
    for node, kinds in declared.items():
        beat = {"node": node}
        if kinds is not None:
            beat["records_changes"] = kinds
        store.record("site-a", beat, received_at=END - 30)
    return up.site_report("site-a", store=store, now=END, period_s=3600)


def _change_entries(report):
    return [m for m in report["evidence_missing"] if "change record" in m]


def test_no_change_caveat_when_every_node_declares_every_kind(tmp_path):
    report = _declaring(tmp_path, {"node-a": ALL_KINDS, "node-b": ALL_KINDS})
    assert _change_entries(report) == []


def test_a_node_that_does_not_record_deploys_is_named(tmp_path):
    report = _declaring(
        tmp_path, {"node-a": ALL_KINDS, "node-b": ["update", "migration", "config"]}
    )
    (entry,) = _change_entries(report)
    assert "deploy" in entry and "node-b" in entry and "node-a" not in entry


def test_a_node_that_declares_nothing_leaves_every_kind_open(tmp_path):
    report = _declaring(tmp_path, {"node-a": ALL_KINDS, "node-b": None})
    (entry,) = _change_entries(report)
    assert "node-b" in entry and all(k in entry for k in ALL_KINDS)


def test_an_empty_declaration_is_not_mistaken_for_a_full_one(tmp_path):
    report = _declaring(tmp_path, {"node-a": []})
    (entry,) = _change_entries(report)
    assert "node-a" in entry and "deploy" in entry
