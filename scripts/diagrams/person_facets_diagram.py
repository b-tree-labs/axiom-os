# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Render the figure for ADR-247.

    python scripts/diagrams/person_facets_diagram.py

Writes docs/assets/diagrams/person-facets.png. The look lives in diagram_kit.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagram_kit import CORE, INK2, Diagram, configure, render  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "docs" / "assets" / "diagrams"
configure("Axiom")


def person_facets() -> Diagram:
    d = Diagram("person-facets", "Many verified facets, one person, one handle", 560)
    d.zone(36, 86, 290, 420, "Facets (what authenticated)", "halo")
    labels = ["Web sign-in\n(issuer + subject)", "API key\n(key id)", "CLI on a laptop\n(node owner)",
              "Second site's sign-in\n(another issuer)", "Verified email"]
    facets = [d.node(56, 118 + i * 76, 250, 58, t, size=12) for i, t in enumerate(labels)]
    person = d.node(400, 236, 210, 92, "Person record", "canonical handle,\nlinked facets, link log",
                    fill=CORE, size=14)
    for f in facets:
        d.arrow(f.r(), person.l(), via=[(352, f.r().y), (352, person.l().y)])
    d.text(338, 110, "resolve at the\nidentity boundary", 12, 400, INK2)
    d.zone(660, 86, 264, 420, "One handle everywhere", "blue")
    uses = ["Ownership and history", "Grants and clearances", "Audit: accountable human", "Federation (handle only)"]
    outs = [d.node(680, 126 + i * 90, 224, 58, t, size=12) for i, t in enumerate(uses)]
    for o in outs:
        d.arrow(person.r(), o.l(), via=[(640, person.r().y), (640, o.l().y)])
    return d


if __name__ == "__main__":
    render([person_facets()], OUT, set(sys.argv[1:]) or None)
