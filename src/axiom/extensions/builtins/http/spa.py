# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared mechanics for serving a built web bundle from a mount.

Extracted from webgate (the shipped exemplar) the moment a second
surface (receipts) needed them — the generic-mechanics-trapped-in-one-
extension class. Every appkit-shell surface (platform consoles, the
NOS webapp, product extractions) serves the same way: a built Vite
``dist/`` whose ``index.html`` gets a bootstrap global injected before
the bundle boots, and whose assets are resolved with a containment
check so a ``..`` in the request can never escape the tree.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ASSET_CACHE_CONTROL",
    "INDEX_CACHE_CONTROL",
    "BundleAudit",
    "audit_bundle",
    "build_spa_router",
    "inject_bootstrap_global",
    "safe_asset_path",
]

#: The entry document names the current asset hashes, so it is the one
#: file a browser must never reuse without asking. Served bare (no
#: directives at all) a browser may cache it heuristically, and the next
#: build's stylesheet is then requested under last build's name — which
#: is how a fully working app arrives with no styling applied.
INDEX_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"

#: An asset's content hash IS its version: a change is a new URL, so the
#: answer at this URL can never change. Revalidating it is latency spent
#: on a question already answered.
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"


#: An asset is only counted where it appears as a PATH: ``assets/<name>``
#: (the index's src/href, and a chunk's preload list) or ``"./<name>"``
#: (how a chunk names a lazy sibling). Matching bare filenames instead
#: would read minified ``e.map(...)`` as a reference to a file called
#: ``e.map`` — the guard has to follow paths, not identifiers.
_ASSET_EXT = r"(?:js|css|woff2?|svg|png|jpg|jpeg|webp|avif|gif|ico|ttf|otf)"
_ASSET_REF = re.compile(rf"(?:assets/|[\"'`]\./)([A-Za-z0-9_.\-]+\.{_ASSET_EXT})(?=[\"'`)\s?#]|$)")


@dataclass(frozen=True)
class BundleAudit:
    """Whether a built ``dist/`` is internally consistent.

    ``missing`` — the index (or a chunk) names a file that is not there.
    The surface loads and 404s on it, which for a stylesheet is exactly
    "the app has no styling applied".

    ``orphaned`` — a file nothing reaches. Harmless to serve, but it is
    dead weight in the wheel, and when ``dist/`` is committed it is how a
    checkout leaves one build's index beside another build's assets.
    """

    missing: frozenset[str]
    orphaned: frozenset[str]

    @property
    def consistent(self) -> bool:
        return not self.missing and not self.orphaned

    def why(self) -> str:
        parts = []
        if self.missing:
            parts.append(f"referenced but absent: {sorted(self.missing)}")
        if self.orphaned:
            parts.append(f"present but unreachable: {sorted(self.orphaned)}")
        return "; ".join(parts) or "consistent"


def audit_bundle(dist: Path) -> BundleAudit:
    """Walk a built bundle from ``index.html`` and report what does not line up.

    Reachability is transitive: a lazily-imported chunk is named by its
    parent chunk rather than by the index, so following only the index
    would call every lazy chunk an orphan.
    """
    assets_dir = dist / "assets"
    on_disk = (
        {p.name for p in assets_dir.iterdir() if p.is_file()} if assets_dir.is_dir() else set()
    )

    index = dist / "index.html"
    if not index.is_file():
        return BundleAudit(missing=frozenset(), orphaned=frozenset(on_disk))

    reached: set[str] = set()
    frontier = list(_ASSET_REF.findall(index.read_text(encoding="utf-8")))
    while frontier:
        name = frontier.pop()
        if name in reached:
            continue
        reached.add(name)
        target = assets_dir / name
        if target.is_file() and target.suffix in {".js", ".css"}:
            frontier.extend(_ASSET_REF.findall(target.read_text(encoding="utf-8", errors="ignore")))

    # index.html itself can appear in a chunk's text; it is not an asset.
    reached.discard("index.html")
    return BundleAudit(
        missing=frozenset(reached - on_disk),
        orphaned=frozenset(on_disk - reached),
    )


def inject_bootstrap_global(index_html: str, name: str, payload: object) -> str:
    """Splice ``window.<name> = <payload>`` into ``index_html`` ahead of the
    first module script (falling back to ``</head>``, then to prepending).

    The payload is trusted config, but it is JSON-encoded with every ``<``
    neutralized (``\\u003c``) anyway, so a stray ``</script>`` inside a value
    (a logo SVG, a tagline) cannot break out of the inline script.
    """
    payload_json = json.dumps(payload).replace("<", "\\u003c")
    snippet = f"<script>window.{name} = {payload_json};</script>"
    marker = '<script type="module"'
    idx = index_html.find(marker)
    if idx != -1:
        return index_html[:idx] + snippet + index_html[idx:]
    head_close = index_html.lower().find("</head>")
    if head_close != -1:
        return index_html[:head_close] + snippet + index_html[head_close:]
    return snippet + index_html


def build_spa_router(
    *,
    prefix: str,
    dist: Path,
    bootstrap_global: str,
    bootstrap_payload: dict,
    placeholder_html: str,
    tags: list[str] | None = None,
):
    """The one way an appkit-shell surface serves its built bundle.

    ``GET <prefix>/`` (and bare ``<prefix>``) serves ``dist/index.html``
    with ``window.<bootstrap_global> = <payload>`` injected ahead of the
    bundle; ``GET <prefix>/assets/{path}`` serves containment-checked
    assets. The index is read per-request so a fresh build serves
    without a restart, and it is sent ``no-cache`` so the browser
    actually asks — otherwise the rebuild reaches the server and stops
    there. Content-hashed assets cache hard (``immutable``), which is
    safe for exactly the same reason: a new build is a new URL.
    Without a build the router serves ``placeholder_html`` — an honest
    named-build-step page a caller supplies; it never fakes a surface.
    """
    from fastapi import APIRouter, Response
    from fastapi.responses import FileResponse, HTMLResponse

    router = APIRouter(tags=tags or [prefix.strip("/")])
    index_file = dist / "index.html"

    @router.get(prefix + "/")
    @router.get(prefix)
    async def index() -> HTMLResponse:
        if index_file.is_file():
            html = inject_bootstrap_global(
                index_file.read_text(encoding="utf-8"),
                bootstrap_global,
                bootstrap_payload,
            )
            return HTMLResponse(html, headers={"cache-control": INDEX_CACHE_CONTROL})
        # The placeholder is a state, not a page: once the build lands it
        # must disappear on the next load, not linger in a cache.
        return HTMLResponse(placeholder_html, headers={"cache-control": INDEX_CACHE_CONTROL})

    @router.get(prefix + "/assets/{path:path}")
    async def assets(path: str) -> Response:
        target = safe_asset_path(dist / "assets", path)
        if target is None:
            return Response(status_code=404)
        return FileResponse(target, headers={"cache-control": ASSET_CACHE_CONTROL})

    return router


def safe_asset_path(assets_root: Path, requested: str) -> Path | None:
    """Resolve ``requested`` under ``assets_root``; ``None`` unless the result
    stays inside the tree and is a real file (a ``..`` resolves out and is
    refused — the caller answers 404, never reveals why)."""
    root = assets_root.resolve()
    target = (root / requested).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target
