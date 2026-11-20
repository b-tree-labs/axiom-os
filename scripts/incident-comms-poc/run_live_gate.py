#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""First live E2E over Slack: agent runs a task, you command it from the channel.

Env (scripts/incident-comms-poc/.env): SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_CHANNEL.
Run: source the .env, then `python run_live_gate.py`. Reply in the channel:
  status · stop · a number (measured value).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from axiom.extensions.builtins.notifications.channels.slack_interactive import (  # noqa: E402
    SlackInteractiveChannel,
)

from live_e2e import wire  # noqa: E402


def main() -> int:
    for var in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL"):
        if not os.environ.get(var):
            print(f"ERROR: {var} not set (source scripts/incident-comms-poc/.env)", file=sys.stderr)
            return 1
    channel = SlackInteractiveChannel(
        bot_token=os.environ["SLACK_BOT_TOKEN"],
        app_token=os.environ["SLACK_APP_TOKEN"],
        channel=os.environ["SLACK_CHANNEL"],
    )
    import live_e2e
    intro, gw = wire(channel)
    channel.post(intro, author=live_e2e.agent_name())
    prov = gw.providers[0].name if gw.providers else "?"
    print(f"Live agent presence on {os.environ['SLACK_CHANNEL']} via {prov} "
          f"(tool_mode={getattr(gw.providers[0],'tool_mode','?') if gw.providers else '?'}). "
          "Talk to it in Slack. Ctrl-C to stop.", flush=True)
    channel.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
