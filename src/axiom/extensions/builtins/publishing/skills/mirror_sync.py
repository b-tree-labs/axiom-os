# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``press.mirror_add`` / ``press.mirror_sync`` / ``press.mirror_status`` —
coordinate a document between a remote editor and a local mirror.

One registry file (``mirrors.json`` under the skill state dir) names each
mirror: the local path, the remote share URL, and whether git annotation is
on. ``mirror_sync`` runs one reconcile pass (or a watch pass, which also
detects and repairs stale-session flushes); ``mirror_status`` reports the
registry and each mirror's last-known state without touching the remote.
When both sides change with different content the pass returns a *blocked*
conflict and sync pauses; ``mirror_resolve`` clears it by choosing a side
(``theirs`` / ``ours`` / ``merged``) — see ADR-112 §D3.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from axiom.infra.conflict import ConflictOutcome
from axiom.infra.git import ensure_managed_gitignore, is_inside_work_tree
from axiom.infra.skills import SkillContext, SkillResult

from ..mirror import _GITIGNORE_MARKER, _GITIGNORE_PATTERNS, MirrorEngine


def _registry_path(ctx: SkillContext) -> Path:
    return Path(ctx.state_dir) / "mirrors.json"


def _load_registry(ctx: SkillContext) -> dict:
    path = _registry_path(ctx)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_registry(ctx: SkillContext, registry: dict) -> None:
    path = _registry_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2))


def add(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    local = params.get("local")
    url = params.get("url")
    if not local or not url:
        return SkillResult(ok=False, errors=[
            "missing required params: local (mirror path) and url (share URL)"])
    local_path = Path(local).expanduser().resolve()
    name = params.get("name") or local_path.stem
    registry = _load_registry(ctx)
    entry = {
        "local": str(local_path),
        "url": url,
        "git_annotate": bool(params.get("git_annotate", False)),
    }
    # Store the editor vendor only if the caller named one explicitly; otherwise
    # it is resolved from the URL by config (editor-routing.toml) at sync time —
    # no baked default.
    if params.get("vendor"):
        entry["vendor"] = params["vendor"]
    registry[name] = entry
    _save_registry(ctx, registry)
    # proactively ignore the mirror's artifacts if it lives in a git repo, so a
    # .conflict sidecar never shows up as a surprise untracked file (ADR-112 §D5)
    gitignore_updated = False
    try:
        if is_inside_work_tree(local_path.parent):
            _, gitignore_updated = ensure_managed_gitignore(
                local_path.parent, marker=_GITIGNORE_MARKER,
                patterns=_GITIGNORE_PATTERNS)
    except Exception:  # noqa: BLE001 — hygiene must never fail a registration
        pass
    return SkillResult(ok=True, value={
        "name": name, "registered": registry[name],
        "gitignore_updated": gitignore_updated})


def _engine_for(name: str, entry: dict, ctx: SkillContext) -> MirrorEngine:
    # Resolve the remote through the document-editor connector kind (ADR-110
    # §Decision-5, option B). The vendor is an explicit entry field if set, else
    # resolved from the URL by config (editor-routing.toml) — never a baked
    # default. A sharepoint URL routes to onedrive (the same GraphEditorEndpoint).
    from ..providers.editors import get_editor, resolve_editor_vendor

    vendor = resolve_editor_vendor(entry["url"], entry.get("vendor"))
    endpoint = get_editor(vendor, url=entry["url"])
    return MirrorEngine(
        endpoint=endpoint,
        mirror_path=Path(entry["local"]),
        state_path=Path(ctx.state_dir) / "mirror-state" / f"{name}.json",
        git_annotate=entry.get("git_annotate", False),
    )


def sync(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    registry = _load_registry(ctx)
    if not registry:
        return SkillResult(ok=False, errors=[
            "no mirrors registered — add one with mirror add"])
    name = params.get("name")
    targets = {name: registry[name]} if name and name in registry else registry
    if name and name not in registry:
        return SkillResult(ok=False, errors=[f"unknown mirror: {name}"])
    watch = bool(params.get("watch", False))

    reports: dict[str, Any] = {}
    errors: list[str] = []
    for mirror_name, entry in targets.items():
        try:
            engine = _engine_for(mirror_name, entry, ctx)
            report = engine.watch_once() if watch else engine.reconcile()
            row = dataclasses.asdict(report)
            if report.conflict is not None:
                row["guidance"] = report.conflict.guidance()
            reports[mirror_name] = row
        except Exception as exc:  # noqa: BLE001 — every mirror reports, none aborts the rest
            errors.append(f"{mirror_name}: {exc}")
    return SkillResult(ok=not errors, value={"reports": reports}, errors=errors)


def resolve(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Clear a blocked mirror conflict by choosing a side (ADR-112 §D3).

    params: ``name`` (required) and ``strategy`` in {theirs, ours, merged}.
    ``theirs`` keeps the incoming remote text; ``ours`` pushes your preserved
    edit; ``merged`` pushes whatever you hand-edited the mirror into. A remote
    that moved on since the conflict re-blocks rather than being overwritten.
    """
    name = params.get("name")
    strategy = params.get("strategy")
    if not name:
        return SkillResult(ok=False, errors=["missing required param: name"])
    if strategy not in ("theirs", "ours", "merged"):
        return SkillResult(ok=False, errors=[
            "strategy must be one of: theirs, ours, merged"])
    registry = _load_registry(ctx)
    if name not in registry:
        return SkillResult(ok=False, errors=[f"unknown mirror: {name}"])
    try:
        report = _engine_for(name, registry[name], ctx).resolve(strategy)
    except Exception as exc:  # noqa: BLE001 — report the failure, don't crash the CLI
        return SkillResult(ok=False, errors=[f"{name}: {exc}"])
    row = dataclasses.asdict(report)
    if report.conflict is not None:
        row["guidance"] = report.conflict.guidance()
    # a re-blocked resolution (remote advanced) is not a completed resolve
    return SkillResult(ok=report.action in ("resolved", "noop"),
                       value={"report": row})


def status(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    registry = _load_registry(ctx)
    rows = {}
    for name, entry in registry.items():
        state_path = Path(ctx.state_dir) / "mirror-state" / f"{name}.json"
        state = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
            except (json.JSONDecodeError, OSError):
                state = {"error": "unreadable state"}
        conflict = state.get("conflict") if isinstance(state, dict) else None
        row = {
            "local": entry["local"],
            "url": entry["url"],
            "git_annotate": entry.get("git_annotate", False),
            "remote_version": state.get("remote_version"),
            "mirror_exists": Path(entry["local"]).exists(),
            "blocked": bool(conflict),
        }
        if conflict:
            row["conflict"] = conflict
            row["guidance"] = ConflictOutcome(
                resource=name, reason=conflict.get("reason", ""),
                preserved_path=conflict.get("sidecar"),
                incoming_version=conflict.get("incoming_version", ""),
                blocked=True).guidance()
        rows[name] = row
    return SkillResult(ok=True, value={"mirrors": rows})
