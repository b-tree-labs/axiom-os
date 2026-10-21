# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A role node's status says how each function is doing, in one honest screen.

Sharing upstream, updates and the local archive each get a section with a
state, one plain sentence and the fix. And the system view stops calling a
node unhealthy for lacking a database its role never uses (contingency C-42).
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.data_platform.forward.forwarder import (
    Forwarder,
    IntakeTarget,
    LocalOutbox,
)
from axiom.extensions.builtins.data_platform.ingest_sink.edge import EdgeOutbox
from axiom.extensions.builtins.data_platform.sources.edge.puller import FileCursor
from axiom.extensions.builtins.status import function_status
from axiom.extensions.builtins.status.cli import HealthChecker, HealthStatus
from axiom.infra import node_functions as nf


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def role_node(tmp_path, monkeypatch):
    cfg_file = tmp_path / "node.toml"
    monkeypatch.setenv(nf.CONFIG_ENV, str(cfg_file))
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    nf.apply(role="daq", functions=["acquire", "transmit"], path=cfg_file)
    return nf.load(cfg_file)


def _rows(sections, title):
    for sec in sections:
        if sec["title"] == title:
            return {r["label"]: r for r in sec["rows"]}
    raise AssertionError(f"no section {title!r} in {[s['title'] for s in sections]}")


