# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""LOOP-S5 / LOOP-S6: a role declares the smoke journeys it must pass, and the result is kept.

The smoke set is a manifest: per role, a list of journey files. It runs against whatever
address it is given (the idle slot before the switch, the front after it, a throwaway node
in CI), and the journey store keeps the verdict by commit so the release cut can read it.
Real HTTP servers, no mocks.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from axiom.extensions.builtins.synthetic import smoke, store
from axiom.extensions.builtins.synthetic.cli import main as synthetic_main
from axiom.extensions.builtins.synthetic.loader import JourneyError

READY = """
[journey]
name = "ready"
base_url = "http://replaced-by-the-slot"

[[step]]
name = "the slot says it is ready"
path = "/readyz"
expect = { status = 200, body_contains = ['"ready":true'] }
"""

GATED = """
[journey]
name = "gate-holds"
base_url = "http://replaced-by-the-slot"

[[step]]
name = "a signed-out API call is refused"
path = "/api/v1/flags"
expect = { status_in = [401, 403] }
"""

MANIFEST = """
[smoke]
description = "what a serving role must pass before traffic moves"

[[role]]
name = "serve"
journeys = ["ready.toml", "gate-holds.toml"]

[[role]]
name = "edge"
journeys = ["ready.toml"]
"""


def _handler(state: dict):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/readyz":
                ok = state.get("ready", True)
                body = b'{"ready":true}' if ok else b'{"ready":false}'
                self.send_response(200 if ok else 503)
            elif self.path == "/api/v1/flags":
                body = b'{"detail":"sign in first"}'
                self.send_response(200 if state.get("open_gate") else 401)
            else:
                body = b"{}"
                self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return H


@pytest.fixture
def slot():
    servers: list[HTTPServer] = []

    def start(**state) -> tuple[str, dict]:
        srv = HTTPServer(("127.0.0.1", 0), _handler(state))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}", state

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    (tmp_path / "ready.toml").write_text(READY)
    (tmp_path / "gate-holds.toml").write_text(GATED)
    path = tmp_path / "smoke.toml"
    path.write_text(MANIFEST)
    return path


@pytest.mark.acceptance("LOOP-S5")
def test_each_role_declares_the_journeys_it_must_pass(manifest: Path):
    sets = smoke.load_smoke_manifest(manifest)
    assert sorted(sets) == ["edge", "serve"]
    assert [j.name for j in sets["serve"].journeys] == ["ready", "gate-holds"]
    assert sets["serve"].declaration() == {"role": "serve", "journeys": ["gate-holds.toml", "ready.toml"]}


@pytest.mark.acceptance("LOOP-S5")
def test_a_role_with_no_journeys_or_a_typo_is_refused_not_skipped(tmp_path: Path, manifest: Path):
    with pytest.raises(JourneyError, match="no smoke journeys are declared for role 'chat'"):
        smoke.smoke_set(manifest, "chat")
    bad = tmp_path / "bad.toml"
    bad.write_text('[[role]]\nname = "serve"\njourney = ["ready.toml"]\n')
    with pytest.raises(JourneyError, match="unknown key"):
        smoke.load_smoke_manifest(bad)
    missing = tmp_path / "missing.toml"
    missing.write_text('[[role]]\nname = "serve"\njourneys = ["nope.toml"]\n')
    with pytest.raises(JourneyError, match="not found"):
        smoke.load_smoke_manifest(missing)


@pytest.mark.acceptance("LOOP-S5")
def test_the_set_runs_against_the_address_it_is_given_and_takes_the_worst_journey(slot, manifest: Path):
    good, _ = slot()
    result = smoke.run_smoke(smoke.smoke_set(manifest, "serve"), good)
    assert result["status"] == "pass" and result["base_url"] == good
    assert [j["status"] for j in result["journeys"]] == ["pass", "pass"]
    assert all(j["base_url"] == good for j in result["journeys"])

    open_gate, _ = slot(open_gate=True)
    failed = smoke.run_smoke(smoke.smoke_set(manifest, "serve"), open_gate)
    assert failed["status"] == "fail"
    assert failed["failing"] == ["gate-holds: a signed-out API call is refused"]


@pytest.mark.acceptance("LOOP-S6")
def test_the_journey_store_keeps_the_newest_verdict_by_commit(tmp_path: Path, slot, manifest: Path):
    good, state = slot()
    sha = "a" * 40
    assert store.verdict(tmp_path, sha) is None
    store.record(tmp_path, commit=sha, result=smoke.run_smoke(smoke.smoke_set(manifest, "serve"), good))
    assert store.verdict(tmp_path, sha)["status"] == "pass"
    state["ready"] = False
    store.record(tmp_path, commit=sha, result=smoke.run_smoke(smoke.smoke_set(manifest, "serve"), good))
    newest = store.verdict(tmp_path, sha)
    assert newest["status"] == "fail", "a later failure supersedes an earlier pass"
    assert len(store.runs(tmp_path, sha)) == 2
    with pytest.raises(ValueError):
        store.record(tmp_path, commit="../escape", result={"status": "pass"})


@pytest.mark.acceptance("LOOP-S5")
@pytest.mark.acceptance("LOOP-S6")
def test_the_cli_is_a_gate_and_records_under_a_commit(tmp_path: Path, slot, manifest: Path, capsys, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    good, _ = slot()
    sha = "b" * 40
    rc = synthetic_main(["smoke", "--manifest", str(manifest), "--role", "serve", "--base-url", good,
                         "--commit", sha, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["status"] == "pass" and out["recorded"]["commit"] == sha
    bad, _ = slot(ready=False)
    assert synthetic_main(["smoke", "--manifest", str(manifest), "--role", "edge", "--base-url", bad]) == 1
    assert "FAIL" in capsys.readouterr().out


@pytest.mark.acceptance("LOOP-S5")
def test_the_platform_smoke_manifest_declares_the_serving_role():
    shipped = Path(__file__).resolve().parents[6] / "infra" / "smoke" / "journeys" / "smoke.toml"
    sset = smoke.smoke_set(shipped, "serve")
    assert {j.name for j in sset.journeys} == {"serve-up", "serve-gate-holds"}
