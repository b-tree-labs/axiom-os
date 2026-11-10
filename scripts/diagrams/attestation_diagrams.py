#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Render the attestation components figure (docs/specs/spec-attestation.md).

    python scripts/diagrams/attestation_diagrams.py

Writes docs/assets/diagrams/attestation-components.png. The look lives in
diagram_kit.py; this file holds only what the figure says.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagram_kit import CORE, INK2, Diagram, Pt, configure, render  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "assets" / "diagrams"
configure("Axiom")


def attestation() -> Diagram:
    d = Diagram("attestation-components", "How an attestation is recorded", 676)
    w, h = 124, 52
    x = [48 + i * 148 for i in range(6)]
    r1, r2, r3, r4, r5 = 118, 232, 328, 466, 578
    d.zone(36, 86, 140, 306, "Surfaces", "halo")
    d.zone(x[4] - 12, 86, 148, 96, "Platform", "none")
    d.zone(186, 200, 738, 192, "attest extension", "blue", label_at=300)
    d.zone(36, 436, 888, 96, "Platform", "none", label_at="tr")
    d.zone(36, 548, 888, 96, "Logbooks", "halo", label_at="tr")
    ui = d.node(x[0], r1, 116, h, "appkit logbook\nview", size=13)
    cli = d.node(x[0], r2, 116, h, "axi CLI", size=13, mono=True)
    vc = d.node(x[0], r3, 116, h, "Voice capture", size=13)
    dr = d.node(x[1], r2, w, h, "Drafts", size=13)
    cf = d.node(x[2], r2, w, h, "Confirmation", size=13)
    sg = d.node(x[3], r2, w, h, "Signing", fill=CORE, size=13)
    ch = d.node(x[4], r2, w, h, "Chain per\nsite + logbook", size=13)
    ob = d.node(x[5], r2, w, h, "Obligations", size=13)
    an = d.node(x[4], r3, w, h, "Anchors", size=13)
    bus = d.node(x[4], r1, w, h, "Bus + SSE", size=13)
    sp = d.node(x[0], r4, 116, h, "Site speech\nservice", size=13)
    wg = d.node(x[2], r4, w, h, "webgate grants", size=13)
    d.node(x[3], r4, w, h, "Key custody\nsign", size=13)
    rc = d.node(x[4], r4, w, h, "Receipts", size=13)
    chips = ["Approvals", "Case decisions", "Declarations", "Tenant logbooks"]
    for i, name in enumerate(chips):
        d.node(48 + i * 222, r5, 198, h, name, size=13)

    d.arrow(bus.l(.3), ui.r(.3), "live updates", at=.5)
    d.arrow(ui.r(.76), dr.t())
    d.arrow(cli.r(), dr.l())
    d.arrow(vc.b(), sp.t(), "audio", at_xy=(x[0] + 66, 418), anchor="start")
    d.arrow(sp.r(), dr.b(.3), "transcript", at=.8)
    d.arrow(dr.r(), cf.l())
    d.arrow(cf.r(), sg.l())
    d.arrow(sg.r(), ch.l())
    d.arrow(ch.t(), bus.b())
    d.text(x[4] + 70, 196, "signed", 12, 400, INK2)
    d.arrow(ob.t(), bus.r())
    d.text(x[5] + 70, 160, "due", 12, 400, INK2)
    d.arrow(wg.t(.5), sg.b(.3), mid=426)
    d.text(x[2] + 70, 416, "grant to sign", 12, 400, INK2)
    d.arrow(sg.b(.7), Pt(sg.b(.7).x, r4, "v"), "digest", at=.3)
    d.arrow(ch.b(), an.t())
    d.text(x[4] + 70, 312, "Merkle root", 12, 400, INK2)
    d.arrow(an.b(), rc.t(), "registered", at=.38)
    d.arrow(Pt(dr.b(.78).x, 548, "v"), dr.b(.78), "declare entries", at=.515)
    return d


if __name__ == "__main__":
    render([attestation()], OUT, set(sys.argv[1:]) or None)
