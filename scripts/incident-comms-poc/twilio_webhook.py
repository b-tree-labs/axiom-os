#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""SMS leg of the live E2E: the same agent control plane, over your phone.

Dependency-free webhook (stdlib http.server). Twilio POSTs inbound texts to
``/twilio/sms`` (form-encoded Body/From); we hand them to the InboundReceiver →
TwilioInteractiveChannel.dispatch. The agent texts you the prediction at startup.

Env: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM (Twilio number),
     TWILIO_TO (your mobile). Optional PORT (default 8000).
Run: source env, `python twilio_webhook.py`, expose it publicly (e.g.
     `cloudflared tunnel --url http://localhost:8000`) and point the Twilio
     number's inbound webhook at https://<tunnel>/twilio/sms.
"""
from __future__ import annotations

import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from axiom.extensions.builtins.notifications.channels.twilio_interactive import (  # noqa: E402
    TwilioInteractiveChannel,
)
from axiom.extensions.builtins.notifications.gateway.inbound import InboundReceiver  # noqa: E402

from live_e2e import agent_name, wire  # noqa: E402

_ROUTE = "/twilio/sms"


def _build():
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "TWILIO_TO"):
        if not os.environ.get(var):
            raise SystemExit(f"ERROR: {var} not set")
    channel = TwilioInteractiveChannel(
        account_sid=os.environ["TWILIO_ACCOUNT_SID"],
        auth_token=os.environ["TWILIO_AUTH_TOKEN"],
        from_number=os.environ["TWILIO_FROM"],
        to_number=os.environ["TWILIO_TO"],
    )
    intro, _gw = wire(channel)
    channel.post(intro, author=agent_name())  # text the intro now
    rx = InboundReceiver()
    rx.register(_ROUTE, channel)  # (add a Twilio-signature verifier here in prod)
    return rx


def main() -> int:
    rx = _build()
    port = int(os.environ.get("PORT", "8000"))

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            if self.path.split("?")[0] != _ROUTE:
                self.send_response(404); self.end_headers(); return
            n = int(self.headers.get("Content-Length", 0))
            form = urllib.parse.parse_qs(self.rfile.read(n).decode())
            payload = {k: v[0] for k, v in form.items()}  # Body, From, …
            rx.handle(_ROUTE, payload)
            self.send_response(204); self.end_headers()  # ack; replies go out-of-band

        def log_message(self, *a):  # quiet
            pass

    print(f"SMS webhook on :{port}{_ROUTE}. "
          "Expose publicly + set the Twilio inbound webhook. Ctrl-C to stop.", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
