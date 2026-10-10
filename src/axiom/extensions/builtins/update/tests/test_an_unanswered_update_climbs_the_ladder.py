# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An update nobody approves is asked about again, louder, until the operator calls the site.

A month runs in a second: the clock is passed in. Delivery goes through
HERALD's own email and chat adapters to a real SMTP server and a real HTTP
endpoint on this machine, and the SMTP server refuses a departed address with
550 the way a real one does.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.update.escalation import Escalation, herald_deliverer

DEPARTED = "left@site.example"


class _SMTP(socketserver.StreamRequestHandler):
    """Enough SMTP for smtplib.sendmail: refuses one recipient with 550."""

    def handle(self):
        srv = self.server
        w = lambda s: self.wfile.write((s + "\r\n").encode())  # noqa: E731
        w("220 test ESMTP")
        rcpts, in_data, data = [], False, []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\r\n")
            if in_data:
                if text == ".":
                    in_data = False
                    srv.messages.append({"to": list(rcpts), "data": "\n".join(data)})
                    rcpts, data = [], []
                    w("250 queued")
                else:
                    data.append(text)
                continue
            cmd = text.upper()
            if cmd.startswith(("EHLO", "HELO")):
                w("250 test")
            elif cmd.startswith("MAIL FROM"):
                w("250 ok")
            elif cmd.startswith("RCPT TO"):
                addr = text.split(":", 1)[1].strip(" <>")
                if addr == DEPARTED:
                    w("550 5.1.1 user unknown")
                else:
                    rcpts.append(addr)
                    w("250 ok")
            elif cmd == "DATA":
                in_data = True
                w("354 go")
            elif cmd == "QUIT":
                w("221 bye")
                return
            else:
                w("250 ok")


class _Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        self.server.posts.append(json.loads(self.rfile.read(n) or b"{}"))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


@pytest.fixture
def servers():
    smtp = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTP)
    smtp.messages = []
    hook = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
    hook.posts = []
    for s in (smtp, hook):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield smtp, hook
    smtp.shutdown()
    hook.shutdown()


def _deliver(smtp, hook):
    return herald_deliverer(
        email_config={"from_address": "updates@platform.example", "smtp_host": "127.0.0.1",
                      "smtp_port": smtp.server_address[1], "smtp_use_tls": False, "smtp_timeout": 5},
        chat_config={"webhook_url": f"http://127.0.0.1:{hook.server_address[1]}/hook"},
    )


T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def _open(tmp_path, contacts, chat="#daq"):
    return Escalation.open(
        tmp_path / "pending.json", site="site-a", node="daq-pc-1", version="1.18.0",
        contacts=contacts, operator="operator@platform.example",
        notes="- faster archive reads\n- a fix for late timestamps", link="https://intake.example/approve/abc",
        chat=chat, now=T0,
    )


def _text(message: dict) -> str:
    import email

    msg = email.message_from_string(message["data"])
    parts = msg.walk() if msg.is_multipart() else [msg]
    return "\n".join(
        p.get_payload(decode=True).decode(errors="replace")
        for p in parts if p.get_content_type() == "text/plain"
    )


def _addressed(smtp):
    return [m["to"] for m in smtp.messages]


def test_a_month_of_silence_climbs_every_rung_once(tmp_path, servers):
    smtp, hook = servers
    deliver = _deliver(smtp, hook)
    e = _open(tmp_path, ["lead@site.example"])
    for day in range(0, 31):
        Escalation.load(e.path).step(T0 + timedelta(days=day, hours=1), deliver)
    to = _addressed(smtp)
    assert to == [["lead@site.example"], ["lead@site.example"], ["operator@platform.example"]]
    assert len(hook.posts) == 1  # the chat reminder, once
    assert "What changed" in _text(smtp.messages[0]) and "approve/abc" in _text(smtp.messages[0])
    assert "contact the site" in _text(smtp.messages[-1]).lower()
    hb = Escalation.load(e.path).heartbeat_fields(T0 + timedelta(days=30))
    assert hb["update_waiting"]["flag"] is True and hb["update_waiting"]["days"] == 30.0


