#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Render the figures for ADR-244, ADR-245 and ADR-246.

    python scripts/diagrams/infra_portability_diagrams.py

Writes docs/assets/diagrams/provisioning-layers.png, source-cutover-stages.png
and change-gate.png. The look lives in diagram_kit.py; this file holds only what
the figures say.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagram_kit import CORE, INK2, Diagram, Pt, configure, render  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "assets" / "diagrams"
configure("Axiom")


def provisioning_layers() -> Diagram:
    d = Diagram("provisioning-layers", "Only the provisioning layer differs by provider", 640)
    d.zone(36, 86, 888, 128, "Provisioning: one module per provider", "halo")
    xs = [52 + i * 218 for i in range(4)]
    names = ["Laptop / CI\nK3D", "On-premises\nnode", "Request-built\nVMs + RKE2", "GKE / EKS\nTerraform"]
    provs = [d.node(x, 120, 200, 70, n, size=13) for x, n in zip(xs, names)]
    inv = d.node(330, 250, 300, 56, "Standard inventory", fill=CORE, size=14)
    for p in provs:
        d.arrow(p.b(), inv.t(), via=[(p.b().x, 228), (480, 228)])
    d.zone(36, 330, 888, 196, "The same on every provider", "blue", label_at="tr")
    cb = d.node(60, 370, 260, 70, "axiom-cluster", "storage class, load balancer,\nsecrets store, S3 endpoint", mono=True, size=13)
    nc = d.node(350, 370, 260, 70, "axiom-node", "sizes, roles, Postgres,\nblue/green slots", mono=True, size=13)
    sv = d.node(640, 370, 260, 70, "One site values file", "the only per-site input", size=13)
    d.arrow(inv.b(.3), cb.t(), "fills", at=.5)
    d.arrow(cb.r(), nc.l())
    d.arrow(sv.l(), nc.r())
    ct = d.node(60, 462, 840, 44, "Contract test + zero-gap upgrade test: must pass on every provider before a deploy counts", size=13)
    d.arrow(nc.b(), Pt(nc.b().x, 462, "v"))
    fl = d.node(330, 560, 300, 52, "Flux pulls a release tag", size=13)
    d.arrow(Pt(480, 506, "v"), fl.t(), "deploy", at=.5)
    return d


def source_cutover() -> Diagram:
    d = Diagram("source-cutover-stages", "A source changes its delivery path in six stages", 420)
    names = ["1 Ready", "2 Shadow", "3 Overlap", "4 Switch", "5 Fallback", "6 Retire"]
    subs = ["host, network,\nkeys, channels", "test data to a\nseparate connector", "both paths,\nreconciled",
            "new path is\nsource of record", "old path kept,\nunread", "old connector\nretired; bytes kept"]
    w, gap = 132, 19
    boxes = []
    for i, (n, s) in enumerate(zip(names, subs)):
        boxes.append(d.node(40 + i * (w + gap), 120, w, 92, n, s, size=13, sub_size=11.5,
                            fill=CORE if i == 3 else None))
    for a, b in zip(boxes, boxes[1:]):
        d.arrow(a.r(), b.l())
    d.zone(36, 250, 888, 130, "If a stage fails", "halo")
    rb = d.node(320, 290, 320, 56, "Roll back to the old path", size=13)
    for bx in boxes[2:5]:
        d.arrow(bx.b(), rb.t(), via=[(bx.b().x, 266), (480, 266)], dash=True)
    d.text(60, 320, "Overlap must be able\nto fail: same schema,\nno reading twice", 12, 400, INK2)
    return d


def change_gate() -> Diagram:
    d = Diagram("change-gate", "Every infrastructure change passes one gate, then soaks", 470)
    checks = ["Delivery proof", "Installed code", "Backup restore", "Source health",
              "Feed freshness", "Front door", "Headroom", "Alert delivery"]
    d.zone(36, 86, 420, 340, "Checks, one command", "halo")
    cs = []
    for i, c in enumerate(checks):
        col, row = i % 2, i // 2
        cs.append(d.node(56 + col * 196, 120 + row * 74, 180, 52, c, size=13))
    g = d.node(500, 210, 150, 70, "Gate", "all pass", fill=CORE, size=14)
    d.arrow(Pt(456, g.l().y, "h"), g.l())
    s = d.node(700, 140, 200, 70, "Soak", "one day, 7 + 7 days,\nor 14 days by risk", size=13)
    rb = d.node(700, 290, 200, 70, "Roll back, or a\nnamed exception", size=13)
    d.arrow(g.r(.3), s.l(), "pass", at=.5)
    d.arrow(g.r(.7), rb.l(), "fail", at=.5)
    nx = d.node(700, 380, 200, 44, "Next change", size=13)
    d.arrow(s.r(.5), nx.r(.5), via=[(916, s.r().y), (916, nx.r().y)])
    return d


if __name__ == "__main__":
    render([provisioning_layers(), source_cutover(), change_gate()], OUT, set(sys.argv[1:]) or None)
