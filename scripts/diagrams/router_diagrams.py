#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Render the routing diagrams (spec-model-routing 2 and 15.2, router build plan).

Standard flowchart symbols: terminator for start and end, rectangle for a process, diamond for a
decision, parallelogram for input, wavy-bottom rectangle for a record, cylinder for stored data.
Writes four PNGs into docs/assets. Run from the repo root:

    python scripts/diagrams/router_diagrams.py
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse, FancyArrowPatch, FancyBboxPatch, PathPatch, Polygon, Rectangle
from matplotlib.path import Path

INK = "#1b2a41"
MUT = "#5a6b84"
KIND = {
    "input":   ("#dbe7ff", "#2b4a8b"),
    "verdict": ("#fbe3e3", "#8f1d1d"),
    "label":   ("#fde5d4", "#8a4a1a"),
    "seat":    ("#d9f2e0", "#1c6b3a"),
    "plain":   ("#e4f2f7", "#0f5c71"),
    "side":    ("#eceff3", "#5a6b84"),
    "surface": ("#e6e0f5", "#4a3a8b"),
    "record":  ("#f1f1f1", "#444444"),
}
ASSETS = pathlib.Path(__file__).resolve().parents[2] / "docs" / "assets"


@dataclass
class Box:
    cx: float
    cy: float
    w: float
    h: float

    @property
    def top(self):
        return (self.cx, self.cy + self.h / 2)

    @property
    def bottom(self):
        return (self.cx, self.cy - self.h / 2)

    @property
    def left(self):
        return (self.cx - self.w / 2, self.cy)

    @property
    def right(self):
        return (self.cx + self.w / 2, self.cy)


def _draw_shape(ax, shape, cx, cy, w, h, fill, edge, dashed, ls=None):
    ls = ls or ("--" if dashed else "-")
    kw = dict(linewidth=1.8, edgecolor=edge, facecolor=fill, linestyle=ls, zorder=2)
    l, r, b, t = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
    if shape == "process":
        ax.add_patch(Rectangle((l, b), w, h, **kw))
    elif shape == "terminator":
        ax.add_patch(FancyBboxPatch((l + h / 2, b), w - h, h,
                                    boxstyle=f"round,pad=0,rounding_size={h / 2}", **kw))
    elif shape == "state":
        ax.add_patch(FancyBboxPatch((l + 0.01, b), w - 0.02, h, boxstyle="round,pad=0.01,rounding_size=0.03", **kw))
    elif shape == "decision":
        ax.add_patch(Polygon([(l, cy), (cx, t), (r, cy), (cx, b)], closed=True, **kw))
    elif shape == "input":
        k = 0.035
        ax.add_patch(Polygon([(l + k, t), (r, t), (r - k, b), (l, b)], closed=True, **kw))
    elif shape == "document":
        wv = h * 0.14
        verts = [(l, t), (r, t), (r, b + wv), (r - w * 0.25, b - wv * 0.2), (cx + w * 0.05, b - wv * 0.2),
                 (cx, b + wv * 0.1), (cx - w * 0.2, b + wv * 1.4), (l + w * 0.2, b + wv * 1.4),
                 (l, b + wv), (l, t)]
        codes = [Path.MOVETO, Path.LINETO, Path.LINETO, Path.CURVE4, Path.CURVE4, Path.CURVE4,
                 Path.CURVE4, Path.CURVE4, Path.CURVE4, Path.CLOSEPOLY]
        ax.add_patch(PathPatch(Path(verts, codes), **kw))
    elif shape == "store":
        eh = h * 0.22
        ax.add_patch(Ellipse((cx, b + eh / 2), w, eh, **kw))
        ax.add_patch(Rectangle((l, b + eh / 2), w, h - eh, facecolor=fill, edgecolor="none", zorder=2))
        ax.plot([l, l], [b + eh / 2, t - eh / 2], color=edge, linewidth=1.8, linestyle=ls, zorder=3)
        ax.plot([r, r], [b + eh / 2, t - eh / 2], color=edge, linewidth=1.8, linestyle=ls, zorder=3)
        ax.add_patch(Ellipse((cx, t - eh / 2), w, eh, **kw))
    else:
        raise ValueError(shape)


