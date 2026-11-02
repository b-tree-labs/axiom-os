#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Render docs/assets/scidisplay-equivalence-flow.png (ADR-184: from an expression pair to a verdict).

Boxes are sized from their content so text cannot overflow. Run from the repo root:

    python scripts/diagrams/scidisplay_equivalence_flow.py
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
    "input":  ("#dbe7ff", "#2b4a8b"),
    "step":   ("#fff1c9", "#8a6d00"),
    "ok":     ("#d9f2e0", "#1c6b3a"),
    "no":     ("#fbe3e3", "#8f1d1d"),
    "maybe":  ("#ffe9d1", "#a14a00"),
    "note":   ("#e4f2f7", "#0f5c71"),
}
PAD_T, TITLE_H, LINE_H, PAD_B = 0.014, 0.034, 0.027, 0.014
GAP = 0.056


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
        ax.text(x + w / 2, cy, ln, ha="center", va="top", fontsize=9.6, color=INK, zorder=3)
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
    fig, ax = plt.subplots(figsize=(9.2, 11.0))
    ax.set_xlim(0, 1)
    ax.set_ylim(-0.06, 1.0)
    ax.axis("off")
    ax.text(0.5, 0.995, "From an expression pair to a verdict",
            ha="center", va="top", fontsize=15, fontweight="bold", color=INK)

    top = 0.95
    a = draw_box(ax, 0.04, 0.44, top, "input", "Candidate",
                 ["math written in prose, in any", "delimiter dialect (model or person)"])
    r = draw_box(ax, 0.52, 0.44, top, "input", "Reference + SymbolTable",
                 ["an expression or equation, and the", "field's notation, supplied as data"])
    arrow(ax, 0.26, a, 0.42, a - GAP + 0.006)
    arrow(ax, 0.74, r, 0.58, a - GAP + 0.006)

    top = a - GAP
    f = draw_box(ax, 0.10, 0.80, top, "step", "Format + lint",
                 ["recognize math spans (a dollar sign follows Pandoc's rule; an escaped one is literal)",
                  "report unbalanced delimiters and symbols missing from the table",
                  "keep a plain-text twin for text-level checks"])
    arrow(ax, 0.50, f, 0.50, f - GAP + 0.006)

    top = f - GAP
    p = draw_box(ax, 0.10, 0.80, top, "step", "Parse",
                 ["SymPy ANTLR backend, runtime pinned (extra [math]), in the sandbox",
                  "notation resolved through the SymbolTable, never guessed"])
    row = p - GAP
    arrow(ax, 0.30, p, 0.30, row + 0.006)
    ax.text(0.825, p - GAP / 2, "unavailable or unparseable", ha="right", va="center",
            fontsize=9, color=MUT, zorder=4)

    s = draw_box(ax, 0.04, 0.60, row, "step", "Symbolic, then numeric, then units",
                 ["difference simplifies to zero (equations: equal up to a factor)",
                  "seeded numeric spot-check, stated domain and tolerance",
                  "unit dimensions compared through the units module"])
    ind_top = row
    arrow(ax, 0.20, s, 0.16, s - GAP + 0.006, "same", lx=0.12, ly=s - GAP / 2)
    arrow(ax, 0.38, s, 0.46, s - GAP + 0.006, "differ", lx=0.495, ly=s - GAP / 2)
    arrow(ax, 0.58, s, 0.74, s - GAP + 0.006, "undecided", lx=0.70, ly=s - GAP / 2 + 0.030)

    v_top = s - GAP
    e = draw_box(ax, 0.02, 0.27, v_top, "ok", "equivalent", ["proof and receipt kept"])
    n = draw_box(ax, 0.34, 0.27, v_top, "no", "not_equivalent", ["the differing point kept"])
    i = draw_box(ax, 0.66, 0.32, v_top, "maybe", "indeterminate",
                 ["the reason is named", "never a pass, never a fail"])
    # the unparseable branch lands in the same indeterminate box: one continuous arrow from Parse
    arrow(ax, 0.84, p, 0.84, v_top + 0.006)

    note_top = min(e, n, i) - GAP
    nb = draw_box(ax, 0.10, 0.80, note_top, "note", "The consumer decides what each verdict means",
                  ["pass, fail, or hand an indeterminate to another grader and label it as such",
                   "a judge model may be that other grader; it is never silently the first one"])
    ax.set_ylim(nb - 0.03, 1.0)

    out = pathlib.Path(__file__).resolve().parents[2] / "docs" / "assets" / "scidisplay-equivalence-flow.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=220, bbox_inches="tight", facecolor="white")
    print(out)


if __name__ == "__main__":
    main()
