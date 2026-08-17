#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Generate the Slack app manifest + the one-click 'create from manifest' link
(or create the app programmatically if you pass a config token).

Usage:
  python poc/make_slack_app.py --channel ops-sysadmin
  python poc/make_slack_app.py --channel ops-sysadmin --config-token xoxe-...
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Use THIS worktree's code, not the installed anchor.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from axiom.extensions.builtins.connect.skills.slack_install import (  # noqa: E402
    build_app_manifest,
    slack_install,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True, help="target channel name, e.g. ops-sysadmin")
    ap.add_argument("--app-name", default=None)
    ap.add_argument("--config-token", default=os.environ.get("SLACK_CONFIG_TOKEN"))
    ap.add_argument("--secret-ref", default="env://SLACK_BOT_TOKEN")
    args = ap.parse_args()

    res = slack_install(
        {
            "channel": args.channel,
            "owner": "@ben",
            "secret_ref": args.secret_ref,
            "app_name": args.app_name,
            "config_token": args.config_token,
        },
        ctx=None,
    )
    if not res.ok:
        print("ERROR:", "; ".join(res.errors), file=sys.stderr)
        return 1

    v = res.value
    print("\n=== Slack app manifest ===")
    print(json.dumps(v["manifest"], indent=2))
    if "app_id" in v:
        print(f"\n✅ Created app {v['app_id']} via apps.manifest.create.")
        print(f"➡  Install to your workspace (one click):\n   {v['install_url']}")
    else:
        print("\n➡  Create the app in 2 clicks — open this, review, Create:")
        print(f"   {v['create_url']}")
        print("\n   (Or set SLACK_CONFIG_TOKEN / --config-token for zero-click creation.)")
    print(f"\nThen: OAuth → Install to Workspace, copy the Bot token (xoxb-) and App-level token (xapp-),")
    print(f"and invite the bot to #{args.channel}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
