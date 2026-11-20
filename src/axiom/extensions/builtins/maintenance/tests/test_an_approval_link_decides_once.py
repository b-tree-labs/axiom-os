# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A signed one-time approval link decides one pending request, once, for the person it names (ADR-179 §4).

Real processes, as in the end-to-end maintenance test: an edge composed as
``ingest-edge``, a node running the real ``maintenance`` command, real issued
keys. A person opens the link (which only shows what it decides), presses the
button, and the node acts on its next check-in.

What has to hold:

- opening a link does nothing; pressing it once files it once, and a second
  press is answered "already used";
- a valid link approves exactly once, and the result names the recipient;
- forged, tampered, expired, replayed, other-site, wrong-request and
  not-an-approver links are refused by the node, and nothing runs;
- a decline link declines, and nothing runs.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from axiom.infra import maintenance as m
from axiom.vega.identity.keypair import generate_keypair

from .test_remote_maintenance_end_to_end import (
    OTHER,
    SITE,
    _cli,
    _poll,
    _post_request,
    _results,
    _sign,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="shares the POSIX end-to-end fixture")

LEAD = "lead@site-a.example"


def _open(url: str, method: str = "GET") -> tuple[int, str]:
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - test loopback
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _pending_change(world) -> dict:
    env = _sign(world, action="restart_service", params={"service": "collector"})
    assert _post_request(world, env)[0] == 200
    _poll(world)
    assert _results(world)[env["request"]["id"]]["status"] == "pending"
    return env


def _link(world, request_env, *, key=None, recipient=LEAD, decision="approve", issued=None, ttl_s=600, key_id="op-1"):
    issued = issued or datetime.now(UTC)
    return m.sign_approval(key or world["operator"], key_id=key_id, operator_principal="@ops:org",
                           request_envelope=request_env, recipient=recipient, decision=decision,
                           issued_at=issued, expires_at=issued + timedelta(seconds=ttl_s))


def _audit(world) -> list[dict]:
    path = world["node_state"] / "maintenance" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _link_results(world) -> list[dict]:
    """Every refused link, in order (several may name one request, so not keyed by id)."""
    from .test_remote_maintenance_end_to_end import _http

    status, body = _http("GET", f"{world['url']}/maintenance/results?site={SITE}", world["tokens"]["operator"])
    assert status == 200
    return [r["result"] for r in body["results"] if r["result"].get("status") == "link_refused"]


def _smuggle(world, envelope) -> None:
    """Put a link in the site's mailbox directly, as a misbehaving relay or an attacker with the box could."""
    from axiom.extensions.builtins.data_platform.ingest_sink.maintenance_box import MaintenanceBox

    MaintenanceBox(world["edge_maint"])._append(SITE, "requests", {"approval": envelope, "posted_by": "test"})


def test_a_valid_link_approves_exactly_once(world):
    change = _pending_change(world)
    url = m.approval_link(world["url"], _link(world, change))

    status, page = _open(url)  # a person, or a mail scanner, opens it
    assert status == 200 and "restart_service" in page and "works once" in page
    _poll(world)
    assert not world["marker"].exists(), "opening a link must not act"

    assert _open(url, "POST")[0] == 200
    _poll(world)
    assert world["marker"].read_text() == "restarted"
    assert _results(world)[change["request"]["id"]]["status"] == "done"
    events = [(a["event"], a.get("by") or a.get("recipient")) for a in _audit(world)]
    assert ("link_used", LEAD) in events and ("approved", LEAD) in events

    status, page = _open(url, "POST")  # pressed again
    assert status == 409 and "already been used" in page
    _poll(world)
    assert world["marker"].read_text() == "restarted"  # ran once


def test_a_link_carried_twice_is_refused_by_the_node(world):
    change = _pending_change(world)
    link = _link(world, change)
    _smuggle(world, link)
    _smuggle(world, link)
    _poll(world)
    assert world["marker"].read_text() == "restarted"
    assert [r["reason"] for r in _link_results(world)] == ["replayed"]


def test_bad_links_are_refused_and_nothing_runs(world):
    change = _pending_change(world)
    forged = _link(world, change, key=generate_keypair())
    tampered = _link(world, change)
    tampered["approval"]["recipient"] = "someone-else@example.org"
    expired = _link(world, change, issued=datetime.now(UTC) - timedelta(hours=2), ttl_s=600)
    altered_request = json.loads(json.dumps(change))
    altered_request["request"]["params"] = {"service": "everything"}
    wrong_request = _link(world, altered_request)
    other_site_req = _sign(world, action="restart_service", params={"service": "collector"}, site=OTHER)
    other_site = _link(world, other_site_req)
    for bad in (forged, tampered, expired, wrong_request, other_site):
        _smuggle(world, bad)
    _poll(world)
    assert not world["marker"].exists(), "a bad link ran a change"
    reasons = sorted(r["reason"] for r in _link_results(world))
    assert reasons == sorted(["bad_signature", "bad_signature", "expired", "wrong_request", "wrong_site"])
    # The request is still waiting for a real decision.
    assert [p["id"] for p in json.loads(_cli(world, "pending", "--json").stdout)["pending"]] == [change["request"]["id"]]


def test_only_a_named_approver_can_decide_when_the_site_lists_them(world):
    from pathlib import Path

    wiring = Path(world["node_env"]["AXIOM_MAINTENANCE_WIRING"])
    # Top level, before any table, or TOML files it under the last table.
    wiring.write_text(f'approvers = ["{LEAD}"]\n' + wiring.read_text(encoding="utf-8"), encoding="utf-8")
    change = _pending_change(world)
    _smuggle(world, _link(world, change, recipient="intern@site-a.example"))
    _poll(world)
    assert [r["reason"] for r in _link_results(world)] == ["not_an_approver"]
    assert not world["marker"].exists()
    assert _open(m.approval_link(world["url"], _link(world, change)), "POST")[0] == 200
    _poll(world)
    assert world["marker"].read_text() == "restarted"


def test_a_decline_link_declines_and_nothing_runs(world):
    change = _pending_change(world)
    assert _open(m.approval_link(world["url"], _link(world, change, decision="deny")), "POST")[0] == 200
    _poll(world)
    res = _results(world)[change["request"]["id"]]
    assert res["status"] == "denied" and res["by"] == LEAD
    assert not world["marker"].exists()


def test_a_link_that_is_not_one_is_not_found(world):
    assert _open(f"{world['url']}/approve/not-a-token")[0] == 404
    assert _open(f"{world['url']}/approve/not-a-token", "POST")[0] == 404
