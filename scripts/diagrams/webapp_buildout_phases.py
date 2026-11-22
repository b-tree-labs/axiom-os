#!/usr/bin/env python3
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Render docs/assets/diagrams/webapp-buildout-phases.png (spec-webapp-buildout-roadmap).

The phase dependency graph of the web app build-out: P0 foundation through P7
assurance and reach, with the two work packages that moved earlier than their
phase (the device-class contract into P0, the signing card into P2's first
package). Boxes are sized from their content. Run from the repo root:

    python scripts/diagrams/webapp_buildout_phases.py
"""
from __future__ import annotations

import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

INK = "#1b2a41"
MUT = "#5a6b84"
KIND = {
    "found": ("#e4f2f7", "#0f5c71"),
    "mvp": ("#dbe7ff", "#2b4a8b"),
    "phase": ("#f2f2ee", "#4a5057"),
    "moved": ("#fff1c9", "#8a6d00"),
}

# id: (x, y, width, kind, title, lines)
NODES = {
    "P0": (0.01, 0.52, 0.17, "found", "P0 Foundation  (L)",
           ["shell, tokens, components", "flags, variants, CI smoke",
            "device-class contract *"]),
    "P1": (0.22, 0.52, 0.215, "mvp", "P1 MVP, node upgrade (XL)",
           ["search, chat, Chart, Library", "RAG, auth, settings, tenants",
            "ops guardrails, Needs you"]),
    "P2a": (0.48, 0.78, 0.18, "moved", "P2.0 Signing card *  (S)",
            ["one card, A · H · ?", "attest-backed approval"]),
    "P2": (0.72, 0.78, 0.215, "phase", "P2 Public site  (L)",
           ["published snapshots", "intake, delayed replay",
            "public canvas per site"]),
    "P3": (0.48, 0.52, 0.18, "phase", "P3 Log + signing  (L)",
           ["typed logs, supplements", "co-sign, seal, Audit mode"]),
    "P4": (0.72, 0.52, 0.215, "phase", "P4 Site view, devices (XL)",
           ["modes, systems, lineage", "live mode, walls, devices"]),
    "P5": (0.72, 0.24, 0.21, "phase", "P5 Frozen workflow (L)",
           ["product design, wholesale", "cross-links only"]),
    "P6": (0.48, 0.24, 0.17, "phase", "P6 Model  (L)",
           ["registry, notebooks", "improvement loop"]),
    "P7": (1.0, 0.52, 0.17, "phase", "P7 Assurance  (XL)",
           ["ConCert, AI audit", "voice, phone", "partner canvases"]),
}

# (from, to, label)
EDGES = [
    ("P0", "P1", ""),
    ("P1", "P2a", ""),
    ("P2a", "P2", "approvals"),
    ("P1", "P2", ""),
    ("P2a", "P3", "card"),
    ("P1", "P3", ""),
    ("P3", "P4", "logs"),
    ("P3", "P5", "entries"),
    ("P4", "P5", "kiosk, live"),
    ("P1", "P6", "Chart, Library"),
    ("P4", "P7", ""),
    ("P2", "P7", ""),
]

PAD_T, TITLE_H, LINE_H, PAD_B = 0.012, 0.032, 0.026, 0.012


def box_h(n: int) -> float:
    return PAD_T + TITLE_H + n * LINE_H + PAD_B


def anchor(node, side):
    x, y, w, _k, _t, lines = NODES[node]
    h = box_h(len(lines))
    return {
        "r": (x + w, y + h / 2),
        "l": (x, y + h / 2),
        "t": (x + w / 2, y + h),
        "b": (x + w / 2, y),
    }[side]


def pick_sides(a, b):
    ax_, ay, aw, *_ = NODES[a]
    bx, by, bw, *_ = NODES[b]
    if bx >= ax_ + aw - 0.01:
        return "r", "l"
    if by > ay + 0.05:
        return "t", "b"
    if bx + bw <= ax_ + 0.01:
        return "l", "r"
    return "b", "t"


def main() -> None:
    fig, ax = plt.subplots(figsize=(17, 8.2), dpi=110)
    ax.set_xlim(0, 1.18)
    ax.set_ylim(0, 1.06)
    ax.axis("off")
    ax.text(0.02, 1.02, "Web app build-out: phase dependencies", fontsize=16,
            fontweight="bold", color=INK, va="top")
    ax.text(0.02, 0.975,
            "* moved earlier than the owner's draft order (see the roadmap, "
            "\"Order changes\"). Sizes: S · M · L · XL.",
            fontsize=10, color=MUT, va="top")

    for nid, (x, y, w, kind, title, lines) in NODES.items():
        fill, edge = KIND[kind]
        h = box_h(len(lines))
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.004,rounding_size=0.01",
                                    fc=fill, ec=edge, lw=1.6))
        ax.text(x + 0.008, y + h - PAD_T, title, fontsize=10.5, fontweight="bold",
                color=edge, va="top")
        for i, line in enumerate(lines):
            ax.text(x + 0.008, y + h - PAD_T - TITLE_H - i * LINE_H, line,
                    fontsize=9.3, color=INK, va="top")

    for a, b, label in EDGES:
        sa, sb = pick_sides(a, b)
        p0, p1 = anchor(a, sa), anchor(b, sb)
        rad = 0.0 if sa in ("r", "l") and abs(p0[1] - p1[1]) < 0.02 else 0.12
        ax.add_patch(FancyArrowPatch(p0, p1, arrowstyle="-|>", mutation_scale=13,
                                     color=MUT, lw=1.2,
                                     connectionstyle=f"arc3,rad={rad}"))
        if label:
            mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
            ax.text(mx, my + 0.012, label, fontsize=8.5, color=MUT, ha="center",
                    bbox=dict(fc="white", ec="none", pad=0.6))

    ax.text(0.01, 0.12,
            "P2 and P3 run in parallel once P2.0 lands. P7 needs P3 (Audit mode) through P4.\n"
            "P6 depends only on P1 and may "
            "start beside P3–P5 when capacity allows (at most three workers at once).",
            fontsize=10, color=INK, va="top")

    out = pathlib.Path("docs/assets/diagrams/webapp-buildout-phases.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    print(out)


if __name__ == "__main__":
    main()
