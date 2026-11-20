#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Render the placement figure for ADR-251: one record, labelled projections, one door.

    python scripts/diagrams/log_data_placement_diagram.py

Writes docs/assets/diagrams/log-data-placement.png. The look lives in diagram_kit.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagram_kit import Diagram, configure, render  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "assets" / "diagrams"
configure("Axiom")

X0, COL_W, GAP = 24, 186, 52
TOP, COL_H = 92, 372
INNER = COL_W - 28


def _col(i: int) -> int:
    return X0 + i * (COL_W + GAP)


def log_data_placement() -> Diagram:
    d = Diagram("log-data-placement",
                "One record, labelled projections of gold, one check at every door", 600)

    d.zone(_col(0), TOP, COL_W, COL_H, "Record of truth", "halo")
    d.zone(_col(1), TOP, COL_W, COL_H, "Gold, by time", "blue")
    d.zone(_col(2), TOP, COL_W, COL_H, "Labelled projections", "none")
    d.zone(_col(3), TOP, COL_W, COL_H, "One door", "core")

    chain = d.node(_col(0) + 14, TOP + 44, INNER, 70, "Attest chain",
                   "signed, kept for life", tone="core", size=13, sub_size=11)
    package = d.node(_col(0) + 14, TOP + 150, INNER, 86, "Sealed period package",
                     "export, chain, hashes\nkeys and anchors\nverified, then retired",
                     tone="plain", dash=True, size=13, sub_size=11)
    blobs = d.node(_col(0) + 14, TOP + 282, INNER, 70, "Blob store",
                   "attachments by hash\nnamed in signed bytes", tone="plain",
                   size=13, sub_size=11)

    gold = d.node(_col(1) + 14, TOP + 44, INNER, 110, "Gold view",
                  "one row per record\ndigest, labels per row\npartitioned by year\nfed by sequence",
                  tone="core", size=13, sub_size=11)

    proj = [
        d.node(_col(2) + 14, TOP + 40 + j * 80, INNER, 64, title, sub, size=12.5, sub_size=11)
        for j, (title, sub) in enumerate([
            ("Text index", "a column on the view"),
            ("Vector table", "record id, (model, dim)"),
            ("Journal", "references only"),
            ("Reference fragments", "pointer and summary"),
        ])
    ]

    check = d.node(_col(3) + 14, TOP + 44, INNER, 118, "One check",
                   "scope, labels, GUARD\nfilter before ranking\nevery read recorded",
                   tone="core", solid=True, size=13, sub_size=11)
    surfaces = d.node(_col(3) + 14, TOP + 196, INNER, 150, "Every surface",
                      "API, MCP, chat, search\nTimeline, export, charts\nmemory recall, restore\nfederation",
                      tone="plain", size=13, sub_size=11)

    d.arrow(chain.r(), gold.l(0.3))
    d.arrow(chain.b(), package.t(), "seal", dash=True)
    d.arrow(blobs.t(), package.b(), dash=True)
    for p in proj:
        d.arrow(gold.r(), p.l())
    for p in proj:
        d.arrow(p.r(), check.l())
    d.arrow(check.b(), surfaces.t(), "allow, or deny the same way")
    gold_cx = _col(1) + 14 + INNER / 2
    d.arrow(package.r(), gold.b(), "rehydrate", via=[(gold_cx, package.r().y)], at=0.8, dash=True)

    d.text(X0, 500, "Every projection is derived from gold and rebuildable; none holds the signed bytes. "
           "The Log page reads the chain", 12, 400, "#46453f", "start")
    d.text(X0, 520, "through the attest door; a blob is reachable only through a record that names it; "
           "the record is sealed, never compacted.", 12, 400, "#46453f", "start")
    return d


if __name__ == "__main__":
    render([log_data_placement()], OUT)
    print(OUT / "log-data-placement.png")
