# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Remote maintenance, end to end, with real processes (ADR-183).

An edge composed as ``ingest-edge`` runs as its own server process with real
issued keys. An operator signs requests and posts them to it. The site's node
runs the real ``maintenance`` command in its own processes: it fetches the
requests addressed to its site over an outbound connection, verifies each,
acts, and posts results back, which the operator then reads.

What has to hold, with nothing but the relay between the two:

- forged, replayed, expired, other-site and disallowed requests are refused,
  and nothing runs;
- an approved (read-only) action runs, and its result reaches the operator;
- a change waits until a person at the site approves it, then runs;
- a node reads only its own site's requests, an operator only with a listed
  credential, and a site's key can never post a request;
- a site that turns maintenance off refuses everything;
- a support session is opened only by the site's node, carries a real shell's
  bytes both ways, is recorded on both sides, and ends when the site ends it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.infra import maintenance as m
from axiom.vega.identity.keypair import generate_keypair

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="support sessions use a POSIX pseudo-terminal")

SITE = "site-a"
OTHER = "site-b"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method, url, token, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - test loopback
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


_EDGE = """
import uvicorn, sys
from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.registry import RouterRegistry
app = compose_app(profile="ingest-edge", registry=RouterRegistry(), bind_host="0.0.0.0")
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
"""


@pytest.fixture
def world(tmp_path):
    pytest.importorskip("uvicorn")
    from axiom.webauth import append_api_key_record, mint_api_key

    keys = tmp_path / "api-keys.json"
    tokens = {}
    for name, principal, scopes, site in (
        ("node", "@daq:site-a", ("maintenance:read", "maintenance:invoke"), SITE),
        ("other_node", "@daq:site-b", ("maintenance:read", "maintenance:invoke"), OTHER),
        ("operator", "@ops:org", ("maintenance:read", "maintenance:invoke"), None),
        ("stranger", "@someone:org", ("maintenance:read", "maintenance:invoke"), None),
    ):
        token, rec = mint_api_key(principal=principal, scopes=scopes, **({"site": site} if site else {}))
        append_api_key_record(keys, rec)
        tokens[name] = token

    src = str(Path(m.__file__).resolve().parents[2])
    base_env = {k: v for k, v in os.environ.items()
                if k not in ("AXIOM_API_KEY", "AXIOM_HTTP_API_KEYS", "AXIOM_SERVE_INSECURE")}
    base_env["PYTHONPATH"] = src + os.pathsep + base_env.get("PYTHONPATH", "")
    edge_env = {**base_env, "AXI_STATE_DIR": str(tmp_path / "edge-state"),
                "AXIOM_INGEST_OUTBOX_DIR": str(tmp_path / "edge-outbox"),
                "AXIOM_MAINTENANCE_DIR": str(tmp_path / "edge-maint"),
                "AXIOM_GATE_API_KEYS_FILE": str(keys), "AXIOM_MODE": "dev",
                "AXIOM_MAINTENANCE_OPERATORS": "@ops:org",
                "AXIOM_RELEASE_CHANNEL_DIR": str(tmp_path / "channels")}
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    edge = {}

    def start_edge():
        proc = subprocess.Popen([sys.executable, "-c", _EDGE, str(port)], env=edge_env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(200):
            try:
                if _http("GET", f"{url}/healthz", "")[0] == 200:
                    break
            except OSError:
                pass
            if proc.poll() is not None:
                pytest.fail(proc.stdout.read().decode())
            time.sleep(0.1)
        edge["proc"] = proc

    def stop_edge():
        proc = edge.pop("proc", None)
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)

    start_edge()

    operator = generate_keypair()
    marker = tmp_path / "restarted.txt"
    node_state = tmp_path / "node-state"
    node_state.mkdir()
    (node_state / "node.toml").write_text(
        '[node]\nrole = "collector"\nfunctions = ["acquire", "transmit"]\nagents = "none"\n'
        'maintenance = "sessions"\n')
    wiring = node_state / "maintenance.toml"
    py = json.dumps(sys.executable)
    wiring.write_text(f"""
site = "{SITE}"
relay_url = "{url}"
key_ref = "env://NODE_RELAY_KEY"
support_shell = ["/bin/sh", "-i"]

[trusted_keys.op-1]
public_key = "{m.b64(operator.public_bytes)}"
operator = "@ops:org"

[commands]
diagnose = [{py}, "-c", "print('all checks passed')"]

[commands.restart_service]
collector = [{py}, "-c", 'open({json.dumps(str(marker))}, "a").write("restarted")']
""")
    node_env = {**base_env, "AXI_STATE_DIR": str(node_state), "AXIOM_NODE_CONFIG": str(node_state / "node.toml"),
                "AXIOM_MAINTENANCE_WIRING": str(wiring), "NODE_RELAY_KEY": tokens["node"]}
    yield {"url": url, "tokens": tokens, "operator": operator, "node_env": node_env, "marker": marker,
           "node_state": node_state, "edge_maint": tmp_path / "edge-maint",
           "start_edge": start_edge, "stop_edge": stop_edge,
           "channels": tmp_path / "channels", "wiring": wiring}
    stop_edge()