def test_the_rungs_come_at_their_days(tmp_path, servers):
    smtp, hook = servers
    deliver = _deliver(smtp, hook)
    e = _open(tmp_path, ["lead@site.example"])
    e.step(T0, deliver)
    assert len(smtp.messages) == 1
    e.step(T0 + timedelta(days=6), deliver)
    assert len(smtp.messages) == 1 and not hook.posts
    e.step(T0 + timedelta(days=7), deliver)
    assert len(smtp.messages) == 2
    e.step(T0 + timedelta(days=13), deliver)
    assert len(hook.posts) == 1 and e.heartbeat_fields(T0 + timedelta(days=13))["update_waiting"]["flag"] is False
    e.step(T0 + timedelta(days=14), deliver)
    assert e.heartbeat_fields(T0 + timedelta(days=14))["update_waiting"]["flag"] is True
    e.step(T0 + timedelta(days=20), deliver)
    assert ["operator@platform.example"] not in _addressed(smtp)


def test_an_answer_stops_the_ladder(tmp_path, servers):
    smtp, hook = servers
    deliver = _deliver(smtp, hook)
    e = _open(tmp_path, ["lead@site.example"])
    e.step(T0, deliver)
    e.answer("approve", by="lead@site.example", now=T0 + timedelta(days=2))
    for day in range(3, 40):
        e.step(T0 + timedelta(days=day), deliver)
    assert len(smtp.messages) == 1 and not hook.posts
    assert e.heartbeat_fields()["update_waiting"] is None


def test_a_departed_contact_is_dropped_and_the_others_still_asked(tmp_path, servers):
    smtp, hook = servers
    deliver = _deliver(smtp, hook)
    e = _open(tmp_path, [DEPARTED, "lead@site.example"])
    events = e.step(T0, deliver)
    assert any(ev["event"] == "departed" and ev["contact"] == DEPARTED for ev in events)
    e.step(T0 + timedelta(days=7), deliver)
    assert all(DEPARTED not in to for to in _addressed(smtp))
    assert _addressed(smtp) == [["lead@site.example"], ["lead@site.example"]]


def test_when_every_contact_has_left_the_operator_is_told_at_once(tmp_path, servers):
    smtp, hook = servers
    deliver = _deliver(smtp, hook)
    e = _open(tmp_path, [DEPARTED])
    e.step(T0, deliver)
    assert _addressed(smtp) == [["operator@platform.example"]]
    assert "departed: left@site.example" in _text(smtp.messages[0])
    assert e.heartbeat_fields(T0)["update_waiting"]["contacts_reachable"] == 0
    assert e.heartbeat_fields(T0)["update_waiting"]["flag"] is True
    for day in range(1, 40):
        e.step(T0 + timedelta(days=day), deliver)
    assert _addressed(smtp) == [["operator@platform.example"]]  # told once, not every day


def test_a_site_without_chat_skips_that_rung_and_says_so(tmp_path, servers):
    smtp, hook = servers
    e = _open(tmp_path, ["lead@site.example"], chat=None)
    events = []
    for day in range(0, 12):
        events += e.step(T0 + timedelta(days=day), _deliver(smtp, hook))
    assert not hook.posts
    assert any(ev["event"] == "skipped" and ev["rung"] == "chat" for ev in events)


def test_the_ladder_survives_a_restart(tmp_path, servers):
    smtp, hook = servers
    deliver = _deliver(smtp, hook)
    e = _open(tmp_path, ["lead@site.example"])
    e.step(T0, deliver)
    again = Escalation.load(e.path)
    again.step(T0 + timedelta(hours=5), deliver)
    assert len(smtp.messages) == 1
