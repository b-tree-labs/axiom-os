# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""LOOP-S5: smoke journeys run against a release in the idle slot before traffic moves, and
again after; a failed journey keeps or restores the previous slot (ADR-170, ADR-169, ADR-197).

The slots are two real HTTP servers (the idle one and the front); the journeys are real
synthetic journeys run over HTTP. Only the slot driver is a recorder, standing in for
``docker compose`` / ``helm``, so the test can see where traffic was sent.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from axiom.extensions.builtins.synthetic import store
from axiom.extensions.builtins.synthetic.smoke import smoke_set
from axiom.infra.deploy import slot_switch
from axiom.infra.switchplan.ledger import FileLedger

pytestmark = pytest.mark.acceptance("LOOP-S5")

READY = """
[journey]
name = "ready"
base_url = "http://replaced"

[[step]]
name = "the release says it is ready"
path = "/readyz"
expect = { status = 200, body_contains = ['"ready":true'] }
"""


class Slot:
    def __init__(self) -> None:
        self.ready = True
        state = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = b'{"ready":true}' if state.ready else b'{"ready":false}'
                self.send_response(200 if state.ready else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


class Driver:
    """Where traffic goes: ``active`` is the slot the front serves."""

    def __init__(self, active: str = "blue") -> None:
        self.current = active
        self.calls: list[tuple[str, str]] = []

    def active(self) -> str:
        return self.current

    def start(self, to: str, *, current: str | None = None) -> None:
        self.calls.append(("start", to))

    def route(self, active: str, *, slots=None) -> None:
        self.calls.append(("route", active))
        self.current = active


@pytest.fixture
def slots():
    made: list[Slot] = []

    def make() -> Slot:
        s = Slot()
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


@pytest.fixture
def sset(tmp_path: Path):
    (tmp_path / "ready.toml").write_text(READY)
    (tmp_path / "smoke.toml").write_text('[[role]]\nname = "serve"\njourneys = ["ready.toml"]\n')
    return smoke_set(tmp_path / "smoke.toml", "serve")


SHA = "c" * 40


def _switch(tmp_path, driver, sset, idle, live, *, notes, **kw):
    kw.setdefault("settle", 0)
    kw.setdefault("commit", SHA)
    kw.setdefault("store_dir", tmp_path / "state")
    return slot_switch.smoke_switch(
        driver, role="serve", to="green", smoke=sset, idle_url=idle.url, live_url=live.url,
        ledger=FileLedger(tmp_path / "ledger"), lease_dir=tmp_path / "leases",
        change_path=tmp_path / "changes.jsonl", notify=lambda outcome, summary: notes.append((outcome, summary)),
        **kw,
    )


def _record(tmp_path: Path, rid: str) -> dict:
    return json.loads((tmp_path / "ledger" / f"{rid}.json").read_text())


def test_passing_journeys_before_and_after_move_traffic_and_are_recorded(tmp_path, slots, sset):
    idle, live = slots(), slots()
    driver, notes = Driver(), []
    out = _switch(tmp_path, driver, sset, idle, live, notes=notes)
    assert out["outcome"] == "switched" and driver.current == "green"
    assert driver.calls == [("start", "green"), ("route", "both"), ("route", "green")]
    rec = _record(tmp_path, out["record"])
    assert rec["state"]["smoke"]["pre"]["status"] == "pass"
    assert rec["state"]["smoke"]["pre"]["base_url"] == idle.url, "before commit the journeys hit the idle slot"
    assert rec["state"]["smoke"]["post"]["status"] == "pass"
    assert rec["state"]["smoke"]["post"]["base_url"] == live.url, "after commit they hit the front"
    assert [n[0] for n in notes] == ["switched"], "one notification per outcome"
    assert store.verdict(tmp_path / "state", SHA)["status"] == "pass", "the deploy is the release's smoke run"


def test_a_failing_journey_in_the_idle_slot_refuses_the_switch_and_keeps_the_previous_slot(tmp_path, slots, sset):
    idle, live = slots(), slots()
    idle.ready = False
    driver, notes = Driver(), []
    out = _switch(tmp_path, driver, sset, idle, live, notes=notes)
    assert out["outcome"] == "refused"
    assert driver.current == "blue" and not [c for c in driver.calls if c[0] == "route"], "traffic never moved"
    rec = _record(tmp_path, out["record"])
    assert rec["state"]["smoke"]["pre"]["status"] == "fail"
    assert "post" not in rec["state"]["smoke"]
    assert rec["phase"] == "rolled_back"
    assert [n[0] for n in notes] == ["refused"]
    assert "the release says it is ready" in notes[0][1]["why"]
    assert store.verdict(tmp_path / "state", SHA)["status"] == "fail"


def test_a_failing_journey_after_commit_restores_the_previous_slot(tmp_path, slots, sset):
    idle, live = slots(), slots()
    live.ready = False  # the front fails once traffic has moved
    driver, notes = Driver(), []
    out = _switch(tmp_path, driver, sset, idle, live, notes=notes)
    assert out["outcome"] == "rolled_back"
    assert driver.current == "blue", "the previous slot serves again"
    assert driver.calls[-2:] == [("route", "both"), ("route", "blue")], "back through both: no gap"
    rec = _record(tmp_path, out["record"])
    assert rec["state"]["smoke"]["pre"]["status"] == "pass"
    assert rec["state"]["smoke"]["post"]["status"] == "fail"
    assert rec["phase"] == "rolled_back"
    assert [n[0] for n in notes] == ["rolled_back"]


def test_the_journeys_run_again_through_the_soak_and_a_late_failure_rolls_back(tmp_path, slots, sset):
    idle, live = slots(), slots()
    driver, notes = Driver(), []
    ticks = {"n": 0}

    def sleep(_s):
        ticks["n"] += 1
        if ticks["n"] == 2:
            live.ready = False

    out = _switch(tmp_path, driver, sset, idle, live, notes=notes, soak=timedelta(hours=1),
                  soak_interval_s=1, sleep=sleep)
    assert out["outcome"] == "rolled_back" and driver.current == "blue"
    assert [n[0] for n in notes] == ["rolled_back"]


def test_dry_run_moves_nothing_writes_nothing_and_says_what_it_would_run(tmp_path, slots, sset):
    idle, live = slots(), slots()
    driver, notes = Driver(), []
    out = _switch(tmp_path, driver, sset, idle, live, notes=notes, dry_run=True)
    assert out["outcome"] == "dry_run"
    assert out["from"] == "blue" and out["to"] == "green"
    assert out["journeys"] == ["ready.toml"]
    assert out["before_commit"] == idle.url and out["after_commit"] == live.url
    assert driver.calls == [] and notes == []
    assert not (tmp_path / "ledger").exists()


def test_a_switch_to_the_slot_already_serving_is_refused_with_nothing_started(tmp_path, slots, sset):
    idle, live = slots(), slots()
    driver, notes = Driver(active="green"), []
    out = _switch(tmp_path, driver, sset, idle, live, notes=notes)
    assert out["outcome"] == "refused" and driver.calls == []
    assert "already serves" in out["why"]


def test_the_cli_takes_a_smoke_manifest_and_a_dry_run(tmp_path, slots, sset, monkeypatch, capsys):
    from axiom.infra.deploy import bluegreen

    idle, live = slots(), slots()
    driver = Driver()
    monkeypatch.setattr(slot_switch, "_driver_for", lambda a: driver)
    rc = bluegreen.main(["compose", "--role", "serve", "--to", "green", "--smoke", sset.manifest,
                         "--idle-url", idle.url, "--live-url", live.url, "--dry-run"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0 and out["outcome"] == "dry_run" and driver.calls == []
    rc = bluegreen.main(["compose", "--role", "serve", "--to", "green", "--smoke", sset.manifest,
                         "--live-url", live.url])
    assert rc == 2, "a smoke-gated switch needs the idle slot's address"
    assert slot_switch.DEFAULT_RECIPIENTS == ("@site-admin",), "outcomes reach the site admin role by default"
