# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.kit_*`` — the tenant data kit's five verbs, one skill each (ADR-056).

The CLI, MCP, an agent and the walkthrough notebook all call these, so every
surface gets the same answer. Each result carries structured data for an agent
and a ``text`` rendering a person reads at a terminal; ``--json`` shows both.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import kit
from ..kit.project import KitError


def _repo(params: dict[str, Any]) -> Path:
    return Path(str(params.get("dir") or Path.cwd())).expanduser()


def _fail(exc: Exception) -> SkillResult:
    return SkillResult(ok=False, errors=[str(exc)])


def _cli() -> str:
    """The command name the person actually runs (``axi`` or a product's own)."""
    from axiom.infra.branding import get_branding

    return get_branding().cli_name or "axi"


def _next(*steps: str) -> str:
    """Next steps, written with the CLI name in use (#1163)."""
    return "next: " + "  →  ".join(f"`{_cli()} {s}`" for s in steps)


def kit_init(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    try:
        written = kit.init(
            _repo(params), str(params.get("tenant") or ""), force=bool(params.get("force"))
        )
    except KitError as exc:
        return _fail(exc)
    text = "\n".join(
        [
            f"wrote a data kit with one working example of each contribution ({len(written)} files):",
            "  data/conform/   bronze → silver: a normalizer and its tests",
            "  data/silver/    building blocks: a derived channel, roles",
            "  data/gold/      a gold object: SQL over your own silver",
            "  data/verbs/     a question chat can answer",
            "  data/charts/    a chart over the gold object",
            "  data/notebooks/ the walkthrough, runnable",
            _next("data kit-up", "data kit-try"),
        ]
    )
    return SkillResult(ok=True, value={"written": written, "text": text})


def kit_up(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    try:
        project = kit.load(_repo(params))
        out = kit.up(project)
    except KitError as exc:
        return _fail(exc)
    started = "started" if out.get("started") else "already running"
    text = "\n".join(
        [
            f"local medallion {started} for {project.tenant}",
            *(f"  {line}" for line in out.get("prepared", [])),
            _next("data kit-try"),
        ]
    )
    return SkillResult(ok=True, value={**out, "tenant": project.tenant, "text": text})


def _render_try(r: dict[str, Any]) -> str:
    s = r["silver"]
    lines = [
        f"tenant {r['tenant']}",
        "",
        f"bronze → silver   {s['rows_in']} records in, {s['rows_out']} rows out, "
        f"{s['errored']} errored  ({', '.join(s['normalizers']) or 'no normalizers'})",
    ]
    if s.get("unknown_schema"):
        lines.append(f"  ! no normalizer for: {', '.join(s['unknown_schema'])}")
    dep = s.get("deposits")
    if dep:
        lines.append(
            f"  deposits          {dep['deposits_claimed']} claimed → {dep['rows_out']} rows, "
            f"{dep['deposits_skipped']} declined by format, {dep['errored']} errored"
        )
        lines += [f"    ! {e}" for e in dep.get("errors", [])]
        if dep.get("content_missing"):
            lines.append(f"    ! bytes missing for: {', '.join(dep['content_missing'][:5])}")
    lines += [f"  {d}" for d in s.get("declarations", [])]
    lines += ["", "gold"]
    for name, rows in r["gold"]["samples"].items():
        lines.append(f"  {name}: {len(rows)} sample rows")
        for row in rows[:3]:
            lines.append("    " + "  ".join(f"{k}={v}" for k, v in row.items()))
    lines += ["", "verbs (run as your tenant role)"]
    for name, rows in r["verbs"].items():
        lines.append(f"  {name}: {len(rows)} rows")
        for row in rows[:3]:
            lines.append("    " + "  ".join(f"{k}={v}" for k, v in row.items()))
    lines += ["", "charts"]
    for c in r["charts"]:
        lines.append(f"  {c['chart']} → {c['svg']}")
        lines += [f"  {line}" for line in c["lines"]]
    lines += ["", _next("data kit-check")]
    return "\n".join(lines)


def kit_try(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    try:
        report = kit.try_kit(kit.load(_repo(params)))
    except KitError as exc:
        return _fail(exc)
    return SkillResult(ok=True, value={**report, "text": _render_try(report)})


def kit_check(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    try:
        report = kit.check_kit(kit.load(_repo(params)), isolation=not params.get("static_only"))
    except KitError as exc:
        return _fail(exc)
    lines = []
    for label, key in (("declarations", "declarations"), ("normalizers", "normalizers")):
        found = report[key]
        lines.append(f"{label}: {'clean' if not found else f'{len(found)} to fix'}")
        lines += [f"  ✗ {f}" for f in found]
    iso = report["isolation"]
    static_only = bool(params.get("static_only"))
    if iso == "not run":
        lines.append(
            f"isolation: NOT RUN (no local medallion; run `{_cli()} data kit-up` first). Not a pass."
        )
    elif not iso:
        lines.append(
            "isolation: proven (every answer unchanged when another tenant's data is added)"
        )
    else:
        lines.append("isolation: LEAKS")
        lines += [f"  ✗ {f}" for f in iso]
    lines += [f"  note: {n}" for n in report.get("notes", [])]
    ok = report["ok"] and (iso != "not run" or static_only)
    lines.append("")
    # No promotion verb exists yet (#1163): say what the next real step is.
    lines.append(
        "ready for a pull request to your site repository"
        if ok
        else "not ready: fix the checks above first"
    )
    errors = [] if ok else ["kit-check found something to fix (see above)"]
    return SkillResult(ok=bool(ok), value={**report, "text": "\n".join(lines)}, errors=errors)


def kit_down(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    try:
        out = kit.down(kit.load(_repo(params)))
    except KitError as exc:
        return _fail(exc)
    return SkillResult(
        ok=True,
        value={**out, "text": f"dropped the sandbox {out['removed']}. Snapshots under data/snapshots/ stay."},
    )


def _rows(params: dict[str, Any]) -> list[dict[str, Any]]:
    raw = params.get("rows")
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.startswith("@"):
        raw = Path(raw[1:]).expanduser().read_text(encoding="utf-8")
    if isinstance(raw, str) and raw.strip():
        loaded = json.loads(raw)
        if isinstance(loaded, list):
            return loaded
    return []


def kit_snapshot(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    try:
        project = kit.load(_repo(params))
        channels = params.get("channels") or []
        if isinstance(channels, str):
            channels = [part for part in channels.split(",") if part]
        window = {}
        if params.get("start"):
            window["start"] = str(params["start"])
        if params.get("end"):
            window["end"] = str(params["end"])
        doc = kit.take_snapshot(
            project,
            site=str(params.get("site") or project.tenant),
            channels=list(channels),
            rows=_rows(params),
            window=window,
            snapshot_id=str(params.get("id") or ""),
        )
    except KitError as exc:
        return _fail(exc)
    text = (
        f"kept snapshot {doc['id']}: {len(doc['rows'])} rows for {doc['site']}. "
        "kit-down does not remove it."
    )
    return SkillResult(ok=True, value={**doc, "text": text})


def kit_materialize(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    try:
        project = kit.load(_repo(params))
        out = kit.materialize(project, str(params.get("id") or ""))
    except KitError as exc:
        return _fail(exc)
    text = f"materialized {out['rows']} rows from snapshot {out['snapshot']} into the sandbox"
    return SkillResult(ok=True, value={**out, "text": text})


# --- preconditions (declared on the specs in skills/__init__.py) -----------
# Cheap, side-effect free, and each says why when it is unmet.


def kit_present(params: dict[str, Any], ctx: SkillContext) -> tuple[bool, str]:  # noqa: ARG001
    """A data/kit.toml exists at or above ``dir`` and reads."""
    try:
        kit.load(_repo(params))
    except KitError as exc:
        return False, str(exc)
    return True, ""


def source_declared(params: dict[str, Any], ctx: SkillContext) -> tuple[bool, str]:  # noqa: ARG001
    """``source`` names a [sources.<name>] table in kit.toml."""
    try:
        project = kit.load(_repo(params))
    except KitError as exc:
        return False, str(exc)
    source = str(params.get("source") or "")
    declared = {*project.sources, *project.deposit_sources}
    if source in declared:
        return True, ""
    return (
        False,
        f"no source {source!r} in kit.toml (declared: {', '.join(sorted(declared)) or 'none'})",
    )


def origin_exists(params: dict[str, Any], ctx: SkillContext) -> tuple[bool, str]:  # noqa: ARG001
    """``origin`` is a file or folder that exists."""
    origin = Path(str(params.get("origin") or "")).expanduser()
    return (
        (True, "")
        if str(params.get("origin") or "") and origin.exists()
        else (False, f"{origin} does not exist")
    )


def kit_scaffold(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    try:
        project = kit.load(_repo(params))
        written = kit.scaffold_contribution(
            project,
            str(params.get("kind") or ""),
            str(params.get("name") or ""),
            deposit=bool(params.get("deposit")),
        )
    except KitError as exc:
        return _fail(exc)
    text = "\n".join(
        [
            f"wrote {len(written)} file(s) under data/:",
            *(f"  {w}" for w in written),
            "fill in what it leaves blank; kit-check names anything still missing",
            _next("data kit-try", "data kit-check"),
        ]
    )
    return SkillResult(ok=True, value={"written": written, "text": text})


def kit_sample(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    try:
        project = kit.load(_repo(params))
        out = kit.sample(project, str(params.get("source") or ""), str(params.get("origin") or ""))
    except KitError as exc:
        return _fail(exc)
    lines = [f"added {len(out['added'])} file(s) to source {out['source']}"]
    if out["first"]:
        lines.append(f"samples now span {out['first']} → {out['last']}")
    lines.append(f"channels your normalizers read from them ({len(out['channels'])}):")
    for ch, e in out["channels"].items():
        unit = e["unit"] or "unit not declared"
        lines.append(f"  {ch}  [{unit}]  {e['rows']} rows")
    if not out["channels"]:
        lines.append("  none — register a normalizer for this source's schema_ref in conform/")
    for item in out.get("unreadable", []):
        lines.append(f"  ! your normalizer could not read {item}")
    lines.append(_next("data kit-try"))
    return SkillResult(ok=True, value={**out, "text": "\n".join(lines)})


__all__ = [
    "kit_check",
    "kit_down",
    "kit_init",
    "kit_materialize",
    "kit_sample",
    "kit_scaffold",
    "kit_snapshot",
    "kit_try",
    "kit_up",
]