def node(ax, shape, cx, cy, w, h, kind, title, lines=(), dashed=False, tsize=11.5, lsize=9.4, ls=None):
    fill, edge = KIND[kind]
    _draw_shape(ax, shape, cx, cy, w, h, fill, edge, dashed, ls)
    parts = title.split("\n") if title else []
    n = len(lines)
    ty = cy + (0.013 * n if n else 0) + 0.0135 * (len(parts) - 1) + (0.008 if shape == "document" else 0) - (0.02 if shape == "store" else 0)
    for i, part in enumerate(parts):
        ax.text(cx, ty - i * 0.027, part, ha="center", va="center", fontsize=tsize, fontweight="bold",
                color=INK, zorder=4)
    base = ty - 0.027 * (len(parts) - 1)
    for i, ln in enumerate(lines):
        ax.text(cx, base - 0.034 - i * 0.026, ln, ha="center", va="center", fontsize=lsize,
                color=INK, zorder=4)
    return Box(cx, cy, w, h)


def polyline(ax, pts, label="", lpos=None, dashed=False):
    xs, ys = zip(*pts[:-1])
    ax.plot(xs + (pts[-1][0],), ys + (pts[-1][1],), color=INK if not dashed else MUT, linewidth=1.8,
            linestyle="--" if dashed else "-", zorder=1, solid_capstyle="butt")
    arrow(ax, pts[-2], pts[-1], label if False else "", None, dashed)
    if label:
        ax.text(*lpos, label, ha="center", va="center", fontsize=9, color=MUT, zorder=5,
                bbox=dict(boxstyle="round,pad=0.16", facecolor="white", edgecolor="none", alpha=0.95))


def legend(ax, y, items):
    n = len(items)
    step = 0.15
    x0 = 0.5 - (n - 1) * step / 2
    for i, (shape, label) in enumerate(items):
        node(ax, shape, x0 + i * step, y, 0.10, 0.035, "side", "", tsize=8)
        ax.text(x0 + i * step, y - 0.036, label, ha="center", va="center", fontsize=8.5, color=MUT)


def arrow(ax, p1, p2, label="", lpos=None, dashed=False, rad=0.0):
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle="-|>", mutation_scale=15, linewidth=1.8,
                                 color=INK if not dashed else MUT, zorder=1,
                                 linestyle="--" if dashed else "-",
                                 connectionstyle=f"arc3,rad={rad}"))
    if label:
        x, y = lpos if lpos else ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
        ax.text(x, y, label, ha="center", va="center", fontsize=9, color=MUT, zorder=4,
                bbox=dict(boxstyle="round,pad=0.16", facecolor="white", edgecolor="none",
                          alpha=0.95))


def canvas(w, h, title, ylo=0.0, yhi=1.0):
    fig, ax = plt.subplots(figsize=(w, h))
    ax.set_xlim(0, 1)
    ax.set_ylim(ylo, yhi)
    ax.axis("off")
    ax.text(0.5, yhi - 0.015, title, ha="center", va="top", fontsize=14.5, fontweight="bold", color=INK)
    return fig, ax


def save(fig, name):
    ASSETS.mkdir(parents=True, exist_ok=True)
    path = ASSETS / name
    fig.savefig(path, dpi=144, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"wrote {path.relative_to(ASSETS.parents[1])}")


