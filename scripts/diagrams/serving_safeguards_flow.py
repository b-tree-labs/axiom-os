#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Render docs/assets/serving-safeguards-flow.png (prd-gold-serving-safeguards architecture).

Boxes are sized from their content so text cannot overflow. Run from the repo root:

    python scripts/diagrams/serving_safeguards_flow.py
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
    "caller": ("#dbe7ff", "#2b4a8b"),
    "gate":   ("#dbe7ff", "#2b4a8b"),
    "layer":  ("#fff1c9", "#8a6d00"),
    "role":   ("#d9f2e0", "#1c6b3a"),
    "roll":   ("#d9f2e0", "#1c6b3a"),
    "deny":   ("#fbe3e3", "#8f1d1d"),
    "env":    ("#e4f2f7", "#0f5c71"),
}
PAD_T, TITLE_H, LINE_H, PAD_B = 0.014, 0.034, 0.027, 0.014
GAP = 0.058


def box_h(n: int) -> float:
    return PAD_T + TITLE_H + n * LINE_H + PAD_B


def draw_box(ax, x, w, y_top, kind, title, lines):
    h = box_h(len(lines))
    y = y_top - h
    fill, edge = C[kind]
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.004,rounding_size=0.016",
                                linewidth=1.8, edgecolor=edge, facecolor=fill, zorder=2))
    cy = y_top - PAD_T - 0.004
    ax.text(x + w / 2, cy, title, ha="center", va="top", fontsize=12, fontweight="bold", color=INK, zorder=3)
    cy -= TITLE_H
    for ln in lines:
        ax.text(x + w / 2, cy, ln, ha="center", va="top", fontsize=9.8, color=INK, zorder=3)
        cy -= LINE_H
    return y


def arrow(ax, x1, y1, x2, y2, label="", lx=None, ly=None):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=15,
                                 linewidth=1.8, color=INK, zorder=1))
    if label:
        ax.text(lx if lx is not None else (x1 + x2) / 2, ly if ly is not None else (y1 + y2) / 2,
                label, ha="center", va="center", fontsize=9, color=MUT, zorder=4,
                bbox=dict(boxstyle="round,pad=0.16", facecolor="white", edgecolor="none", alpha=0.95))


def main() -> None:
    fig, ax = plt.subplots(figsize=(8.8, 10.2))
    ax.set_xlim(0, 1)
    ax.set_ylim(-0.07, 1.0)
    ax.axis("off")
    ax.text(0.5, 0.995, "Gold serving safeguards: no single call can overrun the tier",
            ha="center", va="top", fontsize=15, fontweight="bold", color=INK)

    top = 0.955
    b = draw_box(ax, 0.22, 0.56, top, "caller", "Caller",
                 ["UI, CLI, MCP agent, or service", "each caller class has its own defaults and ceilings"])
    arrow(ax, 0.50, b, 0.50, b - GAP + 0.006)

    top = b - GAP
    b = draw_box(ax, 0.26, 0.48, top, "gate", "Gate: verified principal",
                 ["identity from the gate, never from a header", "quotas belong to the principal"])
    arrow(ax, 0.50, b, 0.50, b - GAP + 0.006)

    top = b - GAP
    b = draw_box(ax, 0.10, 0.80, top, "layer", "Serving layer",
                 ["the verb's declared cost class x the caller class -> default and ceiling",
                  "cost estimated BEFORE the query runs; per-principal quota and concurrency"])
    row_top = b - GAP
    arrow(ax, 0.30, b, 0.26, row_top + 0.006, "fits the budget", lx=0.165, ly=b - GAP / 2)
    arrow(ax, 0.70, b, 0.76, row_top + 0.006, "too large", lx=0.815, ly=b - GAP / 2)

    role_b = draw_box(ax, 0.06, 0.42, row_top, "role", "Serving database role",
                      ["statement, lock and idle timeouts", "read-only; connection and temp-file caps"])
    deny_b = draw_box(ax, 0.52, 0.45, row_top, "deny", "Reshape, paginate, or refuse",
                      ["even downsample across the WHOLE window", "never silent truncation to the oldest part",
                       "a refusal names the cheaper call"])
    arrow(ax, 0.27, role_b, 0.27, role_b - GAP + 0.006)

    top2 = role_b - GAP
    roll_b = draw_box(ax, 0.06, 0.42, top2, "roll", "Rollups first",
                      ["wide ranges answered from aggregates", "raw scans stay narrow by design"])

    bottom_top = min(roll_b, deny_b) - GAP
    arrow(ax, 0.27, roll_b, 0.40, bottom_top + 0.006)
    arrow(ax, 0.745, deny_b, 0.62, bottom_top + 0.006)
    draw_box(ax, 0.10, 0.80, bottom_top, "env", "Bounded response envelope",
             ["requested, returned, covered, resolution, reduced, cursor: stated FIRST",
              "reduced answers keep min, max, mean, count, faults and uncertainty"])

    out = pathlib.Path(__file__).resolve().parents[2] / "docs" / "assets" / "serving-safeguards-flow.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=220, bbox_inches="tight", facecolor="white")
    print(out)


if __name__ == "__main__":
    main()
