# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What we STORE about a credential, against what the issuer actually says.

Expiry auditing reads our own metadata, which records what we believed when we
wrote it down — not what is true at the issuer. The gap between those is where a
live credential goes quietly dead:

A stored token had been deleted or regenerated in the provider's UI months
earlier. We still held its value, with no expiry recorded, so no audit could
flag it and no rotation pass could act on it. The first symptom was an HTTP 401
in somebody's face, and the conclusion drawn from it was that the entire host
was unreachable.

Nothing had ever compared the two sides. A token can describe itself, so the
comparison is cheap and exact — revoked, real expiry, real name, real scopes.

**Backfilling an expiry the issuer already knows is the single change that would
have made this visible months earlier.** An unrecorded expiry is not unknowable;
it is unasked.

Two boundaries this does not cross:

**Applying writes METADATA only.** Recording a fact the issuer just told us is
not the same authority as changing what a credential is. No value is written, no
token is minted, nothing is rotated.

**An orphan is never "fixed" by applying.** Writing an expiry onto a value the
issuer rejects would make a dead credential look healthy, which is precisely the
failure this exists to end. It needs a human to mint a replacement.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult, ensure_context

#: Statuses that mean a human should look.
ACTIONABLE = ("orphaned", "revoked", "expiry_drift", "expiry_unknown_locally")

#: Statuses `--apply` may act on. Deliberately narrow: only recording something
#: the issuer told us about a credential it still recognises.
APPLICABLE = ("expiry_drift", "expiry_unknown_locally")

#: Statuses that mean the credential DEMONSTRABLY worked just now. For a
#: credential we cannot date — somebody else's service account, a token with no
#: expiry concept — "it answered a moment ago" is the only honest health signal
#: there is, and recording it turns "no idea" into "verified N days ago".
#:
#: A failed or unreachable check stamps NOTHING. A dead credential that looked
#: healthier after the check than before it would be the worst possible outcome.
PROVEN_ALIVE = ("ok", "expiry_drift", "expiry_unknown_locally",
                "expiry_unknown_everywhere", "revoked")


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def _describe_via_issuer(name: str, meta: dict[str, Any]) -> tuple[str, dict | None]:
    """Ask the issuer what it knows about this credential.

    Returns ``(outcome, facts)`` where outcome is ``"ok"``, ``"rejected"`` (the
    issuer does not recognise the value), ``"unsupported"`` (no API for this
    provider), or ``"unreachable"``.

    ``unreachable`` is NOT ``rejected``. A network blip must never be recorded
    as a dead credential — that would recreate the original error from the
    other side, declaring a healthy token orphaned.
    """
    import json
    import urllib.error
    import urllib.request

    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
    from axiom.infra.paths import get_user_state_dir

    if str(meta.get("provider") or "") != "gitlab-pat":
        return "unsupported", None
    issuer = str(meta.get("issuer_url") or "").rstrip("/")
    if not issuer:
        return "unsupported", None

    try:
        secret = ForeignCredentialStore(get_user_state_dir()).get(name)
        value = getattr(secret, "value", secret)
        value = value.decode() if isinstance(value, bytes) else str(value)
    except Exception:  # noqa: BLE001
        return "unsupported", None

    request = urllib.request.Request(
        f"{issuer}/api/v4/personal_access_tokens/self",
        headers={"PRIVATE-TOKEN": value},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        return ("rejected", None) if exc.code in (401, 403) else ("unreachable", None)
    except Exception:  # noqa: BLE001
        return "unreachable", None

    return "ok", {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "revoked": bool(payload.get("revoked")),
        "active": payload.get("active"),
        "expires_at": payload.get("expires_at"),
        "scopes": list(payload.get("scopes") or []),
    }


def _classify(stored: dict[str, Any], outcome: str, facts: dict | None) -> str:
    if outcome == "unsupported":
        return "unsupported"
    if outcome == "unreachable":
        return "unreachable"
    if outcome == "rejected":
        return "orphaned"
    facts = facts or {}
    if facts.get("revoked"):
        return "revoked"

    stored_expiry = str(stored.get("expires_at") or "").strip()
    issuer_expiry = str(facts.get("expires_at") or "").strip()
    if not stored_expiry and issuer_expiry:
        return "expiry_unknown_locally"
    if not stored_expiry and not issuer_expiry:
        # Neither side knows. Not drift — the issuer genuinely minted a
        # non-expiring credential, and saying "drift" would be a false finding.
        return "expiry_unknown_everywhere"
    if stored_expiry != issuer_expiry:
        return "expiry_drift"
    return "ok"


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Compare every stored credential against its issuer."""
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    ctx = ensure_context(ctx)
    store = params.get("_store") or ForeignCredentialStore(ctx.state_dir)
    describe = params.get("_describer") or _describe_via_issuer
    apply_changes = bool(params.get("apply"))

    findings: list[dict[str, Any]] = []
    applied: list[str] = []
    verified: list[str] = []
    errors: list[str] = []

    for meta in store.list():
        name = str(meta.get("name") or "")
        if not name:
            continue
        outcome, facts = describe(name, meta)
        status = _classify(meta, outcome, facts)
        finding: dict[str, Any] = {
            "name": name,
            "status": status,
            "provider": meta.get("provider", ""),
            "stored": {"expires_at": meta.get("expires_at")},
            "issuer": facts or {},
            "proposed_metadata": {},
        }
        if status in APPLICABLE:
            finding["proposed_metadata"] = {"expires_at": (facts or {}).get("expires_at")}
        findings.append(finding)

        if status == "orphaned":
            errors.append(
                f"{name}: the issuer does not recognise this value — it was "
                f"deleted or regenerated at the provider. Mint a replacement and "
                f"store it; no metadata change can fix this."
            )
        elif status == "revoked":
            errors.append(f"{name}: revoked at the issuer, still in the store")
        elif status == "expiry_unknown_locally":
            errors.append(
                f"{name}: no expiry recorded locally, but the issuer says "
                f"{(facts or {}).get('expires_at')} — nothing can audit it until "
                f"that is written down"
            )
        elif status == "expiry_drift":
            errors.append(
                f"{name}: stored expiry {meta.get('expires_at')} disagrees with "
                f"the issuer's {(facts or {}).get('expires_at')}"
            )

        if apply_changes and status in APPLICABLE:
            try:
                # Metadata only. Never the value.
                store.update_metadata(name, expires_at=(facts or {}).get("expires_at"))
                applied.append(name)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: could not record the issuer's expiry: {exc}")

        if apply_changes and status in PROVEN_ALIVE:
            # The credential answered, so it works — even a revoked one that
            # still responds has proved the value is real. Record the heartbeat.
            try:
                store.update_metadata(name, last_verified_at=_now_iso())
                verified.append(name)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: could not record proof of life: {exc}")

    actionable = [f for f in findings if f["status"] in ACTIONABLE]
    return SkillResult(
        ok=not actionable,
        value={
            "checked": len(findings),
            "actionable": len(actionable),
            "findings": findings,
            "applied": applied,
            "verified": verified,
            "applied_changes": "metadata only (expiry); never a credential value",
        },
        actions_taken=[
            f"reconciled {len(findings)} credential(s) against their issuers"
        ] + ([f"recorded the issuer's expiry for {len(applied)}"] if applied else [])
          + ([f"recorded proof of life for {len(verified)}"] if verified else []),
        errors=errors,
    )


__all__ = ["run", "ACTIONABLE", "APPLICABLE", "PROVEN_ALIVE"]
