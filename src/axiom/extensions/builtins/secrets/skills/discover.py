# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.discover`` — credential material living outside the store.

`secrets.audit` answers "are the credentials I manage expiring?". This answers
the prior question nobody was asking: **"what am I not managing?"** The store
reports a clean bill precisely because it has never heard of the credential in
a git remote URL, so no cadence applies and no expiry audit sees it.

Two answers from one scan, because they are the same evidence indexed
differently:

* fingerprints absent from the store → **unmanaged** — adopt them
* fingerprints present in the store → **consumers** — who to update before you
  rotate, so a rotation does not break something you did not know existed

Values are never returned, logged, or stored. A finding carries a truncated
digest, so a discovery report is safe to paste into a ticket.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from axiom.infra.skills import SkillContext, SkillResult

from ..discovery import Finding, correlate_stale, fingerprint, run_probes


#: Stale holders from the most recent scan — a rotation left these behind.
_LAST_STALE: list = []


def discover_findings(
    roots: dict[str, Path],
    *,
    known_fingerprints: Iterable[str] | None = None,
    only: Iterable[str] | None = None,
) -> list[Finding]:
    """The pure core — no store, no I/O beyond the probes. Shared by CLI + KEEP.

    ``known_fingerprints`` is injected rather than read from the store here, so
    this stays testable and so the decision to reveal stored values (to compute
    their digests) is made once, by the caller, and never as a side effect of a
    scan.
    """
    known = set(known_fingerprints or ())
    errors: list[str] = []
    hits = run_probes(roots, only=only, on_error=lambda n, e: errors.append(f"{n}: {e}"))
    _LAST_STALE.clear()
    _LAST_STALE.extend(correlate_stale(hits))

    from ..discovery.matchers import match_text

    findings: list[Finding] = []
    for hit in hits:
        matched = match_text(hit.value)
        matcher = matched[0][0] if matched else "unknown"
        fp = fingerprint(hit.value)
        managed = (fp in known) if known else None
        hints: tuple[str, ...] = ()
        if managed is False:
            hints = (
                "adopt: axi secrets set <name> --provider <kind>",
                "then remove the literal from its location and wire the consumer "
                "through the credential helper",
            )
        elif managed is True:
            hints = ("consumer of a managed credential — update here when it rotates",)
        findings.append(
            Finding(
                locator=hit.locator,
                probe=hit.probe,
                matcher=matcher,
                fingerprint=fp,
                managed=managed,
                detail=hit.detail,
                hints=hints,
            )
        )
    # Unmanaged first: the report should open on what needs action.
    findings.sort(key=lambda f: (f.managed is not False, f.probe, f.locator))
    return findings


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    home = Path(params.get("home") or Path.home())
    workspace = Path(params.get("workspace") or home / "Projects")
    roots = {
        "git-remote": workspace,
        "git-credentials": home,
        "env-file": Path(params.get("env_root") or home / ".config"),
        "default": home,
    }
    findings = discover_findings(
        roots,
        known_fingerprints=params.get("known_fingerprints"),
        only=params.get("probes"),
    )
    unmanaged = [f for f in findings if f.managed is False]
    unknown = [f for f in findings if f.managed is None]
    stale = list(_LAST_STALE)

    return SkillResult(
        # Unmanaged credential material is the whole point of the sweep, so its
        # presence is not "ok" — a clean run must mean nothing is loose.
        # A stale holder is a live outage wearing a healthy unit, so it counts
        # against a clean run exactly as loose material does.
        ok=not unmanaged and not stale,
        value={
            "total": len(findings),
            "unmanaged": len(unmanaged),
            "consumers": len([f for f in findings if f.managed is True]),
            "unchecked": len(unknown),
            "stale_holders": [s.to_dict() for s in stale],
            "summary": [
                f"{f.severity}: {f.matcher} at {f.locator} ({f.detail})"
                for f in findings
            ],
            "findings": [f.to_dict() for f in findings],
        },
        errors=[s.remediation for s in stale],
        actions_taken=[],
    )


__all__ = ["discover_findings", "run"]