def test_a_db_less_role_is_not_called_unhealthy_for_having_no_database(role_node, monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", f"postgresql+psycopg2://x:y@127.0.0.1:{_closed_port()}/none")
    health = HealthChecker().check_database()
    assert health.status != HealthStatus.UNHEALTHY
    assert "not used" in health.message


def test_a_node_that_uses_the_database_still_reports_it_down(tmp_path, monkeypatch):
    cfg_file = tmp_path / "node.toml"
    monkeypatch.setenv(nf.CONFIG_ENV, str(cfg_file))
    nf.apply(role="platform", functions=["ingest", "data_platform"], path=cfg_file)
    monkeypatch.setenv("AXIOM_DB_URL", f"postgresql+psycopg2://x:y@127.0.0.1:{_closed_port()}/none")
    pytest.importorskip("psycopg2")
    assert HealthChecker().check_database().status == HealthStatus.UNHEALTHY


def test_sharing_upstream_says_where_it_sends_and_how_far_behind(role_node, tmp_path):
    outbox_dir = tmp_path / "outbox"
    box = EdgeOutbox(outbox_dir)
    for i in range(3):
        batch = SimpleNamespace(
            item_id=f"run-{i}",
            schema_ref="s/v1",
            etag="",
            source_path="",
            metadata={},
            rows=[{}] * 10,
        )
        box.record(source="site-a-src", batch=batch, content_hash=f"{i:064x}", rows_landed=10)
    work = tmp_path / "state" / "forward"
    fwd = Forwarder(
        LocalOutbox(outbox_dir, bronze_root_for=lambda s: tmp_path),
        cursor=FileCursor(work / "cursor.json"),
        intake=IntakeTarget(
            f"http://127.0.0.1:{_closed_port()}", token="t", probe_source="site-a-src"
        ),
        status_path=work / "status.json",
    )
    fwd.forward_once()
    rows = _rows(function_status.sections(role_node, outbox_dir=outbox_dir), "Sharing upstream")
    assert rows["sending to"]["state"] == "warn" and "waiting" in rows["sending to"]["value"]
    assert rows["sending to"]["fix"]
    assert rows["behind"]["value"].startswith("3 batches")


def test_updates_report_the_policy_and_the_last_outcome(role_node, tmp_path):
    root = tmp_path / "upd"
    root.mkdir()
    (root / "update-log.jsonl").write_text(
        json.dumps(
            {
                "status": "rolled_back",
                "from_version": "1.17.0",
                "to_version": "1.17.1",
                "detail": "post-switch health check failed",
                "at": "2026-10-08T03:00:00+00:00",
            }
        )
        + "\n"
    )
    rows = _rows(
        function_status.sections(role_node, update_root=root, update_policy="auto-patch"), "Updates"
    )
    assert "auto-patch" in rows["policy"]["value"]
    assert rows["last update"]["state"] == "warn" and "rolled back" in rows["last update"]["value"]


def test_a_node_with_none_of_these_functions_reports_nothing_extra(tmp_path, monkeypatch):
    monkeypatch.setenv(nf.CONFIG_ENV, str(tmp_path / "absent.toml"))
    assert function_status.sections(nf.load(tmp_path / "absent.toml")) is None


def test_the_role_screen_prints_on_a_console_that_cannot_encode_its_marks(tmp_path):
    """A Windows node whose output is redirected writes cp1252; the marks must not crash it."""
    import os
    import subprocess
    import sys

    cfg_file = tmp_path / "node.toml"
    nf.apply(role="daq", functions=["acquire", "transmit"], path=cfg_file)
    env = {**os.environ, nf.CONFIG_ENV: str(cfg_file), "AXI_STATE_DIR": str(tmp_path / "s"),
           "PYTHONIOENCODING": "cp1252"}
    code = (
        "from axiom.extensions.builtins.status import role_status as r\n"
        "sections = [{'title': 'X', 'rows': [{'label': 'a', 'value': 'v', 'state': 'ok', 'fix': ''},"
        " {'label': 'b', 'value': 'v', 'state': 'fail', 'fix': 'do it'}]}]\n"
        "from axiom.infra import node_functions as nf\n"
        "r.emit(nf.load(), sections)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    out = proc.stdout.decode("cp1252")
    assert "a  v" in out and "fix: do it" in out


class _Intake(BaseHTTPRequestHandler):
    """A real intake over HTTP: healthy, accepts every row it is sent."""

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or b"{}"))
        time.sleep(0.2)  # a real intake takes time: one backlog request fits a pass, not six
        rows = sum(len(b.get("rows") or []) for b in body.get("batches") or [])
        out = json.dumps({"rows_in": rows, "rows_landed": rows, "errored": 0}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def _outbox_with_old_batches(tmp_path, n: int, source: str = "site-a-src") -> EdgeOutbox:
    box = EdgeOutbox(tmp_path / "outbox")
    for i in range(n):
        rows = [{"ts": f"2026-01-0{i + 1}T00:00:00Z", "v": i}] * 3
        blob = json.dumps(rows).encode()
        digest = hashlib.sha256(blob).hexdigest()
        content = tmp_path / source / "_content" / digest[:2] / digest
        content.parent.mkdir(parents=True, exist_ok=True)
        content.write_bytes(blob)
        batch = SimpleNamespace(item_id=f"run-{i}", schema_ref="s/v1", etag="", source_path="", metadata={}, rows=rows)
        box.record(source=source, batch=batch, content_hash=digest, rows_landed=3)
    return box


def test_sharing_upstream_says_how_far_catch_up_has_to_go(role_node, tmp_path):
    """After an outage the forwarder sends live first and drains the backlog paced
    (W16); status shows how much is left and the ETA it measured, not a guess."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Intake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _outbox_with_old_batches(tmp_path, 6)
        time.sleep(0.6)  # older than the live window: the next pass is a catch-up
        work = tmp_path / "state" / "forward"
        fwd = Forwarder(
            LocalOutbox(tmp_path / "outbox", bronze_root_for=lambda s: tmp_path),
            cursor=FileCursor(work / "cursor.json"),
            intake=IntakeTarget(f"http://127.0.0.1:{server.server_port}", token="t", probe_source="site-a-src"),
            status_path=work / "status.json",
            up_after=1,
            live_window_s=0.5,
            backlog_records_per_request=1,
            backlog_slice_s=0.05,
        )
        fwd.forward_once()
        rows = _rows(function_status.sections(role_node, outbox_dir=tmp_path / "outbox"), "Sharing upstream")
        assert rows["catching up"]["state"] == "info"
        assert "of 6" in rows["catching up"]["value"] and "live first" in rows["catching up"]["value"]
        time.sleep(2.1)  # the rate needs a two-second window before an ETA is honest
        fwd.forward_once()
        rows = _rows(function_status.sections(role_node, outbox_dir=tmp_path / "outbox"), "Sharing upstream")
        assert "ETA" in rows["catching up"]["value"]
    finally:
        server.shutdown()


def test_a_catch_up_held_by_the_intake_says_so_and_for_how_long(role_node, tmp_path):
    work = tmp_path / "state" / "forward"
    work.mkdir(parents=True)
    until = datetime.now(UTC).timestamp() + 90
    (work / "status.json").write_text(json.dumps({
        "current": "intake", "since": datetime.now(UTC).isoformat(), "reason": "intake healthy",
        "after": 2, "switches": [],
        "catch_up": {"active": True, "split": 40, "backlog_total": 38, "drained": 2, "remaining": 36,
                     "records_per_s": 1.5, "eta_s": 24.0, "refused_429": 3, "refused_507": 0,
                     "backlog_wait_until": until, "oldest_pending_at": "2026-09-30T10:00:00+00:00"},
    }), encoding="utf-8")
    rows = _rows(function_status.sections(role_node), "Sharing upstream")
    assert rows["catching up"]["state"] == "warn"
    assert "paused" in rows["catching up"]["value"] and "429" in rows["catching up"]["value"]
    assert "36 of 38" in rows["catching up"]["value"] and "ETA" in rows["catching up"]["value"]
