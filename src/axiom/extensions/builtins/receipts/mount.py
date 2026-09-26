# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The Receipts surface — serving skeleton (ADR-123 D2, spec-receipts-surface §3).

An appkit-shell application served the webgate way: the built Vite
bundle from ``webui/dist`` behind the gate (``requires_authz=True``),
brand injected as a bootstrap global before the bundle boots, assets
containment-checked. The SAME serving arrangement carries the NOS
webapp and product extractions — one UI infra, by direction.

Without a build present the mount serves an honest placeholder that
names the build step. It never fakes a surface: the program's first
rule is that nothing renders that the platform didn't actually do.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

WEBUI_DIR = Path(__file__).parent / "webui"
_DEFAULT_DIST = WEBUI_DIR / "dist"

BRAND_GLOBAL = "__AXIOM_BRAND__"


@dataclass(frozen=True)
class SurfaceBrand:
    """The tenant skin for a platform surface — structure is appkit's,
    values are the tenant's (same contract as the token canon)."""

    product_name: str = field(default_factory=lambda: os.environ.get("AXIOM_BRAND_NAME", "Axiom"))
    accent: str = field(default_factory=lambda: os.environ.get("AXIOM_BRAND_ACCENT", "#bf5700"))

    def payload(self) -> dict:
        return {"product_name": self.product_name, "accent": self.accent}


_PLACEHOLDER = """\
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Receipts</title>
<link rel="stylesheet" href="/_appkit/tokens.css">
<style>body{background:var(--ground,#14171a);color:var(--ink,#e8eaec);
font-family:var(--font-sans,system-ui);max-width:44rem;margin:15vh auto;padding:0 16px}
code{background:var(--panel,#1b1f23);padding:2px 6px;border-radius:3px}</style>
</head><body>
<h1>Receipts surface: not built</h1>
<p>The API is live at <code>/api/v1/fleet/status</code>, but this deploy has no
web bundle. Build it from the extension's <code>webui/</code> directory:</p>
<p><code>npm install &amp;&amp; npm run build</code></p>
<p>This page is deliberate: the surface never renders anything the platform
didn't do, and that includes pretending to be built.</p>
</body></html>"""


def build_receipts_router(*, spa_dist: Path | None = None, brand: SurfaceBrand | None = None):
    """Assemble the ``/receipts`` router.

    ``spa_dist`` defaults to the in-tree ``webui/dist``; passing a path is
    the test seam and the packaging seam both.
    """
    from axiom.extensions.builtins.http.spa import build_spa_router

    the_brand = brand if brand is not None else SurfaceBrand()
    dist = Path(spa_dist) if spa_dist is not None else _DEFAULT_DIST
    return build_spa_router(
        prefix="/receipts",
        dist=dist,
        bootstrap_global=BRAND_GLOBAL,
        bootstrap_payload=the_brand.payload(),
        placeholder_html=_PLACEHOLDER,
        tags=["receipts"],
    )


def mount_spec():
    """The gate-fronted ``/receipts`` mount (server profile)."""
    from axiom.extensions.builtins.http.registry import MountSpec

    return MountSpec(
        prefix="/receipts",
        router=build_receipts_router(),
        extension="receipts",
        requires_authz=True,
        profiles=("server",),
    )
