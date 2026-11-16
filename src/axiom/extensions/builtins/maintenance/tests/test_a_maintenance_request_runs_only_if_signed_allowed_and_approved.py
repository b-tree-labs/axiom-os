# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A remote maintenance request runs on a site's node only when it should.

The node, never the relay that carried it, decides: a request must be signed by
an operator key this node trusts, addressed to this site, inside its validity
window, never seen before, an action on the allowlist with valid parameters,
wired to a command on this node, and, if it changes anything, approved by a
person at the site. Every decision is written to the node's audit log.
These tests run real commands (``sys.executable -c ...``), not stand-ins.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.infra import maintenance as m
from axiom.vega.identity.keypair import generate_keypair

SITE = "site-a"


@pytest.fixture
def operator():
    return generate_keypair()


@pytest.fixture
def node(tmp_path: Path, operator) -> m.MaintenanceNode:
    marker = tmp_path / "ran.txt"
    wiring = {
        "trusted_keys": {"op-1": {"public_key": m.b64(operator.public_bytes), "operator": "@ops:ut"}},
        "commands": {
            "diagnose": [sys.executable, "-c", "print('diagnose ok')"],
            "restart_service": {
                "collector": [sys.executable, "-c", f"open({str(marker)!r},'a').write('restarted\\n')"],
            },
            "apply_update": [sys.executable, "-c", "import sys; print('update to', sys.argv[1])", "{version}"],
        },
    }
    return m.MaintenanceNode(site=SITE, level="requests", wiring=wiring, state_dir=tmp_path / "state")


def _req(operator, *, action="diagnose", params=None, site=SITE, key_id="op-1", issued=None, ttl_s=600, rid=None):
    issued = issued or datetime.now(UTC)
    return m.sign_request(
        operator,
        key_id=key_id,
        operator_principal="@ops:ut",
        site=site,
        action=action,
        params=params or {},
        issued_at=issued,
        expires_at=issued + timedelta(seconds=ttl_s),
        request_id=rid,
    )


def _audit(node: m.MaintenanceNode) -> list[dict]:
    p = node.audit_path
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def test_a_signed_read_only_request_runs_and_is_audited(node, operator):
    res = node.receive(_req(operator))
    assert res["status"] == "done" and res["exit_code"] == 0 and "diagnose ok" in res["output_tail"]
    events = [e["event"] for e in _audit(node)]
    assert events == ["received", "done"]


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda e: e.__setitem__("signature", m.b64(b"x" * 64)), "bad_signature"),
        (lambda e: e["request"].__setitem__("action", "restart_service"), "bad_signature"),
    ],
)
def test_a_forged_or_altered_request_is_refused(node, operator, mutate, reason):
    env = _req(operator)
    mutate(env)
    res = node.receive(env)
    assert res["status"] == "refused" and res["reason"] == reason


def test_a_key_this_node_does_not_trust_is_refused(node):
    stranger = generate_keypair()
    res = node.receive(_req(stranger, key_id="op-1"))
    assert res["status"] == "refused" and res["reason"] == "bad_signature"
    res = node.receive(_req(stranger, key_id="nobody"))
    assert res["reason"] == "unknown_key"


def test_a_replayed_request_is_refused(node, operator):
    env = _req(operator)
    assert node.receive(env)["status"] == "done"
    again = node.receive(json.loads(json.dumps(env)))
    assert again["status"] == "refused" and again["reason"] == "replayed"


def test_a_replay_is_refused_after_the_node_restarts(node, operator, tmp_path):
    env = _req(operator)
    node.receive(env)
    fresh = m.MaintenanceNode(site=SITE, level="requests", wiring=node.wiring, state_dir=node.state_dir)
    assert fresh.receive(env)["reason"] == "replayed"


def test_an_expired_or_not_yet_valid_request_is_refused(node, operator):
    old = _req(operator, issued=datetime.now(UTC) - timedelta(hours=2), ttl_s=600)
    assert node.receive(old)["reason"] == "expired"
    future = _req(operator, issued=datetime.now(UTC) + timedelta(hours=1))
    assert node.receive(future)["reason"] == "not_yet_valid"
    too_long = _req(operator, ttl_s=3 * 24 * 3600)
    assert node.receive(too_long)["reason"] == "validity_too_long"


def test_a_request_for_another_site_is_refused(node, operator):
    assert node.receive(_req(operator, site="site-b"))["reason"] == "wrong_site"


def test_an_action_off_the_allowlist_or_with_bad_params_is_refused(node, operator):
    assert node.receive(_req(operator, action="run_shell", params={"cmd": "rm -rf /"}))["reason"] == "unknown_action"
    bad = _req(operator, action="apply_update", params={"version": "1.0; rm -rf /"})
    assert node.receive(bad)["reason"] == "bad_params"
    extra = _req(operator, action="diagnose", params={"also": "this"})
    assert node.receive(extra)["reason"] == "bad_params"


def test_an_allowed_action_this_node_has_not_wired_is_refused(node, operator):
    res = node.receive(_req(operator, action="rerun_backfill", params={
        "source": "src-a", "start_at": "2026-10-01T00:00:00Z", "end_at": "2026-10-02T00:00:00Z"}))
    assert res["reason"] == "not_wired"
    res = node.receive(_req(operator, action="restart_service", params={"service": "database"}))
    assert res["reason"] == "not_wired"