def route_flow():
    fig, ax = canvas(9.8, 11.0, "A route is a verdict, then seats", ylo=-0.07)
    X, W, H = 0.30, 0.46, 0.075
    req = node(ax, "input", X, 0.91, W, H, "input", "Request", ["text, principal, surface, session"])
    ctx = node(ax, "process", X, 0.80, W, H, "input", "Choose context sources", ["corpora, memory, files"])
    lab = node(ax, "process", X, 0.69, W, H, "label", "Join labels", ["strictest label across text and sources"])
    ver = node(ax, "decision", X, 0.555, W, 0.15, "verdict", "GUARD verdict:\nany option permitted?")
    fit = node(ax, "process", X, 0.42, W, H, "seat", "Fit seats", ["request intent, model class, corpus relevance"])
    opt = node(ax, "process", X, 0.31, W, H, "plain", "Optimize", ["cost, latency, health, stickiness"])
    asm = node(ax, "process", X, 0.20, W, H, "plain", "Assemble the route", ["models, corpora, tools, augmenters, verifier"])
    end = node(ax, "terminator", X, 0.09, W, 0.065, "surface", "Execute the route")
    rec = node(ax, "document", 0.80, 0.555, 0.34, 0.105, "record", "Decision record", ["content-free (C5)", "options removed, with reasons"])
    prov = node(ax, "store", 0.80, 0.42, 0.34, 0.11, "side", "Providers per seat", ["rules, local models, services", "advice until the seat is active"], dashed=True)
    cards = node(ax, "store", 0.80, 0.285, 0.34, 0.105, "side", "Capability cards", ["declared, measured"], dashed=True)
    arrow(ax, req.bottom, ctx.top)
    arrow(ax, ctx.bottom, lab.top)
    arrow(ax, lab.bottom, ver.top)
    arrow(ax, ver.bottom, fit.top, "yes: permitted options", (X + 0.11, 0.485))
    arrow(ax, ver.right, rec.left, "no, or removed", (0.585, 0.603))
    arrow(ax, fit.bottom, opt.top)
    arrow(ax, opt.bottom, asm.top)
    arrow(ax, asm.bottom, end.top)
    arrow(ax, prov.left, fit.right, dashed=True)
    arrow(ax, cards.left, opt.right, dashed=True)
    legend(ax, -0.03, [("terminator", "start / end"), ("process", "process"), ("decision", "decision"),
                       ("input", "input"), ("document", "record"), ("store", "stored data")])
    save(fig, "routing-route-flow.png")


def execution_flow():
    fig, ax = canvas(10.6, 13.4, "What a route drives: retrieval, tool calls, answer", ylo=-0.075)
    X, W, H = 0.27, 0.40, 0.06
    start = node(ax, "terminator", X, 0.925, W, 0.05, "surface", "Assembled route")
    pre_r = node(ax, "process", X, 0.858, W, 0.055, "seat", "pre_retrieve", ["plan retrieval, pick corpora"])
    cor_ok = node(ax, "decision", X, 0.760, W, 0.09, "verdict", "Corpus permitted\nfor these labels?")
    ret = node(ax, "process", X, 0.665, W, H, "plain", "Retrieve chunks", ["hybrid search over the corpus"])
    post_r = node(ax, "process", X, 0.58, W, H, "seat", "post_retrieve", ["sanitize chunks, inherit labels"])
    pre_g = node(ax, "process", X, 0.495, W, H, "seat", "pre_generate", ["assemble prompt from context"])
    gen = node(ax, "process", X, 0.41, W, H, "plain", "Generate", ["model chosen by the route"])
    tool_q = node(ax, "decision", X, 0.31, W, 0.105, "plain", "Tool call\nrequested?")
    post_g = node(ax, "process", X, 0.205, W, H, "seat", "post_generate", ["verifier, provenance check"])
    pre_a = node(ax, "process", X, 0.12, W, H, "seat", "pre_answer", ["citations, disclosures"])
    end = node(ax, "terminator", X, 0.04, W, 0.05, "surface", "Answer to the surface")
    corp = node(ax, "store", 0.87, 0.665, 0.22, 0.085, "side", "Corpora", dashed=True)
    mem = node(ax, "store", 0.87, 0.495, 0.22, 0.085, "side", "Memory, files,", ["program state"], dashed=True)
    skip = node(ax, "process", 0.66, 0.765, 0.24, 0.07, "side", "Skip corpus", ["record the reason"])
    pre_t = node(ax, "decision", 0.62, 0.31, 0.20, 0.11, "verdict", "pre_tool:\npermitted?")
    refuse = node(ax, "process", 0.84, 0.31, 0.17, 0.07, "side", "Refuse call", ["tell the model why"])
    exe = node(ax, "process", 0.62, 0.195, 0.22, 0.06, "plain", "Execute tool", ["declared scopes only"])
    post_t = node(ax, "process", 0.62, 0.105, 0.22, 0.06, "seat", "post_tool", ["filter result, label it"])
    rec = node(ax, "document", 0.83, 0.03, 0.30, 0.07, "record", "Decision record", ["each guarded stage appends"])
    arrow(ax, start.bottom, pre_r.top)
    arrow(ax, pre_r.bottom, cor_ok.top)
    arrow(ax, cor_ok.bottom, ret.top, "yes", (X + 0.03, 0.715))
    arrow(ax, cor_ok.right, skip.left, "no", (0.505, 0.78))
    arrow(ax, ret.bottom, post_r.top)
    arrow(ax, (X + 0.20, 0.680), (0.76, 0.680), "query", (0.62, 0.70), dashed=True)
    arrow(ax, (0.76, 0.650), (X + 0.20, 0.650), "chunks", (0.62, 0.63), dashed=True)
    arrow(ax, post_r.bottom, pre_g.top)
    arrow(ax, (skip.cx - 0.12, 0.765 - 0.035 + 0.0), (X + 0.20, 0.51))
    arrow(ax, mem.left, (X + 0.20, 0.48), "augment", (0.60, 0.485), dashed=True)
    arrow(ax, pre_g.bottom, gen.top)
    arrow(ax, gen.bottom, tool_q.top)
    arrow(ax, tool_q.bottom, post_g.top, "no: final text", (X + 0.09, 0.255))
    arrow(ax, tool_q.right, pre_t.left, "yes", (0.515, 0.325))
    arrow(ax, pre_t.bottom, exe.top, "yes", (0.65, 0.255))
    arrow(ax, pre_t.right, refuse.left, "no", (0.745, 0.345))
    arrow(ax, exe.bottom, post_t.top)
    polyline(ax, [post_t.right, (0.96, 0.105), (0.96, 0.40), (X + 0.20, 0.40)],
             "result re-enters the context (bounded loop)", (0.70, 0.428))
    arrow(ax, refuse.top, (0.84, 0.40))
    arrow(ax, post_g.bottom, pre_a.top)
    arrow(ax, pre_a.bottom, end.top)
    arrow(ax, end.right, rec.left, "record closes", (0.51, 0.045), dashed=True)
    legend(ax, -0.045, [("terminator", "start / end"), ("process", "process"), ("decision", "decision"),
                        ("store", "stored data"), ("document", "record")])
    save(fig, "routing-execution-flow.png")


