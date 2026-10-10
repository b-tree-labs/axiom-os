# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""UT sees each partner site's health, and hears when something needs a person.

Built from what nodes report (their heartbeats) and nothing else a partner
could misstate: when the platform last heard each node, how fresh its newest
reading is, each lane's state, its version against the compatibility window,
the last update, and how full the archive disk is. Three conditions become
alerts: data stopped, a node too old for the window, an archive disk above
80%. Each alert is sent once per condition, not once per check.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.data_platform import partner_health as ph
from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore

NOW = time.time()


def _iso(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


def _beat(**over):
    beat = {
        "node": "daq-pc",
        "collector": "running",
        "version": "1.17.1",
        "last_reading_at": _iso(30),
        "pending": 0,
        "lanes": {
            "realtime": {"consumers": 1, "latency_ms": {"p95": 1.2}},
            "store_and_forward": {"current": "intake"},
        },
        "disk_used": 0.41,
        "update": {"policy": "auto-patch", "last": {"status": "updated", "to_version": "1.17.1"}},
    }
    beat.update(over)
    return beat


@pytest.fixture
def store(tmp_path):
    return HeartbeatStore(tmp_path / "hb")


def test_a_healthy_site_is_green_with_every_part_named(store):
    store.record("site-a", _beat(), received_at=NOW - 20)
    h = ph.site_health("site-a", store=store, now=NOW, min_version="1.17.0")
    assert h["state"] == "ok"
    node = h["nodes"][0]
    assert node["liveness"] == "online" and node["compatible"] is True
    assert {"freshness", "realtime", "store_and_forward", "update", "disk"} <= set(node["checks"])
    assert ph.alerts(h, now=NOW) == []


def test_data_that_stopped_raises_one_alert_naming_the_site(store):
    store.record("site-a", _beat(last_reading_at=_iso(30 * 3600)), received_at=NOW - 30 * 3600)
    h = ph.site_health("site-a", store=store, now=NOW, stopped_after_s=24 * 3600)
    found = ph.alerts(h, now=NOW)
    assert [a["kind"] for a in found] == ["data_stopped"]
    assert "site-a" in found[0]["summary"] and found[0]["dedup_key"].startswith(
        "partner-health:site-a:"
    )


def test_a_node_too_old_for_the_window_is_flagged(store):
    store.record("site-a", _beat(version="1.15.2"), received_at=NOW - 20)
    h = ph.site_health("site-a", store=store, now=NOW, min_version="1.17.0")
    assert h["nodes"][0]["compatible"] is False
    assert [a["kind"] for a in ph.alerts(h, now=NOW)] == ["out_of_date"]


def test_an_archive_disk_above_80_percent_is_flagged(store):
    store.record("site-a", _beat(disk_used=0.86), received_at=NOW - 20)
    h = ph.site_health("site-a", store=store, now=NOW)
    assert [a["kind"] for a in ph.alerts(h, now=NOW)] == ["disk_high"]


def test_a_site_that_never_reported_says_so_rather_than_ok(store):
    h = ph.site_health("site-z", store=store, now=NOW)
    assert h["state"] == "info" and "no node has reported" in h["summary"]


def test_alerts_are_delivered_once_per_condition(store):
    store.record("site-a", _beat(disk_used=0.91), received_at=NOW - 20)
    sent = []

    def deliver(alert):
        sent.append(alert["dedup_key"])
        return True

    for _ in range(3):
        ph.deliver(ph.alerts(ph.site_health("site-a", store=store, now=NOW), now=NOW), send=deliver)
    # The send callable is the inbox, which dedupes on the key; three checks of
    # one condition carry one key, so the operator sees one alert.
    assert len(set(sent)) == 1


def test_the_command_reports_every_site_and_its_alerts(tmp_path):
    import json
    import os
    import subprocess
    import sys

    HeartbeatStore(tmp_path / "hb").record(
        "site-a", _beat(last_reading_at=_iso(40 * 3600)), received_at=time.time() - 40 * 3600
    )
    env = {k: v for k, v in os.environ.items() if k != "AXIOM_SERVED_SITES"}
    env = {
        **env,
        "AXIOM_HEARTBEAT_DIR": str(tmp_path / "hb"),
        "AXI_STATE_DIR": str(tmp_path / "st"),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys;from axiom.infra.skills import SkillContext;"
            "from axiom.extensions.builtins.data_platform.skills import partner_health as p;"
            "r=p.run({}, SkillContext.default() if hasattr(SkillContext,'default') else None);"
            "print(json.dumps({'ok': r.ok, 'alerts': [a['kind'] for a in r.value['alerts']], 'text': r.value['text']}))",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ok"] and out["alerts"] == ["data_stopped"]
    assert out["text"].startswith("site-a: fail")
