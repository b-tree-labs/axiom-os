# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A site's local archive forwards what it landed upstream, choosing the way itself.

Two real nodes, started with the shipped CLI: the site's local archive (an
ingest node with an outbox) and the upstream intake. The forwarder reads the
local outbox from disk and delivers each batch to the intake when it is
healthy, to a drop folder (rclone) when it is not, and waits when neither is
available, advancing only on a confirmed delivery. The upstream lands each
batch once however it travelled.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.forward import (
    BoxDropTarget,
    Forwarder,
    IntakeTarget,
    LocalOutbox,
)
from axiom.extensions.builtins.data_platform.sources.edge.puller import FileCursor

AXI = [sys.executable, "-c", "import sys; from axiom.axiom_cli import main; sys.exit(main())"]
pytestmark = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone is needed for the drop target")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _axi(args, env):
    out = subprocess.run(AXI + args, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-1500:]
    return json.loads(out.stdout) if out.stdout.strip().startswith("{") else {}


class Node:
    """One ingest node (edge profile) with its own state, outbox and keys."""

    def __init__(self, root: Path, name: str, *, sources: list[tuple[str, str]], downstream: str = "@up:org"):
        self.root = root / name
        base = {k: v for k, v in os.environ.items() if not k.startswith(("AXIOM_", "AXI_"))}
        self.env = dict(base, AXI_STATE_DIR=str(self.root / "state"),
                        AXIOM_INGEST_OUTBOX_DIR=str(self.root / "outbox"),
                        AXIOM_GATE_API_KEYS_FILE=str(self.root / "state" / "keys.json"),
                        AXIOM_EDGE_DOWNSTREAM=downstream, AXI_DIAGNOSES_QUIET="1",
                        AXIOM_ACTOR="@operator:org")
        for source, site in sources:
            _axi(["data", "register", "--bronze-root", str(self.bronze_root(source)), "--site", site,
                  "--default-disposition", "allow", "--default-tier", "restricted", source, "push"], self.env)
        self.port = _free_port()
        self.proc = None

    def bronze_root(self, source: str) -> Path:
        return self.root / "bronze" / source

    def key(self, site: str) -> str:
        return _axi(["gate", "--json", "issue", "api-key", "--principal", f"@daq:{site}", "--site", site,
                     "--scope", "data_platform:invoke"], self.env)["value"]["token"]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def up(self) -> None:
        self.proc = subprocess.Popen(AXI + ["serve", "--profile", "ingest-edge", "--host", "127.0.0.1",
                                            "--port", str(self.port)], env=self.env,
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

    def push(self, token: str, source: str, item: str, n: int = 20) -> int:
        minute = sum(item.encode()) % 60
        rows = [{"channel": "TC1", "ts": f"2026-10-08T01:{minute:02d}:{i:02d}Z", "value": 20 + i, "unit": "degC"}
                for i in range(n)]
        body = {"source": source, "batches": [{"item_id": item, "schema_ref": "site/epics-v1", "rows": rows}]}
        req = urllib.request.Request(self.url + "/ingest/rows", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["rows_landed"]

    def outbox_records(self) -> list[dict]:
        p = self.root / "outbox" / "outbox.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


@pytest.fixture
def world(tmp_path):
    site, source = "site-a", "site-a-site"
    local = Node(tmp_path, "local", sources=[(source, site)])
    upstream = Node(tmp_path, "upstream", sources=[(source, site)])
    local_key, up_key = local.key(site), upstream.key(site)
    local.up()
    upstream.up()
    box = tmp_path / "box"
    box.mkdir()
    yield local, upstream, local_key, up_key, box, source
    local.down()
    upstream.down()


def _forwarder(local, upstream, up_key, box, tmp, source, **kw):
    return Forwarder(
        LocalOutbox(local.root / "outbox", bronze_root_for=local.bronze_root),
        cursor=FileCursor(tmp / "fwd-cursor.json"),
        intake=IntakeTarget(upstream.url, token=up_key, probe_source=source),
        box=BoxDropTarget(str(box)),
        status_path=tmp / "fwd-status.json",
        up_after=kw.pop("up_after", 1), down_after=kw.pop("down_after", 1), **kw)


def _collect_box(upstream, up_key, box) -> int:
    """The upstream's daily collection: land each dropped body through its own intake."""
    landed = 0
    for f in sorted(box.rglob("*.rows.json")):
        req = urllib.request.Request(upstream.url + "/ingest/rows", data=f.read_bytes(), method="POST",
                                     headers={"Authorization": f"Bearer {up_key}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            landed += json.loads(r.read())["rows_landed"]
    return landed


def test_a_healthy_intake_receives_every_batch_once(world, tmp_path):
    local, upstream, local_key, up_key, box, source = world
    for i in range(3):
        assert local.push(local_key, source, f"run-{i}") == 20
    f = _forwarder(local, upstream, up_key, box, tmp_path, source)
    r = f.forward_once()
    assert (r.current, r.sent_intake) == ("intake", 3)
    assert len(upstream.outbox_records()) == 3
    assert f.forward_once().sent_intake == 0          # nothing new: nothing sent
    assert not list(box.rglob("*.rows.json"))


def test_with_the_intake_down_batches_go_to_the_drop_and_land_once_after(world, tmp_path):
    local, upstream, local_key, up_key, box, source = world
    upstream.down()
    for i in range(2):
        local.push(local_key, source, f"run-{i}")
    f = _forwarder(local, upstream, up_key, box, tmp_path, source)
    r = f.forward_once()
    assert (r.current, r.sent_box) == ("box", 2)
    assert "unreachable" in r.reason or "intake" in r.reason
    upstream.up()
    assert _collect_box(upstream, up_key, box) == 40
    # The intake comes back: the same batches arriving again land nothing new.
    f.cursor.set(0)
    r2 = f.forward_once()
    assert r2.current == "intake" and r2.sent_intake == 2
    assert len(upstream.outbox_records()) == 2


def test_hysteresis_needs_consecutive_healthy_checks_before_switching_up(world, tmp_path):
    local, upstream, local_key, up_key, box, source = world
    upstream.down()
    f = _forwarder(local, upstream, up_key, box, tmp_path, source, up_after=3, down_after=2)
    assert f.forward_once().current == "box"
    upstream.up()
    assert [f.forward_once().current for _ in range(3)] == ["box", "box", "intake"]
    switches = json.loads((tmp_path / "fwd-status.json").read_text())["switches"]
    assert [s["to"] for s in switches] == ["box", "intake"]


def test_a_refused_key_stays_on_the_drop_and_says_why(world, tmp_path):
    local, upstream, local_key, up_key, box, source = world
    local.push(local_key, source, "run-0")
    f = _forwarder(local, upstream, "not-a-valid-key", box, tmp_path, source)
    r = f.forward_once()
    assert r.current == "box" and r.sent_box == 1
    assert "refused this site's key" in r.reason


def test_with_nowhere_to_send_it_waits_and_loses_nothing(world, tmp_path):
    local, upstream, local_key, up_key, box, source = world
    upstream.down()
    local.push(local_key, source, "run-0")
    f = Forwarder(LocalOutbox(local.root / "outbox", bronze_root_for=local.bronze_root),
                  cursor=FileCursor(tmp_path / "c.json"),
                  intake=IntakeTarget(upstream.url, token=up_key, probe_source=source),
                  box=None, status_path=tmp_path / "s.json", up_after=1, down_after=1)
    r = f.forward_once()
    assert r.current == "waiting" and r.after == 0
    upstream.up()
    assert f.forward_once().sent_intake == 1


def test_a_policy_can_exclude_or_hold_a_batch(world, tmp_path):
    from datetime import UTC, datetime, timedelta

    local, upstream, local_key, up_key, box, source = world
    local.push(local_key, source, "keep")
    local.push(local_key, source, "private")

    class Policy:
        def allows(self, record, rows):
            return [] if record["item_id"] == "private" else rows

        def hold_until(self, record):
            return None

    f = _forwarder(local, upstream, up_key, box, tmp_path, source, policy=Policy())
    r = f.forward_once()
    assert (r.sent_intake, r.excluded_by_policy) == (1, 1)

    class Hold(Policy):
        def hold_until(self, record):
            return datetime.now(UTC) + timedelta(hours=1)

    local.push(local_key, source, "later")
    g = _forwarder(local, upstream, up_key, box, tmp_path, source, policy=Hold())
    assert g.forward_once().held_by_policy == 1


def test_an_upstream_that_cannot_store_a_batch_does_not_move_the_cursor(world, tmp_path):
    """The intake answers 200 when a batch's write fails, and reports it as
    ``errored``. Treating that 200 as delivered would skip data that never
    landed (found by filling an upstream's disk)."""
    import stat

    local, upstream, local_key, up_key, box, source = world
    local.push(local_key, source, "run-0")
    root = upstream.bronze_root(source)
    root.mkdir(parents=True, exist_ok=True)
    mode = root.stat().st_mode
    root.chmod(stat.S_IRUSR | stat.S_IXUSR)  # the upstream can no longer write this source's bronze
    try:
        f = _forwarder(local, upstream, up_key, box, tmp_path, source, up_after=1, down_after=5)
        r = f.forward_once()
        assert r.sent_intake == 0 and r.after == 0, r
        # A face answers 507 when nothing in a push landed; an older face
        # answers 200 with ``errored``. Either way the cursor must hold.
        assert "507" in r.reason or "could not store" in r.reason, r.reason
    finally:
        root.chmod(mode)
    # The upstream recovers: the same batch lands, once the forwarder has
    # waited out the refusal (a 507 is honoured, not retried at once).
    import time

    sent, deadline = 0, time.time() + 30
    while not sent and time.time() < deadline:
        sent = f.forward_once().sent_intake
        time.sleep(0.2)
    assert sent == 1 and len(upstream.outbox_records()) == 1