def build_plan_dependencies():
    fig, ax = canvas(12.4, 12.0, "Router releases: track A (the gate) and track B (the brain)", ylo=-0.01, yhi=1.07)
    W, H = 0.14, 0.08
    A = {
        "R0": (0.34, 0.955, "verdict", ["parity suite, site default label"]),
        "R1": (0.10, 0.84, "verdict", ["honest servers"]),
        "R2": (0.34, 0.84, "label", ["envelope, record"]),
        "R3": (0.10, 0.70, "input", ["sink, peer gate"]),
        "R4": (0.26, 0.70, "input", ["stages call GUARD"]),
        "R5": (0.42, 0.70, "seat", ["seats, conformance"]),
        "R6": (0.58, 0.70, "plain", ["token principals"]),
        "R7": (0.26, 0.56, "input", ["route stage, shadow"]),
        "R10": (0.10, 0.42, "surface", ["labels, assemblies"]),
        "R8": (0.42, 0.42, "seat", ["seats in shadow"]),
        "R9": (0.42, 0.28, "seat", ["graduation"]),
    }
    B = {
        "B1": (0.90, 0.84, "plain", ["need, cards, utility"]),
        "B3": (0.90, 0.70, "seat", ["topic, coverage"]),
        "B2": (0.735, 0.56, "surface", ["skill matrix, latency"]),
        "B4": (0.90, 0.56, "plain", ["live signals, re-solve"]),
        "B5": (0.735, 0.28, "surface", ["combinations"]),
        "B6": (0.90, 0.14, "surface", ["medallion, exploration"]),
    }
    n = {}
    for k, (x, y, kind, lines) in {**A, **B}.items():
        n[k] = node(ax, "process", x, y, W + (0.14 if k == "R0" else 0), H, kind, k, lines, tsize=11, lsize=8.4)
    for src, dst in [("R0", "R1"), ("R0", "R2"), ("R2", "R3"), ("R2", "R4"), ("R2", "R5"), ("R2", "R6"),
                     ("R3", "R7"), ("R4", "R7"), ("R5", "R7"), ("R7", "R8"), ("R5", "R8"), ("R7", "R10"),
                     ("R3", "R10"), ("R8", "R9"),
                     ("R0", "B1"), ("B1", "B3"), ("R5", "B3"), ("B1", "B4"), ("B1", "B2"), ("R5", "B2"),
                     ("B2", "B5"), ("B4", "B5"), ("R10", "B5"), ("B2", "B6"), ("R9", "B6")]:
        arrow(ax, n[src].bottom, n[dst].top)
    for y, label in [(0.955, "wave 0"), (0.84, "wave 1"), (0.70, "wave 2"), (0.56, "wave 3"), (0.42, "wave 4"), (0.28, "wave 5"), (0.14, "wave 6")]:
        ax.text(0.005, y, label, ha="left", va="center", fontsize=9, color=MUT)
    ax.plot([0.655, 0.655], [0.06, 0.90], color=MUT, linewidth=1, linestyle=":")
    ax.text(0.34, 0.03, "track A: the gate", ha="center", va="center", fontsize=10, color=MUT)
    ax.text(0.82, 0.03, "track B: the brain", ha="center", va="center", fontsize=10, color=MUT)
    save(fig, "routing-build-plan-dependencies.png")