def test_a_site_that_turned_maintenance_off_refuses_everything(tmp_path, operator, node):
    off = m.MaintenanceNode(site=SITE, level="off", wiring=node.wiring, state_dir=tmp_path / "off")
    assert off.receive(_req(operator))["reason"] == "maintenance_off"


def test_a_change_waits_for_a_person_at_the_site_then_runs(node, operator, tmp_path):
    env = _req(operator, action="restart_service", params={"service": "collector"})
    res = node.receive(env)
    assert res["status"] == "pending"
    assert not (tmp_path / "ran.txt").exists()
    rid = env["request"]["id"]
    assert [p["id"] for p in node.pending()] == [rid]
    done = node.approve(rid, by="partner-admin")
    assert done["status"] == "done" and (tmp_path / "ran.txt").read_text() == "restarted\n"
    events = [e["event"] for e in _audit(node)]
    assert events == ["received", "pending", "approved", "done"]
    assert node.pending() == []


def test_a_denied_change_never_runs(node, operator, tmp_path):
    env = _req(operator, action="restart_service", params={"service": "collector"})
    node.receive(env)
    res = node.deny(env["request"]["id"], by="partner-admin")
    assert res["status"] == "denied" and not (tmp_path / "ran.txt").exists()
    with pytest.raises(KeyError):
        node.approve(env["request"]["id"], by="partner-admin")


def test_an_unapproved_change_expires_without_running(node, operator, tmp_path):
    env = _req(operator, action="restart_service", params={"service": "collector"}, ttl_s=1)
    node.receive(env)
    swept = node.sweep(now=datetime.now(UTC) + m.SKEW + timedelta(seconds=5))
    assert [r["status"] for r in swept] == ["expired"]
    with pytest.raises(KeyError):
        node.approve(env["request"]["id"], by="partner-admin")
    assert not (tmp_path / "ran.txt").exists()


def test_parameters_reach_the_command_as_one_argument_never_a_shell(node, operator):
    res = node.receive(_req(operator, action="apply_update", params={"version": "1.17.1"}))
    assert res["status"] == "pending"
    out = node.approve(res["id"], by="partner-admin")
    assert "update to 1.17.1" in out["output_tail"]


def test_the_site_may_require_approval_for_everything(tmp_path, operator, node):
    strict = m.MaintenanceNode(site=SITE, level="requests", wiring={**node.wiring, "approve": "all"},
                               state_dir=tmp_path / "strict")
    assert strict.receive(_req(operator))["status"] == "pending"


def test_the_heartbeat_section_says_what_is_waiting_and_what_ran(node, operator):
    node.receive(_req(operator))
    node.receive(_req(operator, action="restart_service", params={"service": "collector"}))
    hb = node.heartbeat_section()
    assert hb["level"] == "requests" and hb["pending"] == 1
    assert [r["status"] for r in hb["recent"]] == ["done", "pending"]


# -- a pass that fails half way loses nothing (C-62) ---------------------------


class _HalfDownRelay:
    """A real HTTP server standing in for a proxy in front of the relay that
    serves reads but answers writes with 503, the failure the node must survive
    after it has already acted on what it fetched. ``down`` fails reads too."""

    def __init__(self, envelopes):
        import http.server
        import threading

        relay = self
        self.envelopes, self.down, self.posted = list(envelopes), False, []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if relay.down:
                    return self._send(503, {"detail": "down"})
                self._send(200, {"requests": [{"seq": i + 1, "envelope": e} for i, e in enumerate(relay.envelopes)]})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if relay.posts_fail:
                    return self._send(503, {"detail": "write failed"})
                relay.posted.extend(body["results"])
                self._send(200, {"seq": len(relay.posted)})

        self.posts_fail = True
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


def test_results_survive_a_post_that_fails_after_the_node_acted(node, operator):
    env = _req(operator)
    relay = _HalfDownRelay([env])
    client = m.EdgeMailboxClient(relay.url, "token")
    with pytest.raises(OSError):
        m.poll_once(node, client)
    # The request ran and is marked seen; its result must not be lost with the post.
    assert [r["id"] for r in node._read(node.state_dir / "unsent.json", [])] == [env["request"]["id"]]
    relay.posts_fail, relay.envelopes = False, []
    m.poll_once(node, client)
    assert [(r["id"], r["status"]) for r in relay.posted] == [(env["request"]["id"], "done")]
    relay.server.shutdown()


def test_an_expiry_survives_a_fetch_that_fails(node, operator):
    env = _req(operator, action="restart_service", params={"service": "collector"})
    node.receive(env)
    pending = node._read(node._pending_path, {})
    pending[env["request"]["id"]]["expires_at"] = "2000-01-01T00:00:00Z"  # long past, skew included
    node._write(node._pending_path, pending)
    relay = _HalfDownRelay([])
    relay.down = True
    client = m.EdgeMailboxClient(relay.url, "token")
    with pytest.raises(OSError):
        m.poll_once(node, client)
    assert node.pending() == []
    assert [r["status"] for r in node._read(node.state_dir / "unsent.json", [])] == ["expired"]
    relay.down, relay.posts_fail = False, False
    m.poll_once(node, client)
    assert [r["status"] for r in relay.posted] == ["expired"]
    relay.server.shutdown()
