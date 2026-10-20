# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The front door checks every node it can reach (ADR-182 D5a, outside observer).

A node's heartbeat says "I am up" from the inside. For a node the platform can
reach, the outside observer is an active check of its /readyz: a node that
keeps beating while the check fails was up behind a broken path, which is a
network outage and not ours. A push-only node advertises no address and is
not checked; its outside observer is when its beats arrive.

Real servers and real sockets throughout: the claim is about what a remote
observer sees.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI

from axiom.extensions.builtins.data_platform import uptime as up
from axiom.extensions.builtins.data_platform import uptime_probe as probe
from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore
from axiom.extensions.builtins.http.server import ThreadedServer, install_readiness

T0 = 1_760_000_000.0
MIN = 60.0


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def ready_node():
    app = FastAPI()
    install_readiness(app)
    with ThreadedServer(app).serving() as srv:
        yield srv.base_url


@pytest.fixture
def healthz_only_node():
    """An older node: no /readyz, only /healthz."""
    app = FastAPI()

    @app.get("/healthz")
    def _h():
        return {"ok": True}

    with ThreadedServer(app).serving() as srv:
        yield srv.base_url


def _beat(store: HeartbeatStore, site: str, node: str, url: str | None, t: float) -> None:
    beat = {"node": node, "sent_at": datetime.fromtimestamp(t, UTC).isoformat()}
    if url is not None:
        beat["probe_url"] = url
    store.record(site, beat, received_at=t)


def test_a_reachable_node_is_checked_and_found_up(tmp_path, ready_node):
    store = HeartbeatStore(tmp_path / "hb")
    _beat(store, "site-a", "node-a", ready_node, T0)
    probes = probe.ProbeStore(tmp_path / "probes")
    report = probe.probe_nodes(store, probes, now=T0 + 5)
    assert report == {"site-a": {"node-a": True}}
    assert probes.readings("site-a", "node-a") == [(T0 + 5, True)]


def test_an_unreachable_node_is_recorded_down(tmp_path):
    store = HeartbeatStore(tmp_path / "hb")
    _beat(store, "site-a", "node-a", f"http://127.0.0.1:{_closed_port()}", T0)
    probes = probe.ProbeStore(tmp_path / "probes")
    assert probe.probe_nodes(store, probes, now=T0 + 5, timeout_s=2) == {"site-a": {"node-a": False}}
    assert probes.readings("site-a", "node-a") == [(T0 + 5, False)]


def test_an_older_node_without_readyz_is_checked_on_healthz(tmp_path, healthz_only_node):
    store = HeartbeatStore(tmp_path / "hb")
    _beat(store, "site-a", "node-a", healthz_only_node, T0)
    probes = probe.ProbeStore(tmp_path / "probes")
    assert probe.probe_nodes(store, probes, now=T0 + 5) == {"site-a": {"node-a": True}}


def test_a_push_only_node_advertises_nothing_and_is_not_checked(tmp_path):
    store = HeartbeatStore(tmp_path / "hb")
    _beat(store, "site-a", "node-a", None, T0)
    probes = probe.ProbeStore(tmp_path / "probes")
    assert probe.probe_nodes(store, probes, now=T0 + 5) == {"site-a": {}}
    assert probes.readings("site-a", "node-a") == []


def test_checks_failing_while_the_node_keeps_beating_are_network(tmp_path):
    """The attribution the probe exists for."""
    store = HeartbeatStore(tmp_path / "hb")
    probes = probe.ProbeStore(tmp_path / "probes")
    gap = (T0 + 30 * MIN, T0 + 50 * MIN)
    end = T0 + 120 * MIN
    t = T0
    while t <= end:
        _beat(store, "site-a", "node-a", "http://node-a.example", t)
        probes.record("site-a", "node-a", t + 1, not (gap[0] <= t < gap[1]))
        t += MIN
    report = up.site_report("site-a", store=store, probes=probes, now=end, period_s=end - T0)
    (iv,) = report["intervals"]
    assert (iv["cause"], iv["kind"]) == ("outside", "network"), iv
    assert iv["heartbeat_down"] is False and iv["probe_down"] is True
    assert not any("active probe" in m for m in report["evidence_missing"])


def test_a_reachable_node_never_checked_keeps_the_probe_caveat(tmp_path):
    """Advertising an address is not the same as having been checked."""
    store = HeartbeatStore(tmp_path / "hb")
    _beat(store, "site-a", "node-a", "http://node-a.example", T0)
    report = up.site_report("site-a", store=store, probes=probe.ProbeStore(tmp_path / "p"),
                            now=T0 + 60, period_s=60)
    assert any("active probe" in m and "node-a" in m for m in report["evidence_missing"])


def test_the_sender_advertises_the_nodes_public_address(monkeypatch):
    import json

    from axiom.extensions.builtins.data_platform.daq.beat import HeartbeatSender

    sent = []

    class T:
        def post(self, url, body, headers):
            sent.append(json.loads(body)["batches"][0]["rows"][0])
            return 200, "ok", {}

    monkeypatch.setenv("AXIOM_PUBLIC_URL", "https://node-a.example/")
    HeartbeatSender(face_url="https://f", source="s", bearer=lambda: None, transport=T()).send(
        {"node": "n"}
    )
    assert sent[0]["probe_url"] == "https://node-a.example"
    monkeypatch.delenv("AXIOM_PUBLIC_URL")
    HeartbeatSender(face_url="https://f", source="s", bearer=lambda: None, transport=T()).send(
        {"node": "n"}
    )
    assert "probe_url" not in sent[1]