def solver():
    fig, ax = canvas(11.0, 8.6, "The solve: need against capability, every request and every step", ylo=0.0, yhi=1.0)
    need = node(ax, "input", 0.17, 0.60, 0.30, 0.46, "input", "Need vector",
                ["intent and subcategory", "topic (tree)", "required capabilities", "quality floor",
                 "money and quota budget", "time budget (TTFT, total)", "network state", "surface, session",
                 "determinism"], tsize=12, lsize=9.2)
    cards = node(ax, "store", 0.83, 0.60, 0.30, 0.50, "side", "Capability cards",
                 ["declared and measured skill", "per topic and intent", "cost per unit", "latency curve here",
                  "context, modalities", "reasoning range", "placement: local, site,", "enclave, peer, cloud",
                  "payment class and quota", "health now"], tsize=12, lsize=9.2)
    verdict = node(ax, "decision", 0.50, 0.80, 0.26, 0.13, "verdict", "Verdict:\npermitted options", tsize=11)
    solve = node(ax, "process", 0.50, 0.55, 0.26, 0.20, "seat", "Solve",
                 ["filter hard needs and caps", "score by profile weights", "choose a combination"], tsize=12)
    live = node(ax, "process", 0.17, 0.22, 0.30, 0.12, "plain", "Live signals",
                ["health, reachability, VPN,", "budget burn: re-solve"], tsize=11, lsize=9)
    out = node(ax, "document", 0.50, 0.10, 0.34, 0.11, "record", "Assembly and score table", ["in the decision record"], tsize=11)
    arrow(ax, verdict.bottom, solve.top)
    arrow(ax, need.right, (solve.cx - 0.13, 0.58))
    arrow(ax, cards.left, (solve.cx + 0.13, 0.58))
    arrow(ax, live.right, (solve.cx - 0.10, solve.cy - 0.10), dashed=True)
    arrow(ax, solve.bottom, out.top, "chosen", (0.56, 0.30))
    graded = node(ax, "process", 0.83, 0.17, 0.26, 0.11, "label", "Graded outcomes",
                  ["regret per request; label rule,", "judge seats; medallion ingest"], tsize=11, lsize=9)
    arrow(ax, out.right, graded.left, dashed=True)
    arrow(ax, graded.top, cards.bottom, "measured values, weights versions", (0.83, 0.29), dashed=True)
    save(fig, "routing-solver.png")


