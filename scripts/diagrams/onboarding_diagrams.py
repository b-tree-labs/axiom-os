#!/usr/bin/env python3
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Render the onboarding figures (docs/guides/part-3-build-a-feature.md).

    python scripts/diagrams/onboarding_diagrams.py

Writes docs/assets/diagrams/how-the-pieces-fit.png. The look lives in
diagram_kit.py; this file holds only what the figure says.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagram_kit import CORE, Diagram, configure, render  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "assets" / "diagrams"
configure("Axiom")


def pieces() -> Diagram:
    d = Diagram("how-the-pieces-fit", "How the pieces fit", 360)
    w, h = 176, 64
    ax = d.node(48, 110, w, h, "Axiom", "the platform", fill=CORE)
    ak = d.node(48, 240, w, h, "appkit", "the web UI")
    ds = d.node(392, 175, w, h, "A distribution", "a product for one domain")
    st = d.node(736, 175, w, h, "A site", "one organization's install")
    d.arrow(ax.r(), ds.l(.3), "used by", at=.5)
    d.arrow(ak.r(), ds.l(.7), "used by", at=.5)
    d.arrow(ds.r(), st.l(), "configured by", at=.5)
    return d


if __name__ == "__main__":
    render([pieces()], OUT, set(sys.argv[1:]) or None)
