# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev.check`` — prove the hot-reload loop on this checkout.

Starts the checkout (or uses it if up), fetches its ``ready_path`` through
``lane.front``, applies a one-line design-token override, asserts the dev
server serves it within 3 seconds with no restart, restores the file, and
stops what it started. The override is a seam so a repo (or a test) can supply
its own.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import config, node
from .. import web as webmod
from . import resolve_lane

BUDGET_S = 3.0
_PREFER = ("token", "brand", "theme", "variables")


class Override:
    """One reversible edit and the URL path where its marker must appear."""

    def __init__(self, path: Path, url_path: str, marker: str, original: str):
        self.path, self.url_path, self.marker, self.original = path, url_path, marker, original

    def restore(self) -> None:
        self.path.write_text(self.original, encoding="utf-8")


def default_override(root: Path, cfg: config.DevConfig) -> Override | None:
    """Append one custom property to the repo's token or brand stylesheet."""
    base = (Path(root) / cfg.web.cwd).resolve()
    css = [
        p
        for p in base.rglob("*.css")
        if not {"node_modules", "dist", "build", ".git"} & set(p.relative_to(base).parts)
    ]
    if not css:
        return None
    css.sort(key=lambda p: (not any(k in p.name.lower() for k in _PREFER), len(p.parts), str(p)))
    path = css[0]
    marker = f"dev-check-{uuid.uuid4().hex[:10]}"
    original = path.read_text(encoding="utf-8")
    path.write_text(original + f"\n:root {{ --{marker}: 1; }}\n", encoding="utf-8")
    return Override(path, "/" + path.relative_to(base).as_posix(), marker, original)


def _body(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return ""


NO_WEB = (
    "no web server declared: hot reload is checked in the repo that has one "
    "(the component kit's `make dev`)"
)


def _get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def _node_only(params: dict[str, Any], ctx: SkillContext, lane: dict[str, Any]) -> SkillResult:
    """No [web]: node up, sign-in page and app answer, schema has no drift, down."""
    from . import db
    from .down import run as down_run
    from .up import run as up_run

    home = node.home_for(ctx.state_dir, lane["name"])
    rec = node.read_record(home)
    already = bool(rec and node.alive(int(rec["pid"])))
    started = up_run(params, ctx)
    if not started.ok:
        return started
    try:
        base = started.value["node_url"].split("/", 3)
        base = "/".join(base[:3])
        misses = [
            f"{path} answered {code}, not 200"
            for path in ("/gate/login", "/app/")
            if (code := _get(base + path)) != 200
        ]
        status = db.status(params, ctx)
        if not status.ok:
            misses += status.errors
        else:
            misses += [
                f"schema {d['extension']}: {d['kind']} ({d['cause']})"
                for d in status.value["drift"]
                if d["kind"] in ("ahead", "diverged")
            ]
    finally:
        if not already:
            down_run(params, ctx)
    if misses:
        return SkillResult(ok=False, errors=misses)
    return SkillResult(
        value={"mode": "node-only", "node": base, "web": None},
        actions_taken=[
            f"node up on {base}: /gate/login 200, /app/ 200, schema has no drift, stopped",
            NO_WEB,
        ],
    )


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from .down import run as down_run
    from .up import run as up_run

    if params.get("topology"):
        from .. import graph

        return graph.check(params, ctx)
    lane, errors = resolve_lane(params)
    if errors:
        return SkillResult(ok=False, errors=errors)
    root = Path(lane["root"])
    try:
        cfg = config.load(root)
    except config.DevConfigError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    if cfg.web is None:
        return _node_only(params, ctx, lane)

    home = node.home_for(ctx.state_dir, lane["name"])
    already = bool(node.read_record(home) and node.alive(int(node.read_record(home)["pid"])))
    started = up_run(params, ctx)
    if not started.ok:
        return started
    seam: Callable[[Path, config.DevConfig], Override | None] = (
        params.get("override") or default_override
    )
    override = None
    try:
        front = webmod.url_for(int(lane["front"]), cfg.web.ready_path)
        if not node.answers(front):
            return SkillResult(ok=False, errors=[f"{front} did not answer 200"])
        override = seam(root, cfg)
        if override is None:
            return SkillResult(
                ok=False,
                errors=[
                    "found no stylesheet to override under [web].cwd; supply one with the override seam"
                ],
            )
        target = webmod.url_for(int(lane["front"]), override.url_path)
        t0 = time.monotonic()
        seen = False
        while time.monotonic() - t0 < BUDGET_S:
            if override.marker in _body(target):
                seen = True
                break
            time.sleep(0.1)
        took = time.monotonic() - t0
    finally:
        if override is not None:
            override.restore()
        if not already:
            down_run(params, ctx)
    if not seen:
        return SkillResult(
            ok=False,
            errors=[
                f"hot reload failed: the dev server did not serve the change to {override.url_path} within {BUDGET_S:.0f}s (no restart allowed). A slow or rebuilding loop fails this check."
            ],
        )
    return SkillResult(
        value={"front": front, "changed": override.url_path, "seconds": round(took, 2)},
        actions_taken=[f"hot reload served the change in {took:.2f}s: {override.url_path}"],
    )
