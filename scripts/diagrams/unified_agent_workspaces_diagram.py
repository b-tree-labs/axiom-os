# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Render the "what runs where" figure for the unified agent workspaces review.

    python scripts/diagrams/unified_agent_workspaces_diagram.py

Writes docs/assets/diagrams/unified-agent-workspaces.png. The look lives in
diagram_kit.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagram_kit import Diagram, configure, render  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "assets" / "diagrams"
configure("Axiom")

COL_W, GAP, X0 = 226, 110, 24
TOP, COL_H = 96, 370
ROW_H, ROW_STEP = 50, 64


def _col(i: int) -> int:
    return X0 + i * (COL_W + GAP)


def unified_agent_workspaces() -> Diagram:
    d = Diagram("unified-agent-workspaces", "What runs where: seven nouns on four scales", 600)

    d.zone(_col(0), TOP, COL_W, COL_H, "A person's machine", "halo")
    d.zone(_col(1), TOP, COL_W, COL_H, "A node", "blue")
    d.zone(_col(2), TOP, COL_W, COL_H, "A workspace", "none")
    d.zone(X0, 486, 3 * COL_W + 2 * GAP, 96, "The organization", "none")

    def rows(col: int, items: list[tuple[str, str]], tone: str = "plain"):
        return [
            d.node(_col(col) + 14, TOP + 40 + j * ROW_STEP, COL_W - 28, ROW_H, title, sub,
                   tone=tone, size=12.5, sub_size=11)
            for j, (title, sub) in enumerate(items)
        ]

    person = rows(0, [
        ("Interactive harness", "chat and the command line"),
        ("Vault and standing grants", "the person's own keys"),
        ("Signs", "approve, consent, attest, revoke"),
    ])
    node = rows(1, [
        ("GUARD decides", "deny, ask, notify, allow"),
        ("KEEP mints grants", "and watches every expiry"),
        ("Workspace registry", "owner, expiry, executor"),
        ("Serving gate and relay", "memory out, support in"),
        ("Journal and chain ledger", "written before it runs"),
    ], tone="core")
    workspace = rows(2, [
        ("One run", "a headless harness"),
        ("Repository and tools", "a branch, read-only caches"),
        ("A grant that expires", "no vault, no key, no site"),
    ])

    org_w = 270
    org = [
        d.node(37 + i * 301, 518, org_w, 46, title, sub, size=12.5, sub_size=11)
        for i, (title, sub) in enumerate([
            ("Permissions", "an accountable person per actor"),
            ("Gold projections", "the Timeline, the digests"),
            ("Federation", "what crosses between nodes"),
        ])
    ]

    mid_left = _col(0) + COL_W + GAP / 2
    mid_right = _col(1) + COL_W + GAP / 2

    d.arrow(person[0].r(), node[0].l(), "delegate a run")
    d.arrow(node[0].l(0.9), person[2].r(), "ask, then sign",
            via=[(mid_left - 5, node[0].l(0.9).y), (mid_left - 5, person[2].r().y)])
    d.arrow(workspace[0].l(), node[0].r(), "artifacts,\ntranscript")
    d.arrow(node[1].r(), workspace[2].l(), "grant\nand brief",
            via=[(mid_right, node[1].r().y), (mid_right, workspace[2].l().y)])
    d.arrow(node[4].b(), org[1].t(), "journal to gold")
    return d


if __name__ == "__main__":
    render([unified_agent_workspaces()], OUT, set(sys.argv[1:]) or None)