def legacy_router():
    fig, ax = canvas(10.0, 11.4, "QueryRouter and the gateway", ylo=-0.07)
    LX, RX = 0.25, 0.79
    W = 0.40
    msg = node(ax, "terminator", LX, 0.93, W, 0.06, "input", "User message")
    ses = node(ax, "decision", LX, 0.815, W, 0.13, "plain", "Session override\nset?")
    win = node(ax, "process", LX, 0.69, W, 0.07, "plain", "Build window", ["message + last 5 user turns"])
    kw = node(ax, "decision", LX, 0.565, W, 0.13, "plain", "Keyword\nmatch?")
    sl = node(ax, "decision", LX, 0.40, W, 0.14, "plain", "Ollama SLM\navailable?")
    fb = node(ax, "process", LX, 0.265, W, 0.07, "side", "Fallback", ["public"])
    dec = node(ax, "process", RX, 0.62, 0.34, 0.50, "verdict", "Routing decision",
               ["public or export_controlled", "", "source: session,", "keyword or ollama"])
    pub = node(ax, "terminator", LX, 0.11, W, 0.06, "plain", "Public tier")
    vpn = node(ax, "decision", RX, 0.25, 0.34, 0.13, "plain", "VPN\nreachable?")
    prv = node(ax, "terminator", 0.66, 0.11, 0.27, 0.06, "plain", "Private endpoint")
    wrn = node(ax, "terminator", 0.90, 0.11, 0.14, 0.06, "side", "Warn")
    ex = RX - 0.17
    arrow(ax, msg.bottom, ses.top)
    arrow(ax, ses.bottom, win.top, "no: auto", (LX + 0.065, 0.745))
    arrow(ax, ses.right, (ex, 0.815), "yes", (0.50, 0.835))
    arrow(ax, win.bottom, kw.top)
    arrow(ax, kw.right, (ex, 0.565), "yes", (0.50, 0.585))
    arrow(ax, kw.bottom, sl.top, "no", (LX + 0.03, 0.482))
    arrow(ax, sl.right, (ex, 0.40), "yes / no answer", (0.50, 0.42))
    arrow(ax, sl.bottom, fb.top, "no: unavailable", (LX + 0.085, 0.33))
    arrow(ax, fb.bottom, pub.top)
    arrow(ax, (0.70, 0.37), pub.right, "public", (0.56, 0.20))
    arrow(ax, (RX, 0.37), vpn.top, "export_controlled", (RX + 0.12, 0.34))
    arrow(ax, vpn.bottom, prv.top, "yes", (0.71, 0.185))
    arrow(ax, vpn.bottom, wrn.top, "no", (0.87, 0.185))
    legend(ax, -0.03, [("terminator", "start / end"), ("process", "process"), ("decision", "decision")])
    save(fig, "routing-legacy-router.png")