def _cli(world, *args, input_=None, timeout=60):
    return subprocess.run([sys.executable, "-m", "axiom.extensions.builtins.maintenance.cli", *args],
                          env=world["node_env"], capture_output=True, text=True, timeout=timeout, input=input_)


def _post_request(world, envelope, token="operator"):
    return _http("POST", f"{world['url']}/maintenance/requests", world["tokens"][token], envelope)


def _sign(world, *, action="diagnose", params=None, site=SITE, issued=None, ttl_s=600, key=None, key_id="op-1"):
    issued = issued or datetime.now(UTC)
    return m.sign_request(key or world["operator"], key_id=key_id, operator_principal="@ops:org", site=site,
                          action=action, params=params or {}, issued_at=issued,
                          expires_at=issued + timedelta(seconds=ttl_s))


def _results(world, site=SITE):
    status, body = _http("GET", f"{world['url']}/maintenance/results?site={site}", world["tokens"]["operator"])
    assert status == 200
    return {r["result"]["id"]: r["result"] for r in body["results"]}


def _poll(world):
    out = _cli(world, "poll")
    assert out.returncode == 0, out.stderr
    return out


def test_only_the_right_parties_can_use_the_relay(world):
    url, t = world["url"], world["tokens"]
    env = _sign(world)
    assert _post_request(world, env, token="node")[0] == 403          # a site's key cannot post requests
    assert _post_request(world, env, token="stranger")[0] == 403      # nor an operator nobody listed
    assert _http("POST", f"{url}/maintenance/requests", "", env)[0] in (401, 403)
    assert _post_request(world, env)[0] == 200
    assert _post_request(world, _sign(world, site=OTHER))[0] == 200
    status, body = _http("GET", f"{url}/maintenance/requests?after=0", t["other_node"])
    assert status == 200 and [r["envelope"]["request"]["site"] for r in body["requests"]] == [OTHER]
    assert _http("GET", f"{url}/maintenance/requests?after=0", t["operator"])[0] == 403
    assert _http("GET", f"{url}/maintenance/results?site={SITE}", t["node"])[0] == 403


