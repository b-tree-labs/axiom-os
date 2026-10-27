# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""From a release on the channel to an approved install, with nobody logging in (ADR-179).

Real processes throughout: the edge (relay, approval links and release channel
on one host), the node's ``maintenance poll`` service and the command an
approved ``apply_update`` runs, a real SMTP server receiving HERALD's notice,
a package index over HTTP, real pip and real virtual environments.

1. The operator publishes a fix and a feature release on the node's channel.
2. The node's scheduled pass takes the fix on its own and reports the feature
   release as waiting, with its notes, in its heartbeat.
3. The operator's notice sends the site's contact an email with what changed
   and that person's own Approve and Not now links.
4. Opening the link changes nothing; pressing Approve files it; the node's
   next pass verifies it and installs the release from the channel.
5. The result stops the ladder: no reminder follows.
"""

from __future__ import annotations

import http.server
import json
import re
import socketserver
import sys
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from axiom.extensions.builtins.update import channel as ch
from axiom.extensions.builtins.update import swap
from axiom.extensions.builtins.update.escalation import Escalation, herald_deliverer
from axiom.extensions.builtins.update.notice import UpdateNotice, relay_poster
from axiom.extensions.builtins.update.tests.test_a_node_updates_by_swapping_and_rolls_back import (
    _wheel,
)
from axiom.extensions.builtins.update.tests.test_an_unanswered_update_climbs_the_ladder import (
    _SMTP,
    _text,
)
from axiom.infra import maintenance as m
from axiom.vega.identity.keypair import generate_keypair

from .test_remote_maintenance_end_to_end import SITE, _cli, _http

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="shares the POSIX end-to-end fixture")

LEAD = "lead@site-a.example"


def _open(url: str, method: str = "GET") -> int:
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - test loopback
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture
def stack(world, tmp_path):
    index = tmp_path / "index"
    index.mkdir()
    wheels = {v: _wheel(index, v) for v in ("1.0.0", "1.0.1", "1.1.0")}
    web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(http.server.SimpleHTTPRequestHandler,
                                                                     directory=str(index)))
    web.RequestHandlerClass.log_message = lambda *a: None
    smtp = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTP)
    smtp.messages = []
    for s in (web, smtp):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    find_links = f"http://127.0.0.1:{web.server_address[1]}/"

    root = tmp_path / "collector"
    assert swap.apply_update(root, "nodecheck==1.0.0", "1.0.0", checks=[["nodecheck"]],
                             find_links=index).status == "updated"
    release_key = generate_keypair()
    py = json.dumps(sys.executable)
    wiring = world["wiring"]
    text = wiring.read_text(encoding="utf-8").replace(
        "[commands]\n",
        "[commands]\napply_update = "
        f'[{py}, "-m", "axiom.extensions.builtins.maintenance.cli", "apply-update", "--version", "{{version}}"]\n', 1)
    text += f"""
[update]
channel_url = "{world['url']}"
channel = "partner-stable"
package = "nodecheck"
root = "{root}"
find_links = "{find_links}"
check_every_s = 0
checks = [["nodecheck"]]