def components():
    fig, ax = canvas(13.0, 10.6, "Routing components: what exists, what is new, and how they connect", ylo=-0.06, yhi=1.02)
    S, P, N = "-", (0, (6, 2, 1, 2)), "--"   # shipped, partial, new
    surf = node(ax, "process", 0.11, 0.84, 0.19, 0.20, "surface", "Surfaces",
                ["terminal chat", "MCP server in a harness", "web application", "embedded widget", "org-hosted ingress"], lsize=9, ls=S)
    stages = node(ax, "process", 0.52, 0.87, 0.50, 0.10, "input", "Enforcement stages (PEP)",
                  ["route, pre_retrieve, post_retrieve, pre_tool, post_tool,", "pre_generate, post_generate, pre_answer"], lsize=9, ls=P)
    pip = node(ax, "store", 0.11, 0.52, 0.19, 0.36, "side", "Information providers",
               ["stored labels", "control lists, agreements", "attested processor facts", "principal directory (tokens)", "site policy sets, default label"], lsize=9, ls=P)
    guard = node(ax, "decision", 0.38, 0.62, 0.24, 0.16, "verdict", "GUARD decide (PDP)\nremoves options", tsize=11, ls=S)
    solve = node(ax, "process", 0.66, 0.62, 0.22, 0.11, "plain", "The solve (strategy)", ["need vector against cards", "filter, score, choose"], lsize=9, ls=P)
    cards = node(ax, "store", 0.90, 0.60, 0.16, 0.14, "side", "Capability cards", ["gold: declared,", "measured, attested"], lsize=9, ls=N)
    medal = node(ax, "process", 0.90, 0.84, 0.16, 0.13, "label", "Medallion ingest", ["bronze, silver, gold", "sources, evals,", "peer receipts"], lsize=9, ls=P)
    seats = node(ax, "process", 0.68, 0.40, 0.22, 0.15, "seat", "Seats (D1)",
                 ["request-intent, topic,", "lane-choice, model-class,", "corpus-relevance, verifier-need,", "detector-advice, eval-judge"], lsize=8.8, ls=N)
    prov = node(ax, "store", 0.90, 0.40, 0.16, 0.15, "side", "Providers (D2)", ["rules, local models,", "in-enclave decision models;", "hosted on public seats only"], lsize=8.6, ls=N)
    gate = node(ax, "process", 0.38, 0.40, 0.22, 0.12, "seat", "Graduation gate (D5/D6)", ["human by default;", "external product behind the seam"], lsize=9, ls=N)
    rec = node(ax, "document", 0.38, 0.17, 0.28, 0.12, "record", "Decision record (C5)", ["routing_events + route fields", "content-free; revision stamped"], lsize=9, ls=P)
    graded = node(ax, "process", 0.72, 0.17, 0.22, 0.11, "label", "Graded outcomes", ["label rule, judge seats;", "regret per request"], lsize=9, ls=N)
    arrow(ax, surf.right, (stages.cx - 0.25, 0.86))
    arrow(ax, (0.44, stages.cy - 0.05), guard.top)
    arrow(ax, pip.right, guard.left)
    arrow(ax, guard.right, solve.left, "permitted options", (0.525, 0.585))
    arrow(ax, cards.left, solve.right, dashed=True)
    arrow(ax, medal.bottom, cards.top)
    arrow(ax, seats.top, solve.bottom, "advice until active", (0.66, 0.52), dashed=True)
    arrow(ax, prov.left, seats.right, dashed=True)
    arrow(ax, gate.right, seats.left, "standing per site", (0.53, 0.355))
    arrow(ax, guard.bottom, gate.top, "", None)
    arrow(ax, gate.bottom, rec.top)
    arrow(ax, (solve.cx - 0.09, solve.cy - 0.055), (rec.cx + 0.10, rec.cy + 0.06), "score table", (0.49, 0.27))
    arrow(ax, rec.right, graded.left, dashed=True)
    polyline(ax, [graded.right, (0.985, 0.17), (0.985, 0.84), (medal.cx + 0.08, 0.84)], "outcomes re-enter the ingest", (0.86, 0.26), dashed=True)
    arrow(ax, (0.72, stages.cy - 0.05), (0.66, solve.cy + 0.055))
    ax.text(0.50, -0.03, "border: solid = shipped, dash-dot = partly built, dashed = new   |   dashed arrows carry advice or data, solid arrows carry decisions",
            ha="center", va="center", fontsize=9, color=MUT)
    save(fig, "routing-components.png")


def seat_lifecycle():
    fig, ax = canvas(12.0, 6.6, "A seat's standing, per site: earned by evidence, lost on drift", ylo=0.0, yhi=1.0)
    W, H = 0.20, 0.16
    ax.add_patch(plt.Circle((0.06, 0.60), 0.018, color=INK, zorder=3))
    reg = node(ax, "state", 0.22, 0.60, W, H, "side", "Shadow", ["registered (D1); opinions", "recorded, nothing acted on"], lsize=9)
    adv = node(ax, "state", 0.52, 0.60, W, H, "seat", "Advisory", ["opinion shown and logged;", "a person or rule still decides"], lsize=9)
    act = node(ax, "state", 0.82, 0.60, W, H, "seat", "Active", ["the live decider for this seat", "at this site"], lsize=9)
    arrow(ax, (0.078, 0.60), reg.left, "register", (0.12, 0.64))
    arrow(ax, reg.right, adv.left, "evidence: receipt ok,\ncriterion met (gate)", (0.37, 0.70))
    arrow(ax, adv.right, act.left, "evidence over a window\n(gate; signed transition)", (0.67, 0.70))
    arrow(ax, (act.cx, act.cy - H / 2), (adv.cx, adv.cy - H / 2), "receipt drifted or stale:\nautomatic demotion", (0.67, 0.44), rad=-0.35)
    arrow(ax, (adv.cx, adv.cy - H / 2), (reg.cx, reg.cy - H / 2), "options widened, or\nowner demotes", (0.37, 0.44), rad=-0.35)
    rc = node(ax, "document", 0.30, 0.17, 0.34, 0.14, "record", "Calibration receipt (D3)",
              ["ok / noisy / stale / drifted; bins; label-rule version;", "missing, noisy, stale or drifted = no evidence"], lsize=9)
    gt = node(ax, "process", 0.74, 0.17, 0.30, 0.14, "plain", "Gate (D5, D6)", ["human by default; an external product", "behind the seam; gate version stamped"], lsize=9)
    arrow(ax, rc.top, (0.30, 0.52 - 0.0), dashed=True)
    arrow(ax, gt.top, (0.74, 0.52), dashed=True)
    ax.text(0.5, 0.92, "Transitions are evidence-gated, never time-gated; the same seat can be active at one site and shadow at another.",
            ha="center", va="center", fontsize=9.5, color=MUT)
    save(fig, "routing-seat-lifecycle.png")