def test_bad_requests_are_refused_and_good_ones_run_or_wait(world):
    stranger = generate_keypair()
    good = _sign(world)
    forged = _sign(world, key=stranger)
    tampered = _sign(world)
    tampered["request"]["action"] = "restart_service"
    tampered["request"]["params"] = {"service": "collector"}
    expired = _sign(world, issued=datetime.now(UTC) - timedelta(hours=3))
    elsewhere = _sign(world, site=OTHER)
    disallowed = _sign(world, action="run_shell", params={"cmd": "id"})
    change = _sign(world, action="restart_service", params={"service": "collector"})
    for env in (good, forged, tampered, expired, disallowed, change):
        assert _post_request(world, env)[0] == 200
    # A request for another site, smuggled into this site's queue by a relay
    # that misfiled it, is still refused by the node.
    box = __import__("axiom.extensions.builtins.data_platform.ingest_sink.maintenance_box",
                     fromlist=["MaintenanceBox"]).MaintenanceBox(world["edge_maint"])
    box._append(SITE, "requests", {"envelope": elsewhere, "posted_by": "@ops:org"})

    _poll(world)
    res = _results(world)
    rid = lambda e: e["request"]["id"]  # noqa: E731
    assert res[rid(good)]["status"] == "done" and "all checks passed" in res[rid(good)]["output_tail"]
    assert res[rid(forged)]["reason"] == "bad_signature"
    assert res[rid(tampered)]["reason"] == "bad_signature"
    assert res[rid(expired)]["reason"] == "expired"
    assert res[rid(elsewhere)]["reason"] == "wrong_site"
    assert res[rid(disallowed)]["reason"] == "unknown_action"
    assert res[rid(change)]["status"] == "pending"
    assert not world["marker"].exists(), "an unapproved change ran"

    # Replayed: the operator (or anyone holding the envelope) posts it again.
    assert _post_request(world, good)[0] == 200
    _poll(world)
    replays = [r["result"] for r in _http("GET", f"{world['url']}/maintenance/results?site={SITE}",
                                           world["tokens"]["operator"])[1]["results"]]
    assert replays[-1]["id"] == rid(good) and replays[-1]["reason"] == "replayed"

    # The person at the site sees it waiting, approves it, and it runs.
    pending = _cli(world, "pending", "--json")
    assert [p["id"] for p in json.loads(pending.stdout)["pending"]] == [rid(change)]
    out = _cli(world, "approve", rid(change), "--by", "site-admin")
    assert out.returncode == 0, out.stderr
    assert world["marker"].read_text() == "restarted"
    assert _results(world)[rid(change)]["status"] == "done"

    audit = [json.loads(line) for line in (world["node_state"] / "maintenance" / "audit.jsonl").read_text().splitlines()]
    events = [(a["event"], a.get("reason")) for a in audit]
    for expected in (("refused", "bad_signature"), ("refused", "expired"), ("refused", "wrong_site"),
                     ("refused", "unknown_action"), ("refused", "replayed"), ("pending", None),
                     ("approved", None), ("done", None)):
        assert expected in events, expected
    assert all(a["site"] == SITE for a in audit)

    status = _cli(world, "status", "--json")
    hb = json.loads(status.stdout)
    assert hb["level"] == "sessions" and hb["pending"] == 0 and hb["site"] == SITE


