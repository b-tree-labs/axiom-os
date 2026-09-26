# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``memory.absorb`` skill — ingest a harness's native memory
(ADR-087 D2/D8, PRD F3).

Builds the requested absorb adapter (read-only against the source
store), scans it, and lands the candidates through the D2 import
primitive: origin-stamped provenance, idempotency-key echo suppression
(re-absorb is a no-op), kept-both + queued on same-source content
drift, vault never absorbed.

The write-time near-neighbor check (D3 clock 1) runs by default with
the offline lexical matcher; pass ``no_dedup`` to land candidates
verbatim, or inject ``dedup_engine`` (tests, or an embedder-backed
engine). Params:

- ``composition`` (required), ``harness`` (required), ``principal``
  (required) — who owns the absorbed memories.
- ``account`` — opaque provider-scoped account label (default
  ``"local"``).
- ``home`` — override the harness's home directory (claude-code,
  gemini-cli, codex, hermes).
- ``roots`` — project roots for hierarchy walks (claude-code,
  gemini-cli; required for agents-md).
- ``path`` — store path for goose (memory dir) / letta (sqlite db).
- ``dry_run`` — scan + classify, write nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

_AGENT = "axi-memory"


def _build_adapter(params: dict[str, Any]):
    """Adapter factory keyed by harness name. Raises ValueError."""
    from axiom.memory.absorb.markdown_hierarchy import (
        agents_md_adapter,
        claude_code_adapter,
        gemini_cli_adapter,
    )
    from axiom.memory.absorb.passage_store import letta_adapter
    from axiom.memory.absorb.structured_store import (
        codex_adapter,
        goose_adapter,
        hermes_adapter,
    )

    harness = params["harness"]
    account = params.get("account") or "local"
    home = Path(params["home"]) if params.get("home") else None
    roots = [Path(r) for r in (params.get("roots") or [])]
    path = Path(params["path"]) if params.get("path") else None

    if harness == "claude-code":
        return claude_code_adapter(
            account=account, home=home, project_roots=roots,
        )
    if harness == "agents-md":
        if not roots:
            raise ValueError(
                "agents-md needs at least one --root (the repo/tree to walk)"
            )
        return agents_md_adapter(account=account, roots=roots)
    if harness == "gemini-cli":
        return gemini_cli_adapter(
            account=account, home=home, project_roots=roots,
        )
    if harness == "codex":
        return codex_adapter(account=account, home=home)
    if harness == "goose":
        return goose_adapter(account=account, base=path)
    if harness == "hermes":
        return hermes_adapter(account=account, home=home)
    if harness == "letta":
        return letta_adapter(account=account, db_path=path)
    raise ValueError(
        f"unknown harness {harness!r}; known: agents-md, claude-code, "
        "codex, gemini-cli, goose, hermes, letta"
    )


def absorb(params: dict[str, Any], ctx: SkillContext | None) -> SkillResult:
    """Scan one harness-native store and import its memories."""
    composition = params.get("composition")
    if composition is None:
        return SkillResult(ok=False, errors=["no composition service provided"])
    if not params.get("harness"):
        return SkillResult(ok=False, errors=["--harness is required"])
    principal = params.get("principal")
    if not principal:
        return SkillResult(
            ok=False,
            errors=[
                "--principal is required: absorbed memories need an owner"
            ],
        )

    try:
        adapter = _build_adapter(params)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    dedup = None
    if not params.get("no_dedup"):
        if params.get("dedup_engine") is not None:
            dedup = params["dedup_engine"]
        else:
            from axiom.memory.dedup import DedupEngine

            # Offline-safe default: lexical matching only. An
            # embedder-backed engine is an injection point, not a
            # network surprise.
            dedup = DedupEngine(embedder=None)

    from axiom.memory.absorb.importer import import_candidates

    scan = adapter.scan()
    dry_run = bool(params.get("dry_run", False))
    report = import_candidates(
        composition,
        scan.candidates,
        principal=principal,
        agent=_AGENT,
        dedup=dedup,
        dry_run=dry_run,
    )

    skipped = [
        {"source": s.source, "reason": s.reason}
        for s in (*scan.skipped, *report.skipped)
    ]
    if not dry_run and scan.skipped:
        composition.audit_log.record(
            entry_type="absorb_scan_skips",
            principal_id=principal,
            agent_id=_AGENT,
            fragment_id="",
            outcome="degraded",
            harness=adapter.harness,
            skipped=len(scan.skipped),
        )

    value = {
        "harness": adapter.harness,
        "account": params.get("account") or "local",
        "principal": principal,
        "candidates": len(scan.candidates),
        "imported": report.imported,
        "skipped_echo": report.skipped_echo,
        "conflicts_queued": report.conflicts_queued,
        "collapsed_exact": report.collapsed_exact,
        "merged_near_dup": report.merged_near_dup,
        "skipped": skipped,
        "dry_run": dry_run,
    }
    actions = []
    if not dry_run and report.imported:
        actions.append(
            f"absorbed {report.imported} memorie(s) from {adapter.harness} "
            f"for {principal}"
        )
    return SkillResult(ok=True, value=value, actions_taken=actions)