[update.release_keys]
rel-1 = "{m.b64(release_key.public_bytes)}"
"""
    wiring.write_text(text, encoding="utf-8")
    yield {"root": root, "wheels": wheels, "release_key": release_key, "smtp": smtp, "tmp": tmp_path}
    web.shutdown()
    smtp.shutdown()


def _publish(world, stack, releases):
    ch.publish(ch.sign_manifest(stack["release_key"], key_id="rel-1", channel="partner-stable", package="nodecheck",
                                releases=[{"version": v, "notes": n,
                                           "sha256": __import__("hashlib").sha256(stack["wheels"][v].read_bytes()).hexdigest()}
                                          for v, n in releases]), world["channels"])


def _poll(world):
    """One scheduled pass. Longer than a status call: it may build and check a new environment."""
    out = _cli(world, "poll", timeout=600)
    assert out.returncode == 0, out.stderr
    return out


def _status(world) -> dict:
    out = _cli(world, "status", "--json")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _all_results(world) -> list[dict]:
    status, body = _http("GET", f"{world['url']}/maintenance/results?site={SITE}", world["tokens"]["operator"])
    assert status == 200
    return [r["result"] for r in body["results"]]


def test_a_release_goes_from_the_channel_to_installed_by_one_click(world, stack):
    _publish(world, stack, [("1.0.1", "- a fix for late timestamps"), ("1.1.0", "- reads a second archive")])

    # 2. The scheduled pass: the fix applies on its own, the feature release waits.
    _poll(world)
    assert swap.current_version(stack["root"]) == "1.0.1"
    _poll(world)
    waiting = _status(world)["update"]
    assert waiting["status"] == "ask" and waiting["version"] == "1.1.0"
    assert "reads a second archive" in waiting["notes"]

    # 3. The operator's notice, through HERALD's email adapter to a real SMTP server.
    esc = Escalation.open(stack["tmp"] / "notice.json", site=SITE, node="daq-pc-1", version=waiting["version"],
                          contacts=[LEAD], operator="operator@platform.example", notes=waiting["notes"], link="")
    notice = UpdateNotice(esc, keypair=world["operator"], key_id="op-1", operator_principal="@ops:org",
                          edge_url=world["url"], post=relay_poster(world["url"], world["tokens"]["operator"]))
    deliver = herald_deliverer(email_config={
        "from_address": "updates@platform.example", "smtp_host": "127.0.0.1",
        "smtp_port": stack["smtp"].server_address[1], "smtp_use_tls": False, "smtp_timeout": 5})
    now = datetime.now(UTC)
    notice.step(now, deliver)
    (mail,) = stack["smtp"].messages
    assert mail["to"] == [LEAD]
    body = _text(mail)
    assert "reads a second archive" in body
    approve = re.search(r"Approve: (\S+)", body).group(1)
    not_now = re.search(r"Not now: (\S+)", body).group(1)
    assert approve != not_now and "/approve/" in approve

    # 4. Opening it (a mail scanner, a preview) does nothing; pressing it does.
    assert _open(approve) == 200
    _poll(world)
    assert swap.current_version(stack["root"]) == "1.0.1"
    assert _open(approve, "POST") == 200
    _poll(world)
    assert swap.current_version(stack["root"]) == "1.1.0"
    rid = notice._state["requests"][-1]["request"]["id"]
    assert [r["status"] for r in _all_results(world) if r.get("id") == rid][-1] == "done"
    audit = [json.loads(x) for x in (world["node_state"] / "maintenance" / "audit.jsonl").read_text().splitlines()]
    assert ("approved", LEAD) in [(a["event"], a.get("by")) for a in audit]

    # 5. The result stops the ladder; a week later nothing more is sent.
    assert notice.observe(_all_results(world))["decision"] == "approve"
    notice.step(now + timedelta(days=8), deliver)
    assert len(stack["smtp"].messages) == 1

    # And the waiting release is gone from the heartbeat once installed.
    _poll(world)
    assert _status(world)["update"]["status"] == "current"


def test_not_now_declines_and_nothing_is_installed(world, stack):
    _publish(world, stack, [("1.1.0", "- reads a second archive")])
    _poll(world)
    esc = Escalation.open(stack["tmp"] / "notice.json", site=SITE, node="daq-pc-1", version="1.1.0",
                          contacts=[LEAD], operator="operator@platform.example", notes="- x", link="")
    notice = UpdateNotice(esc, keypair=world["operator"], key_id="op-1", operator_principal="@ops:org",
                          edge_url=world["url"], post=relay_poster(world["url"], world["tokens"]["operator"]))
    deliver = herald_deliverer(email_config={
        "from_address": "updates@platform.example", "smtp_host": "127.0.0.1",
        "smtp_port": stack["smtp"].server_address[1], "smtp_use_tls": False, "smtp_timeout": 5})
    notice.step(datetime.now(UTC), deliver)
    not_now = re.search(r"Not now: (\S+)", _text(stack["smtp"].messages[0])).group(1)
    assert _open(not_now, "POST") == 200
    _poll(world)
    assert swap.current_version(stack["root"]) == "1.0.0"
    assert notice.observe(_all_results(world))["decision"] == "deny"


def test_a_later_rung_signs_a_fresh_request_once_the_last_has_expired(world, stack):
    posted: list[dict] = []
    esc = Escalation.open(stack["tmp"] / "notice.json", site=SITE, node="daq-pc-1", version="1.1.0",
                          contacts=[LEAD], operator="operator@platform.example", notes="- x", link="")
    notice = UpdateNotice(esc, keypair=world["operator"], key_id="op-1", operator_principal="@ops:org",
                          edge_url=world["url"], post=posted.append)
    deliver = herald_deliverer(email_config={
        "from_address": "updates@platform.example", "smtp_host": "127.0.0.1",
        "smtp_port": stack["smtp"].server_address[1], "smtp_use_tls": False, "smtp_timeout": 5})
    now = datetime.now(UTC)
    notice.step(now, deliver)
    notice.step(now + timedelta(days=7, hours=1), deliver)
    assert len(posted) == 2 and posted[0]["request"]["id"] != posted[1]["request"]["id"]
    links = [re.search(r"Approve: (\S+)", _text(x)).group(1) for x in stack["smtp"].messages]
    assert len(links) == 2 and links[0] != links[1]
    second = m.read_link_token(links[1].rsplit("/", 1)[1])["approval"]
    assert second["request_id"] == posted[1]["request"]["id"]