def test_a_site_that_turns_maintenance_off_refuses_everything(world):
    out = subprocess.run([sys.executable, "-m", "axiom.extensions.builtins.features.cli", "maintenance", "off"],
                         env=world["node_env"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    env = _sign(world)
    _post_request(world, env)
    _poll(world)
    assert _results(world)[env["request"]["id"]]["reason"] == "maintenance_off"
    opened = subprocess.run([sys.executable, "-m", "axiom.extensions.builtins.maintenance.cli", "support",
                             "open", "--for", "5m"], env=world["node_env"], capture_output=True, text=True, timeout=60)
    assert opened.returncode != 0 and 'maintenance = "off"' in opened.stderr


def test_a_support_session_is_opened_by_the_site_recorded_and_ended_by_the_site(world):
    from axiom.infra.support_session import SessionError, SupportClient

    url, t = world["url"], world["tokens"]
    op = SupportClient(url, t["operator"])
    # No operator can open one.
    with pytest.raises(SessionError) as exc:
        op.open(duration_s=600, reason="let me in")
    assert exc.value.status == 403

    node = subprocess.Popen(
        [sys.executable, "-m", "axiom.extensions.builtins.maintenance.cli", "support", "open", "--for", "10m",
         "--reason", "collector stalls"],
        env=world["node_env"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        sessions = []
        for _ in range(100):
            sessions = [s for s in op.list(SITE) if s["open"]]
            if sessions:
                break
            time.sleep(0.1)
        assert len(sessions) == 1 and sessions[0]["reason"] == "collector stalls"
        sid = sessions[0]["id"]
        # Another site's node cannot see it.
        with pytest.raises(SessionError):
            SupportClient(url, t["other_node"]).show(sid)

        op.send(sid, b"echo answer-$((40+2))\n")
        seen, after = b"", 0
        deadline = time.monotonic() + 30
        while b"answer-42" not in seen and time.monotonic() < deadline:
            got = op.recv(sid, after, wait_s=2)
            seen += got["bytes"]
            after = got["next"]
        assert b"answer-42" in seen, seen

        # The site ends it, from another terminal.
        closed = subprocess.run(
            [sys.executable, "-m", "axiom.extensions.builtins.maintenance.cli", "support", "close"],
            env=world["node_env"], capture_output=True, text=True, timeout=60)
        assert closed.returncode == 0, closed.stderr
        node.wait(timeout=30)
        assert node.returncode == 0, node.stderr.read().decode()   # ended, not killed mid-cleanup
        assert not op.show(sid)["open"]
        with pytest.raises(SessionError) as exc:
            op.send(sid, b"echo too late\n")
        assert exc.value.status == 410

        # The person at the site saw everything as it happened.
        assert b"answer-42" in node.stdout.read()
        # Both ends kept a recording.
        relay_rec = op.recording(sid)
        assert "echo answer-$((40+2))" in relay_rec and "answer-42" in relay_rec and "session closed by" in relay_rec
        node_rec = (world["node_state"] / "maintenance" / "sessions" / f"{sid}.cast").read_text()
        assert "answer-42" in node_rec and "session ended" in node_rec
        audit = (world["node_state"] / "maintenance" / "audit.jsonl").read_text()
        assert "session_opened" in audit and "session_closed" in audit
    finally:
        if node.poll() is None:
            node.kill()


def test_the_heartbeat_carries_the_maintenance_state(world, monkeypatch):
    from axiom.extensions.builtins.data_platform.daq.beat import HeartbeatSender

    for k in ("AXI_STATE_DIR", "AXIOM_NODE_CONFIG", "AXIOM_MAINTENANCE_WIRING"):
        monkeypatch.setenv(k, world["node_env"][k])
    sent = []

    class Capture:
        def post(self, url, body, headers):
            sent.append(json.loads(body))
            return 200, "{}", {}

    HeartbeatSender(face_url=world["url"], source="s", bearer=lambda: None, transport=Capture()).send({"node": "n1"})
    beat = sent[0]["batches"][0]["rows"][0]
    assert beat["maintenance"]["level"] == "sessions" and beat["maintenance"]["pending"] == 0


def test_results_the_relay_could_not_take_are_kept_and_sent_when_it_returns(world):
    """C-62: a relay that is down loses no result; the next pass delivers it."""
    change = _sign(world, action="restart_service", params={"service": "collector"})
    assert _post_request(world, change)[0] == 200
    _poll(world)
    assert _results(world)[change["request"]["id"]]["status"] == "pending"

    world["stop_edge"]()
    out = _cli(world, "approve", change["request"]["id"], "--by", "site-admin")
    assert out.returncode == 0, out.stderr                # approving never depends on the relay
    assert world["marker"].read_text() == "restarted"
    unsent = json.loads((world["node_state"] / "maintenance" / "unsent.json").read_text())
    assert [r["id"] for r in unsent] == [change["request"]["id"]]
    assert _cli(world, "poll").returncode == 0            # a pass with the relay down fails softly
    unsent = json.loads((world["node_state"] / "maintenance" / "unsent.json").read_text())
    assert [r["id"] for r in unsent] == [change["request"]["id"]], "a failed pass dropped a result"

    world["start_edge"]()
    _poll(world)
    assert _results(world)[change["request"]["id"]]["status"] == "done"
    assert json.loads((world["node_state"] / "maintenance" / "unsent.json").read_text()) == []


def test_an_edge_restart_ends_an_open_session_and_both_recordings_survive(world):
    """C-66: sessions live in the relay's memory; a restart ends them, never strands them."""
    from axiom.infra.support_session import SupportClient

    url, t = world["url"], world["tokens"]
    op = SupportClient(url, t["operator"])
    node = subprocess.Popen(
        [sys.executable, "-m", "axiom.extensions.builtins.maintenance.cli", "support", "open", "--for", "10m"],
        env=world["node_env"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        sessions = []
        for _ in range(100):
            sessions = [s for s in op.list(SITE) if s["open"]]
            if sessions:
                break
            time.sleep(0.1)
        sid = sessions[0]["id"]
        op.send(sid, b"echo before-restart\n")
        after, seen = 0, b""
        deadline = time.monotonic() + 20
        while b"before-restart\r\n" not in seen and time.monotonic() < deadline:
            got = op.recv(sid, after, wait_s=2)
            seen, after = seen + got["bytes"], got["next"]

        world["stop_edge"]()
        world["start_edge"]()

        node.wait(timeout=30)                              # the node notices and ends, it does not hang
        assert node.returncode == 0
        view = node.stdout.read().decode()
        assert '"ended": "the relay ended the session"' in view, view
        assert sid not in [s["id"] for s in op.list(SITE)]
        node_rec = (world["node_state"] / "maintenance" / "sessions" / f"{sid}.cast").read_text()
        assert "before-restart" in node_rec and "session ended: the relay ended the session" in node_rec
        relay_rec = (world["edge_maint"] / "sessions" / f"{sid}.cast").read_text()
        assert "before-restart" in relay_rec
        assert not (world["node_state"] / "maintenance" / "support-session.json").exists()
    finally:
        if node.poll() is None:
            node.kill()


def _children(pid: int) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    return [int(p) for p in out.stdout.split()]


def test_a_signal_racing_a_relay_driven_end_still_completes_the_drain(world):
    """The relay ends a session (a time box, an edge restart) and a SIGTERM
    arrives about the same time: a host shutting the node down. The drain is
    what must survive, whatever the order: the end marker is recorded, the
    audit line is written, and no active-session file is stranded. On main the
    signal landing during cleanup killed the node and lost all three.

    Exit code is not the contract here. A signal caught during the loop or the
    drain exits 0 (the `support close` path asserts that); one that lands in the
    tail after the drain has finished and handlers are restored ends the already
    done process by signal, which is harmless. So 0 or -SIGTERM both pass; a
    lost drain does not."""
    import signal

    from axiom.infra.support_session import SupportClient

    url, t = world["url"], world["tokens"]
    op = SupportClient(url, t["operator"])
    node = subprocess.Popen(
        [sys.executable, "-m", "axiom.extensions.builtins.maintenance.cli", "support", "open", "--for", "10m"],
        env=world["node_env"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        sessions = []
        for _ in range(100):
            sessions = [s for s in op.list(SITE) if s["open"]]
            if sessions:
                break
            time.sleep(0.1)
        sid = sessions[0]["id"]
        deadline = time.monotonic() + 20
        while not _children(node.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _children(node.pid), "the session shell never started"

        # The relay ends the session; the node's long-poll returns and it begins
        # the drain. A SIGTERM arrives concurrently, as a host shutdown would.
        op.close(sid)
        os.kill(node.pid, signal.SIGTERM)

        node.wait(timeout=30)
        assert node.returncode in (0, -signal.SIGTERM), node.stderr.read().decode()
        node_rec = (world["node_state"] / "maintenance" / "sessions" / f"{sid}.cast").read_text()
        assert "session ended" in node_rec, node_rec
        assert "session_closed" in (world["node_state"] / "maintenance" / "audit.jsonl").read_text()
        assert not (world["node_state"] / "maintenance" / "support-session.json").exists()
    finally:
        if node.poll() is None:
            node.kill()
