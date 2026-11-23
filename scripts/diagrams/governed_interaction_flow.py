#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Render docs/assets/governed-interaction-flow.png (the master spec's architecture diagram).

Boxes are sized FROM their content (title and line counts), so text cannot overflow. Diagrams in
these docs are committed PNGs with the generator kept in the repo. Run from the repo root:

    python scripts/diagrams/governed_interaction_flow.py
"""
from __future__ import annotations

import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

INK = "#1b2a41"
MUT = "#5a6b84"
C = {
    "actor": ("#dbe7ff", "#2b4a8b"),
    "pep":   ("#fff1c9", "#8a6d00"),
    "pdp":   ("#d9f2e0", "#1c6b3a"),
    "pip":   ("#eee7fd", "#4b2d8f"),
    "exec":  ("#fff1c9", "#8a6d00"),
    "rec":   ("#fbe3e3", "#8f1d1d"),
    "life":  ("#e4f2f7", "#0f5c71"),
    "grad":  ("#d9f2e0", "#1c6b3a"),
}

PAD_T, TITLE_H, LINE_H, PAD_B = 0.012, 0.030, 0.0235, 0.012
GAP = 0.052  # vertical space between stacked rows, where the arrows live


def box_h(nlines: int) -> float:
    return PAD_T + TITLE_H + nlines * LINE_H + PAD_B


def draw_box(ax, x, w, y_top, kind, title, lines):
    h = box_h(len(lines))
    y = y_top - h
    fill, edge = C[kind]
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.004,rounding_size=0.018",
                                linewidth=1.8, edgecolor=edge, facecolor=fill, zorder=2))
    cy = y_top - PAD_T - 0.004
    ax.text(x + w / 2, cy, title, ha="center", va="top", fontsize=12, fontweight="bold",
            color=INK, zorder=3)
    cy -= TITLE_H
    for ln in lines:
        ax.text(x + w / 2, cy, ln, ha="center", va="top", fontsize=9.6, color=INK, zorder=3)
        cy -= LINE_H
    return y  # bottom


def arrow(ax, x1, y1, x2, y2, label="", lx=None, ly=None):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=15,
                                 linewidth=1.8, color=INK, zorder=1))
    if label:
        ax.text(lx if lx is not None else (x1 + x2) / 2 + 0.012,
                ly if ly is not None else (y1 + y2) / 2, label, ha="left", va="center",
                fontsize=8.8, color=MUT, zorder=4,
                bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.95))


def main() -> None:
    fig, ax = plt.subplots(figsize=(9.2, 12.4))
    ax.set_xlim(0, 1)
    ax.set_ylim(-0.02, 1.0)
    ax.axis("off")

    ax.text(0.5, 0.995, "Governed interaction: one lifecycle for every request",
            ha="center", va="top", fontsize=16, fontweight="bold", color=INK)
    ax.text(0.5, 0.972, "a chat turn, a tool call, a data query, an ingest, a write, an export, a purge",
            ha="center", va="top", fontsize=10.5, color=MUT)

    top = 0.952
    b = draw_box(ax, 0.26, 0.48, top, "actor", "Actor",
                 ["person, agent, harness, or service",
                  "authority arrives as a delegation chain and only attenuates"])
    arrow(ax, 0.50, b, 0.50, b - GAP + 0.006)

    top = b - GAP
    b = draw_box(ax, 0.10, 0.80, top, "pep", "Enforcement points (PEPs): fixed, named stages",
                 ["route · pre_retrieve · post_retrieve · pre_tool · post_tool · pre_generate",
                  "post_generate · pre_answer · write · egress · ingest · purge",
                  "a stage that cannot discharge an obligation must deny"])
    row_top = b - GAP
    arrow(ax, 0.30, b, 0.28, row_top + 0.006, "consults", lx=0.175, ly=b - GAP / 2)
    arrow(ax, 0.70, b, 0.72, row_top + 0.006, "answers from", lx=0.725, ly=b - GAP / 2)

    pdp_b = draw_box(ax, 0.06, 0.43, row_top, "pdp", "GUARD decide( ): the one PDP",
                     ["typed Verdict; deny-overrides combining",
                      "an error is Indeterminate, and it denies",
                      "adds obligations (closed registry) and advice"])
    pip_b = draw_box(ax, 0.53, 0.41, row_top, "pip", "PIPs and policy sets",
                     ["detectors: lists decide, models advise",
                      "label sources · agreements · identity · cost",
                      "regimes as versioned policy sets"])
    mid_y = row_top - box_h(3) / 2
    arrow(ax, 0.49, mid_y, 0.53, mid_y, "attributes", lx=0.472, ly=mid_y - 0.028)

    row_top2 = pdp_b - GAP
    arrow(ax, 0.275, pdp_b, 0.275, row_top2 + 0.006, "verdict + obligations", lx=0.055, ly=pdp_b - GAP / 2)
    arrow(ax, 0.735, pip_b, 0.735, row_top2 + 0.006, "every decision, recorded", lx=0.755, ly=pip_b - GAP / 2)

    ex_b = draw_box(ax, 0.06, 0.43, row_top2, "exec", "Obligation executors",
                    ["redact · store-nothing · add disclosure",
                     "reshape or paginate · approval · rate limit",
                     "route to lane · verify output"])
    rec_b = draw_box(ax, 0.53, 0.41, row_top2, "rec", "Content-free decision records",
                     ["policy hash and provider versions",
                      "obligations and their discharge status",
                      "signed where the regime requires it"])
    mid_y2 = row_top2 - box_h(3) / 2
    arrow(ax, 0.49, mid_y2, 0.53, mid_y2, "discharge", lx=0.474, ly=mid_y2 - 0.028)

    top = min(ex_b, rec_b) - GAP
    arrow(ax, 0.50, min(ex_b, rec_b), 0.50, top + 0.006)
    b = draw_box(ax, 0.06, 0.88, top, "life", "Content lifecycle",
                 ["labels by detection, declaration, or inheritance · retention classes · legal holds",
                  "verified purge: select, plan, approve, execute, verify independently, signed receipt",
                  "residual copies stated honestly · planted canaries prove the machinery can fail"])

    top = b - GAP
    arrow(ax, 0.50, b, 0.50, top + 0.006)
    draw_box(ax, 0.06, 0.88, top, "grad", "Graduation: shadow, then advisory, then active",
             ["every new enforcement and every lane ships shadow-first and is promoted on evidence",
              "typed-decision engine opinions stay advice until their seat is graduated"])

    out = pathlib.Path(__file__).resolve().parents[2] / "docs" / "assets" / "governed-interaction-flow.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=220, bbox_inches="tight", facecolor="white")
    print(out)


if __name__ == "__main__":
    main()
