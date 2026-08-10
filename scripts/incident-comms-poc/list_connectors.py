#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""How the installer/updater would render the connector catalog: every
connector, its availability (trickle-out), on/off state, and clickable
deep-linked vendor setup URLs. Same skills the CLI / UI / AXI call.

  python poc/list_connectors.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from axiom.extensions.builtins.connect.skills import registry_ops as reg  # noqa: E402

_MARK = {"available": "●", "planned": "○", "deprecated": "⊘"}


def main() -> int:
    cat = reg.list_connectors({}, ctx=None).value["entries"]
    print("\nAxiom connectors  (● available  ○ planned)\n")
    for e in sorted(cat, key=lambda x: (x["availability"] != "available", x["name"])):
        toggle = "[on] " if e["enabled"] else "[off]"
        print(f"  {_MARK.get(e['availability'], '?')} {toggle} {e['title']:<18} {e['name']}")
        s = e.get("setup")
        if s:
            print(f"        {s['summary']}")
            print(f"        needs: {', '.join(s['needs'])}")
            for label, url in s["urls"].items():
                print(f"        ↳ {label}: {url}")
        print()
    print("Switch on:  axi connect enable <name>     Set up:  axi connect setup <name>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
