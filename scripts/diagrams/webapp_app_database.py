#!/usr/bin/env python3
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Render docs/assets/diagrams/webapp-app-database.png (spec-site-view, app-database addendum).

Where the web app's data comes from: gold publishes domain data into the app's
own relational database (a projection that behaves almost like a cache); the
app database also keeps the application's own state, which is not in gold.
Pages read the app database, or gold directly. Silver serves nobody. Run from
the repo root:

    python scripts/diagrams/webapp_app_database.py
"""
from __future__ import annotations

import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

INK = "#1b2a41"
MUT = "#5a6b84"
BAD = "#a91830"
KIND = {
    "tier": ("#f2f2ee", "#4a5057"),
    "gold": ("#fff1c9", "#8a6d00"),
    "app": ("#dbe7ff", "#2b4a8b"),
    "state": ("#e4f2f7", "#0f5c71"),
    "deny": ("#fbe9ec", BAD),
}

# id: (x, y, width, height, kind, title, lines)
NODES = {
    "silver": (0.02, 0.62, 0.17, 0.22, "deny", "silver",
               ["conform pipeline only", "serves nobody"]),
    "gold": (0.25, 0.62, 0.20, 0.22, "gold", "gold",
             ["domain data: the system's", "business; built to scale"]),
    "appdb": (0.56, 0.62, 0.21, 0.22, "app", "app database (OLTP)",
              ["projection of gold, fast", "+ the app's own state"]),
    "state": (0.535, 0.16, 0.26, 0.30, "state", "application state (not in gold)",
              ["settings, accounts, profiles", "preferences, drafts",
               "layouts, pins, chart slots", "notifications"]),
    "app": (0.85, 0.62, 0.14, 0.22, "tier", "web app",
            ["pages, chat, walls", "API, MCP"]),
}

EDGES = [
    ("silver", "gold", "builds", INK, "-"),
    ("gold", "appdb", "publishes", INK, "-"),
    ("state", "appdb", "kept in", INK, "-"),
    ("appdb", "app", "reads", INK, "-"),
]


def _box(ax, key):
    x, y, w, h, kind, title, lines = NODES[key]
    fill, edge = KIND[kind]
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.008,rounding_size=0.015",
                                fc=fill, ec=edge, lw=1.6))
    ax.text(x + w / 2, y + h - 0.045, title, ha="center", va="top", fontsize=11.5,
            fontweight="bold", color=edge)
    for i, line in enumerate(lines):
        ax.text(x + w / 2, y + h - 0.105 - i * 0.05, line, ha="center", va="top",
                fontsize=9.5, color=MUT)


def _side(key, side):
    x, y, w, h, *_ = NODES[key]
    return {"r": (x + w, y + h / 2), "l": (x, y + h / 2), "t": (x + w / 2, y + h),
            "b": (x + w / 2, y)}[side]


def main() -> None:
    fig, ax = plt.subplots(figsize=(12, 4.6), dpi=150)
    ax.set_xlim(0, 1.01)
    ax.set_ylim(0.05, 0.92)
    ax.axis("off")
    for key in NODES:
        _box(ax, key)
    route = {("silver", "gold"): ("r", "l"), ("gold", "appdb"): ("r", "l"),
             ("state", "appdb"): ("t", "b"), ("appdb", "app"): ("r", "l")}
    for a, b, label, color, _ in EDGES:
        p, q = _side(a, route[(a, b)][0]), _side(b, route[(a, b)][1])
        ax.add_patch(FancyArrowPatch(p, q, arrowstyle="-|>", mutation_scale=14, lw=1.4,
                                     color=color))
        mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
        dx = 0.035 if a == "state" else 0
        ax.text(mx + dx, my + (0 if a == "state" else 0.05), label, ha="center",
                va="center", fontsize=9, color=INK)
        if a == "gold":
            ax.text(mx, my - 0.05, "freshness,\ninvalidation", ha="center", va="top",
                    fontsize=8.5, color=MUT)
    # gold read directly (a bypass of the app database), dashed
    gx, gy = _side("gold", "t")
    ax.annotate("", xy=(0.92, 0.84), xytext=(gx, gy), arrowprops=dict(
        arrowstyle="-|>", ls="--", lw=1.2, color=MUT, connectionstyle="arc3,rad=-0.25"))
    ax.text(0.62, 0.90, "or read directly from gold (verbs, charts)", ha="center",
            fontsize=9, color=MUT)
    # silver never serves
    sx, sy = _side("silver", "b")
    ax.text(sx, sy - 0.06, "✕ never read by the app, directly or via the app database",
            ha="center", fontsize=9, color=BAD)
    ax.text(0.02, 0.08, "Domain data reaches people from gold, read directly or published "
            "into the app's own database; silver never serves.", fontsize=9.5, color=INK)
    out = pathlib.Path("docs/assets/diagrams/webapp-app-database.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    print(out)


if __name__ == "__main__":
    main()