def detector_placement():
    fig, ax = canvas(10.4, 12.0, "Where may a detector run? Decided at registration", ylo=-0.02, yhi=1.02)
    X = 0.36
    start = node(ax, "terminator", X, 0.935, 0.52, 0.055, "input", "Detector D offered for seat S at site X", tsize=10.5)
    q1 = node(ax, "decision", X, 0.81, 0.40, 0.12, "plain", "Does S read content\n(text, chunks, results)?")
    rec_only = node(ax, "process", 0.80, 0.81, 0.30, 0.08, "side", "Any placement", ["reads digests and labels only"], lsize=9)
    q2 = node(ax, "decision", X, 0.655, 0.40, 0.12, "plain", "Is the input's label known\n(stored or declared)?")
    known = node(ax, "process", 0.80, 0.655, 0.30, 0.08, "label", "Required label = joined label", ["(15.4)"], lsize=9)
    ceil = node(ax, "process", X, 0.52, 0.44, 0.08, "label", "Required label = site ceiling", ["site.default_label on a public-only site"], lsize=9)
    q3 = node(ax, "decision", X, 0.37, 0.44, 0.13, "verdict", "Does D's processor satisfy the\nregime's placement for that label?")
    refuse = node(ax, "terminator", 0.80, 0.37, 0.36, 0.075, "verdict", "Refuse at registration", ["D2.3; recorded with the reason"], lsize=9, tsize=10.5)
    reg = node(ax, "process", X, 0.215, 0.44, 0.09, "seat", "Register in shadow", ["closed options only; no free text;", "records carry digests, never input"], lsize=9)
    end = node(ax, "terminator", X, 0.07, 0.56, 0.07, "surface", "Per call: advice narrows, never refuses", ["reroute, drop a source, prefer local"], lsize=9, tsize=10.5)
    arrow(ax, start.bottom, q1.top)
    arrow(ax, q1.right, rec_only.left, "no", (0.60, 0.83))
    arrow(ax, q1.bottom, q2.top, "yes", (X + 0.03, 0.735))
    arrow(ax, q2.right, known.left, "yes", (0.60, 0.675))
    arrow(ax, q2.bottom, ceil.top, "no", (X + 0.03, 0.59))
    arrow(ax, known.bottom, (X + 0.20, q3.cy + 0.02), rad=0.0)
    arrow(ax, ceil.bottom, q3.top)
    arrow(ax, q3.right, refuse.left, "no", (0.615, 0.39))
    arrow(ax, q3.bottom, reg.top, "yes", (X + 0.03, 0.285))
    arrow(ax, reg.bottom, end.top)
    polyline(ax, [rec_only.right, (0.985, 0.81), (0.985, 0.215), (X + 0.22, 0.215)], "closed outputs still apply", (0.80, 0.235), dashed=True)
    ax.text(0.5, -0.0, "Export control's placement is in-enclave on the local network; a health or personal-data regime may admit an agreement-holder (ADR-201 decisions 14 and 16).",
            ha="center", va="center", fontsize=8.8, color=MUT)
    save(fig, "routing-detector-placement.png")


if __name__ == "__main__":
    route_flow()
    execution_flow()
    build_plan_dependencies()
    solver()
    components()
    seat_lifecycle()
    detector_placement()
    legacy_router()
