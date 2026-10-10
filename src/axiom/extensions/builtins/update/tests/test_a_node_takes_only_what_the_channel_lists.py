# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A node installs only versions its signed release channel lists, with the wheel the channel names (ADR-179 §3).

Real processes: an edge composed as ``ingest-edge`` serving the manifest, a
package index served over HTTP, real pip and real virtual environments.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest

from axiom.extensions.builtins.update import channel as ch
from axiom.extensions.builtins.update import swap
from axiom.infra.maintenance import b64
from axiom.vega.identity.keypair import generate_keypair

from .test_a_node_updates_by_swapping_and_rolls_back import _wheel

CHANNEL, PKG = "partner-stable", "nodecheck"
CHECK = [["nodecheck"]]

_EDGE = """
import uvicorn, sys
from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.registry import RouterRegistry
app = compose_app(profile="ingest-edge", registry=RouterRegistry(), bind_host="0.0.0.0")
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    pytest.importorskip("uvicorn")
    tmp = tmp_path_factory.mktemp("channel")
    index = tmp / "index"
    index.mkdir()
    wheels = {v: _wheel(index, v) for v in ("1.0.0", "1.0.1", "1.1.0", "1.2.0")}
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(http.server.SimpleHTTPRequestHandler,
                                                                      directory=str(index)))
    srv.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    channels = tmp / "channels"
    src = str(Path(ch.__file__).resolve().parents[4])
    env = {k: v for k, v in os.environ.items() if k not in ("AXIOM_API_KEY", "AXIOM_HTTP_API_KEYS")}
    env.update(PYTHONPATH=src + os.pathsep + env.get("PYTHONPATH", ""), AXI_STATE_DIR=str(tmp / "edge-state"),
               AXIOM_INGEST_OUTBOX_DIR=str(tmp / "edge-outbox"), AXIOM_MODE="dev",
               AXIOM_GATE_API_KEYS_FILE=str(tmp / "keys.json"), **{ch.DIR_ENV: str(channels)})
    port = _free_port()
    edge = subprocess.Popen([sys.executable, "-c", _EDGE, str(port)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            urllib.request.urlopen(f"{url}/healthz", timeout=2)
            break
        except OSError:
            if edge.poll() is not None:
                pytest.fail(edge.stdout.read().decode())
            time.sleep(0.1)
    key = generate_keypair()
    yield {"url": url, "index": f"http://127.0.0.1:{srv.server_address[1]}/", "wheels": wheels,
           "channels": channels, "key": key, "tmp": tmp}
    edge.terminate()
    edge.wait(timeout=10)
    srv.shutdown()


def _publish(world, releases, *, key=None, issued=None, valid_for=timedelta(days=7), channel=CHANNEL):
    env = ch.sign_manifest(key or world["key"], key_id="rel-1", channel=channel, package=PKG,
                           releases=releases, issued_at=issued, valid_for=valid_for)
    ch.publish(env, world["channels"])
    return env


def _rel(world, v, notes="", sha=None):
    return {"version": v, "sha256": sha or _sha(world["wheels"][v]), "notes": notes or f"notes for {v}"}


@pytest.fixture
def node(world, tmp_path):
    root = tmp_path / "node"
    assert swap.apply_update(root, f"{PKG}==1.0.0", "1.0.0", checks=CHECK, find_links=Path(world["wheels"]["1.0.0"]).parent).status == "updated"
    n = ch.ChannelNode(channel=CHANNEL, package=PKG, trusted_keys={"rel-1": b64(world["key"].public_bytes)},
                       state_dir=tmp_path / "state")
    return n, root


def _update(world, node, **kw):
    n, root = node
    return ch.update_from_channel(n, world["url"], root, checks=CHECK, find_links=world["index"], **kw)


def test_a_fix_on_the_channel_is_taken_on_its_own(world, node):
    _publish(world, [_rel(world, "1.0.1")])
    out = _update(world, node)
    assert out["status"] == "updated" and out["to"] == "1.0.1"
    assert swap.current_version(node[1]) == "1.0.1"


def test_a_feature_release_waits_for_approval_and_says_what_changed(world, node):
    _publish(world, [_rel(world, "1.0.1", "a fix"), _rel(world, "1.1.0", "a feature")])
    out = _update(world, node, policy="approve")
    assert out["status"] == "ask" and out["version"] == "1.1.0"
    assert "a fix" in out["notes"] and "a feature" in out["notes"]
    assert _update(world, node, policy="approve", approved={"1.1.0"})["to"] == "1.1.0"


def test_a_version_on_the_index_but_not_on_the_channel_is_never_installed(world, node):
    _publish(world, [_rel(world, "1.0.1")])  # 1.2.0 is on the index, not listed
    _update(world, node, approved={"1.2.0"})
    assert swap.current_version(node[1]) == "1.0.1"


def test_a_wheel_that_does_not_match_the_channel_is_refused(world, node):
    _publish(world, [_rel(world, "1.0.1", sha="0" * 64)])
    out = _update(world, node)
    assert out["status"] == "refused" and out["reason"] == "hash_mismatch"
    assert swap.current_version(node[1]) == "1.0.0"


def test_bad_manifests_are_refused(world, node):
    n, root = node
    _publish(world, [_rel(world, "1.0.1")], key=generate_keypair())
    assert _update(world, node)["reason"] == "bad_signature"
    env = _publish(world, [_rel(world, "1.0.1")])
    env["manifest"]["releases"].append(_rel(world, "1.2.0"))  # tampered after signing
    ch.publish(env, world["channels"])
    assert _update(world, node)["reason"] == "bad_signature"
    _publish(world, [_rel(world, "1.0.1")], issued=datetime.now(UTC) - timedelta(days=10), valid_for=timedelta(days=7))
    assert _update(world, node)["reason"] == "expired"
    assert swap.current_version(root) == "1.0.0"


def test_an_older_manifest_cannot_walk_a_node_back(world, node):
    now = datetime.now(UTC)
    _publish(world, [_rel(world, "1.0.1")], issued=now - timedelta(hours=1))
    assert _update(world, node)["status"] == "updated"
    _publish(world, [_rel(world, "1.0.1")], issued=now - timedelta(days=2))  # an older one, replayed
    assert _update(world, node)["reason"] == "rolled_back"


def test_a_node_only_reads_its_own_channel(world, node):
    n, _root = node
    other = ch.sign_manifest(world["key"], key_id="rel-1", channel="beta", package=PKG, releases=[_rel(world, "1.2.0")])
    ch.publish(other, world["channels"])
    path = world["channels"] / f"{CHANNEL}.json"
    path.write_text(json.dumps(other), encoding="utf-8")  # the beta manifest served under this channel's name
    assert _update(world, node)["reason"] == "wrong_channel"


def test_the_edge_serves_only_channel_files(world):
    for bad in ("..%2Fkeys.json", "nope.json", "x"):
        try:
            urllib.request.urlopen(f"{world['url']}/releases/{bad}", timeout=5)
            raise AssertionError(f"{bad} was served")
        except urllib.error.HTTPError as e:
            assert e.code == 404
