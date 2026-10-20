# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``vault.sweep`` — KEEP's stewardship pass over unmanaged credential material.

KEEP is Steward + Governor (ADR-055). Stewardship includes the standing claim
that **credentials are under management** — and nothing was checking it, because
a store can only audit what it knows about.

The boundary matters and is deliberate (secrets/docs/decisions/adr-001): the
*mechanism* of discovery belongs to the ``secrets`` extension, which owns
operational credentials and knows what is in the store. KEEP owns the *policy*
that discovery happens on a cadence and that findings are escalated. This skill
is the seam: KEEP calls, ``secrets`` looks.

Folding discovery into KEEP would put operational-credential scanning inside the
capability-token primitive, which is precisely the conflation ADR-001 warns
against.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult



def _known_fingerprints(ctx: SkillContext | None) -> list[str]:
    """Digests of everything already in custody.

    Metadata only — `list()` never opens the keychain, so a sweep does not
    touch a single credential value to learn what it is holding. A store
    that cannot be read yields an empty list and the sweep reports
    `unverifiable` rather than inventing a clean bill.
    """
    if ctx is None or getattr(ctx, "state_dir", None) is None:
        return []
    try:
        from axiom.extensions.builtins.secrets.foreign.store import (
            ForeignCredentialStore,
        )

        rows = ForeignCredentialStore(ctx.state_dir).list()
    except Exception:  # noqa: BLE001 — an unreadable store is not a clean one
        return []
    return [str(r["fingerprint"]) for r in rows if r.get("fingerprint")]


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Invoke ``secrets.discover`` and turn its findings into KEEP's verdict.

    Escalation, not just reporting: unmanaged material makes the sweep fail, so
    a heartbeat that finds a loose credential is visibly not-ok rather than a
    line in a log nobody reads.
    """
    from axiom.extensions.builtins.secrets.skills import discover as discover_skill

    # The index `discover` compares findings against. Without it every
    # finding is `unchecked`, `unmanaged` is zero, and this sweep reports
    # clean over a host it has not actually checked — which is what it did,
    # hourly, because the heartbeat passed no fingerprints and the store
    # recorded none to pass.
    #
    # Supplied here rather than read inside `discover`, so the scanner stays
    # a pure function of what it is given and this skill stays the place
    # that decides what counts as "in custody".
    params = dict(params)
    if "known_fingerprints" not in params:
        params["known_fingerprints"] = _known_fingerprints(ctx)

    result = discover_skill.run(params, ctx)
    value = result.value or {}
    unmanaged = value.get("unmanaged", 0)
    unchecked = value.get("unchecked", 0)
    stale = value.get("stale_holders", [])
    # Propagated, not recomputed. `discover` is the only thing that knows
    # whether it had an index to classify against, and a sweep that reports
    # ok while the scanner says it could not check is the same false clean
    # bill one layer up.
    unverifiable = bool(value.get("unverifiable"))

    actions = [f"swept {value.get('total', 0)} credential location(s)"]
    errors: list[str] = []

    if unverifiable:
        errors.append(
            f"could not classify {value.get('total', 0)} finding(s): the store "
            "recorded no fingerprints to compare them against, so nothing here "
            "is known to be managed OR unmanaged"
        )
    if unmanaged:
        errors.append(
            f"{unmanaged} credential(s) live outside the store — no rotation "
            f"cadence, no expiry audit, and no revocation path applies to them"
        )
        for f in value.get("findings", []):
            if f.get("managed") is False:
                errors.append(f"  unmanaged: {f['matcher']} at {f['locator']}")
    if stale:
        # The sharpest finding KEEP can make: the credential at rest is correct,
        # the process is running, the unit reports healthy, and every operation
        # needing that credential fails silently.
        errors.append(
            f"{len(stale)} process(es) hold a credential that on-disk config has "
            f"replaced — healthy-looking units, failing silently"
        )
        errors.extend(f"  stale: {h['remediation']}" for h in stale)

    if unchecked:
        actions.append(
            f"{unchecked} finding(s) unchecked against the store — pass "
            f"known_fingerprints to classify them"
        )

    return SkillResult(
        ok=not unmanaged and not stale and not unverifiable,
        value={"sweep": value, "unmanaged": unmanaged, "stale_holders": len(stale)},
        errors=errors,
        actions_taken=actions,
    )


__all__ = ["run"]
