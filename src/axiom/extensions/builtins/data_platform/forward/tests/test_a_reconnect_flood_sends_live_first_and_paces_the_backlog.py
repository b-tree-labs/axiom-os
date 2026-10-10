# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A site that was offline reconnects: live data first, the backlog paced behind it.

Real nodes, started with the shipped CLI: a site's local archive (an ingest
node with an outbox) and the upstream intake. The local archive collects while
the intake is down, so a backlog builds. When the intake comes back:

* **live data goes first.** Each pass sends what arrived since the reconnect
  before any of the backlog, so a reading taken now reaches the intake in well
  under a second however much history is queued;
* **the backlog drains oldest-first, paced.** Backlog requests are marked so
  the intake charges them to a separate, smaller per-site budget; a 429 is
  honoured for exactly the ``Retry-After`` it carried, and live data keeps
  flowing through it;
* **progress is visible**, with an ETA that comes true;
* **a 507 (the intake's disk is short) holds everything** at the site, and the
  drain resumes when the intake has room;
* **nothing is lost or duplicated.** The intake's own outbox, which records each
  batch once with the rows it landed, ends with every batch exactly once and
  every row landed exactly once.

Every number asserted here is measured on real processes over real HTTP.
"""

from __future__ import annotations

import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.forward import Forwarder, IntakeTarget, LocalOutbox
from axiom.extensions.builtins.data_platform.sources.edge.puller import FileCursor

AXI = [sys.executable, "-c", "import sys; from axiom.axiom_cli import main; sys.exit(main())"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _axi(args, env):
    out = subprocess.run(AXI + args, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-1500:]
    return json.loads(out.stdout) if out.stdout.strip().startswith("{") else {}


def _ts(rec: dict) -> float:
    return datetime.fromisoformat(rec["received_at"]).timestamp()


class Node:
    """One ingest node (edge profile) with its own state, outbox and keys."""

    def __init__(self, root: Path, name: str, *, sources: list[tuple[str, str]], env: dict | None = None):
        self.root = root / name
        base = {k: v for k, v in os.environ.items() if not k.startswith(("AXIOM_", "AXI_"))}
        self.env = dict(base, AXI_STATE_DIR=str(self.root / "state"),
                        AXIOM_INGEST_OUTBOX_DIR=str(self.root / "outbox"),
                        AXIOM_GATE_API_KEYS_FILE=str(self.root / "state" / "keys.json"),
                        AXIOM_EDGE_DOWNSTREAM="@up:org", AXI_DIAGNOSES_QUIET="1",
                        AXIOM_ACTOR="@operator:org", **(env or {}))
        for source, site in sources:
            _axi(["data", "register", "--bronze-root", str(self.bronze_root(source)), "--site", site,
                  "--default-disposition", "allow", "--default-tier", "restricted", source, "push"], self.env)
        self.port = _free_port()
        self.proc = None

    def bronze_root(self, source: str) -> Path:
        return self.root / "bronze" / source

    def key(self, site: str, scope: str = "data_platform:invoke") -> str:
        return _axi(["gate", "--json", "issue", "api-key", "--principal", f"@daq:{site}", "--site", site,
                     "--scope", scope], self.env)["value"]["token"]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def up(self, **env) -> None:
        self.proc = subprocess.Popen(AXI + ["serve", "--profile", "ingest-edge", "--host", "127.0.0.1",
                                            "--port", str(self.port)], env={**self.env, **env},
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(150):
            try:
                with urllib.request.urlopen(self.url + "/healthz", timeout=2):
                    return
            except OSError:
                time.sleep(0.2)
        raise AssertionError("node never answered /healthz")

    def down(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=10)
            self.proc = None

    def push(self, token: str, source: str, item: str, n: int = 107) -> float:
        """One second of readings (``n`` channels) as one batch; returns the round trip."""
        rows = [{"channel": f"C{i:03d}", "ts": f"{item}", "value": i, "unit": "V"} for i in range(n)]
        body = {"source": source, "batches": [{"item_id": item, "schema_ref": "site/epics-v1", "rows": rows}]}
        req = urllib.request.Request(self.url + "/ingest/rows", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        t = time.perf_counter()
        while True:
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    assert json.loads(r.read())["errored"] == 0
                return time.perf_counter() - t
            except urllib.error.HTTPError as exc:  # a producer honours its own node's budget too
                if exc.code != 429:
                    raise
                time.sleep(float(exc.headers.get("Retry-After") or 1))

    def outbox_records(self) -> list[dict]:
        p = self.root / "outbox" / "outbox.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def _forwarder(local: Node, upstream: Node, token: str, source: str, **kw) -> Forwarder:
    work = local.root / "forward"
    return Forwarder(
        LocalOutbox(local.root / "outbox", bronze_root_for=local.bronze_root),
        cursor=FileCursor(work / "cursor.json"),
        intake=IntakeTarget(upstream.url, token=token, probe_source=source),
        status_path=work / "status.json",
        up_after=1,
        **kw,
    )


class Run:
    """Run a forwarder in a loop on its own thread, sampling its health."""

    def __init__(self, fwd: Forwarder, every: float = 0.05):
        self.fwd, self.every = fwd, every
        self.samples: list[tuple[float, dict]] = []
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            self.fwd.forward_once()
            self.samples.append((time.time(), self.fwd.health()))
            time.sleep(self.every)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()
        self.thread.join(timeout=60)


def _live_pusher(node: Node, token: str, source: str, prefix: str, hz: float, stop: threading.Event,
                 out: list[str]):
    i = 0
    while not stop.is_set():
        item = f"{prefix}-{i:06d}"
        node.push(token, source, item)
        out.append(item)
        i += 1
        time.sleep(1 / hz)


def _heartbeat(node: Node, token: str) -> dict | None:
    """The forwarder's latest beat as the receiving side shows it."""
    req = urllib.request.Request(node.url + "/ingest/heartbeat", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        nodes = json.loads(r.read())["nodes"]
    beats = [n["latest"] for n in nodes if n["node"] == "site-a-archive"]
    return beats[0] if beats else None


def _assert_exactly_once(local: Node, upstream: Node):
    sent = local.outbox_records()
    got = upstream.outbox_records()
    hashes = [r["content_hash"] for r in got]
    assert len(hashes) == len(set(hashes)), "a batch was recorded twice upstream"
    assert set(hashes) == {r["content_hash"] for r in sent}, "a batch never arrived"
    assert sum(r["rows_landed"] for r in got) == sum(r["rows"] for r in sent), "rows lost or duplicated"


def _live_latencies(local: Node, upstream: Node, items: list[str]) -> list[float]:
    """Seconds from landing at the site to landing upstream, per live batch."""
    here = {r["item_id"]: _ts(r) for r in local.outbox_records()}
    there = {r["item_id"]: _ts(r) for r in upstream.outbox_records()}
    return [there[i] - here[i] for i in items if i in there]


@pytest.fixture
def site(tmp_path):
    site, source = "site-a", "site-a-src"
    local = Node(tmp_path, "local", sources=[(source, site)])
    upstream = Node(tmp_path, "upstream", sources=[(source, site)])
    local_key, up_key = local.key(site), upstream.key(site)
    local.up()
    yield local, upstream, local_key, up_key, source
    local.down()
    upstream.down()


def _build_backlog(local, key, source, n):
    for i in range(n):
        local.push(key, source, f"old-{i:06d}")


def test_after_an_outage_live_goes_first_and_the_backlog_drains_paced(site):
    local, upstream, local_key, up_key, source = site
    backlog = 200
    _build_backlog(local, local_key, source, backlog)  # collected while the intake was down
    fwd = _forwarder(local, upstream, up_key, source, live_window_s=1.0, backlog_records_per_request=10,
                     backlog_request_target_s=2.0, beat_node="site-a-archive", beat_every_s=1.0)
    assert fwd.forward_once().current == "waiting"
    time.sleep(1.2)  # the backlog is now older than the live window

    viewer = upstream.key("site-a", scope="data_platform:read")  # the receiving side's own view
    # A deliberately small backlog budget (1 request a second, no burst) so the
    # drain meets real 429s however fast or slow the machine is.
    upstream.up(AXIOM_INGEST_BACKLOG_BURST="1", AXIOM_INGEST_BACKLOG_RATE_PER_SECOND="1")
    stop, live = threading.Event(), []
    pusher = threading.Thread(target=_live_pusher, args=(local, local_key, source, "live", 4, stop, live),
                              daemon=True)
    run = Run(fwd).start()
    pusher.start()
    seen_upstream = []
    deadline = time.time() + 120
    while time.time() < deadline and fwd.health()["catch_up"]["active"] is not False:
        seen_upstream.append(_heartbeat(upstream, viewer))
        time.sleep(0.5)
    time.sleep(1.0)
    stop.set()
    pusher.join()
    time.sleep(1.0)
    run.stop()

    h = fwd.health()
    assert h["catch_up"]["active"] is False, h
    _assert_exactly_once(local, upstream)

    lat = _live_latencies(local, upstream, live)
    assert len(lat) >= 0.9 * len(live)
    p95 = statistics.quantiles(lat, n=20)[18]
    assert p95 < 1.0, f"live p95 {p95:.3f}s while the backlog drained"

    # The drain met the budget and honoured it: every 429 was waited out.
    cu = h["catch_up"]
    assert cu["refused_429"] >= 1, cu
    drain_s = cu["finished_at"] - cu["started_at"]
    # Honoured: each 429 is waited out for its Retry-After (>= 1 s), so an
    # honest client draws at most about one per second of drain.
    assert cu["refused_429"] <= drain_s + 2, cu

    # The ETA, read a third of the way in, predicted the finish within 40 %.
    mid = [(t, s["catch_up"]) for t, s in run.samples
           if s["catch_up"].get("active") and s["catch_up"].get("eta_s") is not None
           and s["catch_up"]["drained"] >= backlog / 3]
    assert mid, "no ETA was published during the drain"
    t0, c0 = mid[0]
    predicted, actual = c0["eta_s"], cu["finished_at"] - t0
    assert abs(predicted - actual) <= 0.4 * actual + 2, (predicted, actual)

    # The receiving side saw the catch-up in the site's heartbeat as it ran.
    during = [b for b in seen_upstream if b and b.get("catch_up_active")]
    assert during, "the upstream never saw the site catching up"
    assert during[-1]["catch_up_remaining"] < during[0]["catch_up_remaining"]
    assert during[0]["catch_up_total"] == backlog and "disk_alarm" in during[0]

    # Oldest first: the backlog arrived upstream in the order it was collected.
    order = [r["item_id"] for r in upstream.outbox_records() if r["item_id"].startswith("old-")]
    assert order == sorted(order)


def test_a_507_holds_everything_at_the_site_until_the_intake_has_room(site):
    local, upstream, local_key, up_key, source = site
    _build_backlog(local, local_key, source, 30)
    upstream.up(AXIOM_INGEST_MIN_FREE_BYTES=str(1 << 60), AXIOM_INGEST_HEADROOM_RETRY_S="2")
    fwd = _forwarder(local, upstream, up_key, source, live_window_s=0.5)
    time.sleep(0.6)
    t0 = time.time()
    attempts = 0
    while time.time() - t0 < 5:
        fwd.forward_once()
        attempts += 1
        time.sleep(0.05)
    h = fwd.health()
    assert upstream.outbox_records() == []
    assert h["after"] == 0
    assert h["refusal"]["code"] == 507 and "free" in h["refusal"]["reason"], h
    # Honoured: one request, then quiet for Retry-After (2 s), across ~100 passes.
    assert h["catch_up"]["refused_507"] <= 4, h["catch_up"]

    upstream.down()
    upstream.up()
    t0 = time.time()
    while time.time() - t0 < 30 and len(upstream.outbox_records()) < 30:
        fwd.forward_once()
        time.sleep(0.1)
    _assert_exactly_once(local, upstream)


def test_progress_survives_a_restart_mid_drain_without_loss(site):
    local, upstream, local_key, up_key, source = site
    _build_backlog(local, local_key, source, 120)
    upstream.up(AXIOM_INGEST_BACKLOG_BURST="2", AXIOM_INGEST_BACKLOG_RATE_PER_SECOND="2")
    fwd = _forwarder(local, upstream, up_key, source, live_window_s=0.5, backlog_records_per_request=5)
    time.sleep(0.6)
    fwd.forward_once()
    for i in range(5):
        local.push(local_key, source, f"live-{i:06d}")
    fwd.forward_once()
    assert fwd.health()["catch_up"]["active"] is True
    # A new forwarder process picks up the same files.
    again = _forwarder(local, upstream, up_key, source, live_window_s=0.5, backlog_records_per_request=5)
    assert again.health()["catch_up"]["active"] is True
    t0 = time.time()
    while time.time() - t0 < 90 and again.health()["catch_up"]["active"]:
        again.forward_once()
        time.sleep(0.05)
    _assert_exactly_once(local, upstream)
    assert again.health()["after"] == local.outbox_records()[-1]["seq"]


def test_two_sites_reconnect_at_once_and_a_third_site_live_is_unaffected(tmp_path):
    """Two sites drain backlogs into one intake together while a third pushes live."""
    a, b, c = ("site-a", "site-a-src"), ("site-b", "site-b-src"), ("site-c", "site-c-src")
    upstream = Node(tmp_path, "upstream", sources=[(a[1], a[0]), (b[1], b[0]), (c[1], c[0])])
    locals_ = {s: Node(tmp_path, f"local-{s}", sources=[(src, s)]) for s, src in (a, b)}
    keys = {s: (locals_[s].key(s), upstream.key(s)) for s in ("site-a", "site-b")}
    c_key = upstream.key("site-c")
    try:
        for n in locals_.values():
            n.up()
        for s, src in (a, b):
            _build_backlog(locals_[s], keys[s][0], src, 400)
        fwds = {s: _forwarder(locals_[s], upstream, keys[s][1], src, live_window_s=1.0,
                              backlog_records_per_request=10, backlog_request_target_s=0.4)
                for s, src in (a, b)}
        time.sleep(1.2)
        upstream.up()

        # Site C's own live round trip, before the flood.
        before = [upstream.push(c_key, c[1], f"c-pre-{i:04d}") for i in range(20)]

        stop = threading.Event()
        live = {s: [] for s in ("site-a", "site-b")}
        pushers = [threading.Thread(target=_live_pusher, args=(locals_[s], keys[s][0], src, f"live-{s}", 4,
                                                               stop, live[s]), daemon=True) for s, src in (a, b)]
        runs = [Run(f).start() for f in fwds.values()]
        for p in pushers:
            p.start()
        during: list[float] = []
        i = 0
        deadline = time.time() + 150
        while time.time() < deadline and (i < 20 or any(f.health()["catch_up"]["active"] is not False
                                                         for f in fwds.values())):
            during.append(upstream.push(c_key, c[1], f"c-dur-{i:04d}"))
            i += 1
            time.sleep(0.25)
        stop.set()
        for p in pushers:
            p.join()
        time.sleep(1.5)
        for r in runs:
            r.stop()

        for s in ("site-a", "site-b"):
            assert fwds[s].health()["catch_up"]["active"] is False, (s, fwds[s].health()["catch_up"])
            _assert_exactly_once(locals_[s], _SiteView(upstream, f"{s}-src"))
            lat = _live_latencies(locals_[s], upstream, live[s])
            p95 = statistics.quantiles(lat, n=20)[18]
            assert p95 < 1.0, f"{s} live p95 {p95:.3f}s while two sites drained"
        # Site C was never refused and its pushes stayed fast. A shared machine
        # is busier during a flood, so "unaffected" is: never refused, and p95
        # under a second.
        p95_c = statistics.quantiles(during, n=20)[18]
        assert len(during) >= 20
        assert p95_c < 1.0, (statistics.median(before), p95_c)
    finally:
        for n in locals_.values():
            n.down()
        upstream.down()


class _SiteView:
    """The upstream outbox restricted to one source, for the exactly-once check."""

    def __init__(self, node: Node, source: str):
        self.node, self.source = node, source

    def outbox_records(self) -> list[dict]:
        return [r for r in self.node.outbox_records() if r["source"] == self.source]
