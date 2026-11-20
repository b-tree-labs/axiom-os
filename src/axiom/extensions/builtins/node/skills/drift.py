# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``node.drift``: where the node disagrees with itself or with its tracked source.

Three rules, each a function over inputs a test can supply:

- ``pins``: the local-model pins vs the ensure unit vs the serving env
  (:mod:`axiom.llm.model_alignment`, ADR-140);
- ``files``: a declared tracked file vs its source (content hash), and any
  file in a live configuration directory with ``.bak*`` siblings and no
  declared source (a hand edit nobody tracks);
- ``units``: a declared engine's unit that systemd does not know.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import collect, declarations
from ._handoff import KEEP_EXPOSED, next_step
from .inventory import _collectors, _state_dir

RULES = ("pins", "files", "units")
#: Directories of live configuration a node commonly has, checked for hand edits
#: when the declarations name none. A declared ``config_dirs`` replaces this list.
DEFAULT_CONFIG_DIRS = ("~/.config/systemd/user", "~/.config/axiom-serving")
_SECRET_TOKENS = ("key", "token", "secret", "password", "passwd")


def _sha(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def pins_rule(settings=None) -> list[dict[str, Any]]:
    from axiom.llm import model_alignment

    a = model_alignment.check(settings=settings)
    return [
        {"rule": "pins", "severity": f.severity, "code": f.code, "message": f.message, "fix": f.fix}
        for f in a.findings
    ]


def files_rule(
    decl: declarations.Declarations, *, home: Path | None = None
) -> list[dict[str, Any]]:
    home = home or Path.home()
    out: list[dict[str, Any]] = []
    tracked_live = set()
    for t in decl.tracked:
        live = Path(t.live.replace("~", str(home), 1)) if t.live.startswith("~") else Path(t.live)
        tracked_live.add(live)
        if not live.exists():
            out.append(
                {
                    "rule": "files",
                    "severity": "warning",
                    "code": "tracked-missing",
                    "message": f"{t.live} is declared tracked but is not on this machine",
                    "fix": "remove the declaration or restore the file from its source",
                }
            )
            continue
        if not t.source:
            out.append(
                {
                    "rule": "files",
                    "severity": "info",
                    "code": "tracked-no-source",
                    "message": f"{t.live} is tracked but no source is declared, so drift cannot be measured",
                    "fix": f"node declare tracked --live {t.live} --source <path>",
                }
            )
            continue
        src = (
            Path(t.source.replace("~", str(home), 1))
            if t.source.startswith("~")
            else Path(t.source)
        )
        if src.exists() and src.is_file():
            if _sha(live) != _sha(src):
                out.append(
                    {
                        "rule": "files",
                        "severity": "warning",
                        "code": "tracked-differs",
                        "message": f"{t.live} differs from its source {t.source}",
                        "fix": "re-deploy the source, or commit the live change to it",
                    }
                )
        else:
            out.append(
                {
                    "rule": "files",
                    "severity": "info",
                    "code": "source-not-local",
                    "message": f"{t.live}'s source {t.source} is not a local file; compared at deploy time, not here",
                    "fix": None,
                }
            )
    dirs = decl.config_dirs or list(DEFAULT_CONFIG_DIRS)
    for d in dirs:
        if d.startswith("unreadable"):
            continue
        p = Path(d.replace("~", str(home), 1)) if d.startswith("~") else Path(d)
        if not p.is_dir():
            continue
        seen_bases: dict[str, int] = {}
        for f in p.iterdir():
            name = f.name
            if ".bak" in name or name.endswith(("~", ".orig")) or ".pre" in name:
                base = name.split(".bak")[0].split(".pre")[0].rstrip("~").removesuffix(".orig")
                seen_bases[base] = seen_bases.get(base, 0) + 1
        for base, n in sorted(seen_bases.items()):
            live = p / base
            if live.exists() and live not in tracked_live:
                out.append(
                    {
                        "rule": "files",
                        "severity": "warning",
                        "code": "hand-edited-untracked",
                        "message": f"{live} has {n} backup cop{'y' if n == 1 else 'ies'} beside it and no tracked source: it is edited by hand",
                        "fix": f"bring it under a repo and declare it: node declare tracked --live {live} --source <repo path>",
                    }
                )
    return out


def units_rule(
    decl: declarations.Declarations, unit_text_user: str, unit_text_system: str
) -> list[dict[str, Any]]:
    known = {u["unit"] for u in collect.parse_systemctl_units(unit_text_user)} | {
        u["unit"] for u in collect.parse_systemctl_units(unit_text_system)
    }
    out = []
    for e in decl.engines:
        if e.unit and e.unit not in known:
            out.append(
                {
                    "rule": "units",
                    "severity": "warning",
                    "code": "declared-unit-unknown",
                    "message": f"engine {e.name} is declared under {e.unit}, which systemd does not list",
                    "fix": "fix the declaration, or install the unit",
                }
            )
    return out


def credentials_rule(processes_text: str) -> list[dict[str, Any]]:
    """A credential on a command line is readable by every account on the machine."""
    out = []
    for e in collect.detect_engines(collect.parse_ps(processes_text)):
        if e.get("key_required"):
            out.append(
                {
                    "rule": "pins",
                    "severity": "warning",
                    "code": "credential-on-argv",
                    "message": f"the {e['kind']} engine (pid {e['pid']}) takes its API key on the command line, where any process listing shows it",
                    "fix": f"move it to the vault and read it at start; record the exposure with {KEEP_EXPOSED}",
                }
            )
    return out


def gather(ctx: SkillContext | None, only: list[str] | None = None) -> dict[str, Any]:
    want = set(only or RULES)
    decl = declarations.load(_state_dir(ctx))
    c = _collectors(ctx)
    findings: list[dict[str, Any]] = []
    if "pins" in want:
        try:
            findings += pins_rule()
        except Exception as exc:  # noqa: BLE001 - a rule that cannot run is a finding
            findings.append(
                {
                    "rule": "pins",
                    "severity": "info",
                    "code": "rule-unavailable",
                    "message": f"the pins rule could not run: {type(exc).__name__}: {exc}",
                    "fix": None,
                }
            )
        findings += credentials_rule(c.ps())
    if "files" in want:
        findings += files_rule(decl)
    if "units" in want:
        findings += units_rule(decl, c.user_units(), c.system_units())
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    counts = {
        k: sum(1 for f in findings if f["severity"] == k) for k in ("error", "warning", "info")
    }
    summary = (
        "on an even keel: nothing has drifted"
        if not findings
        else f"{counts['error']} error(s), {counts['warning']} warning(s), {counts['info']} note(s)"
    )
    return {"findings": findings, "summary": summary, "counts": counts, "rules": sorted(want)}


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    only = params.get("only")
    only = [r for r in only if r in RULES] if isinstance(only, list) else None
    value = gather(ctx, only)
    steps = []
    codes = {f["code"] for f in value["findings"]}
    if "hand-edited-untracked" in codes or "tracked-no-source" in codes:
        steps.append(
            next_step(
                "node.declare",
                "declare the live file's source so drift is measured, not suspected (a live file has no tracked source)",
            )
        )
    if "credential-on-argv" in codes:
        steps.append(
            next_step(
                KEEP_EXPOSED, "record the exposure and rotate the key (a credential is exposed)"
            )
        )
    return SkillResult(
        ok=True, value=value, actions_taken=[f"ran {len(value['rules'])} drift rule(s)"], next=steps
    )


__all__ = [
    "DEFAULT_CONFIG_DIRS",
    "RULES",
    "credentials_rule",
    "files_rule",
    "gather",
    "pins_rule",
    "run",
    "units_rule",
]
