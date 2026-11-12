# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which credential, of the several this host has?

A host commonly carries more than one credential with different scopes — a
git-only token for pushes, an API token for automation, per-project tokens for
clones. Git's credential-helper protocol is keyed by **host alone**, so it can
only ever hand back one of them, chosen by whatever the store happened to
return first.

That silent pick cost a session: the helper returned the git-scoped token when
the API-scoped one was needed, the call 401'd, and the conclusion drawn was
"there is no working credential for this host" while a healthy one sat in the
store under a different name.

This verb answers the question the helper cannot: *which* credential, and is it
alive. It reports every candidate with its scope hint and expiry health, names
the one the git helper would silently return, and **refuses to choose** when the
requested purpose matches nothing — because returning a best guess is exactly
how the original mistake happened.

It never returns a credential value, which is what makes it safe to expose on
chat, MCP and in a transcript.
"""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.vault.index import (
    PURPOSE_PATTERNS,
    CredentialIndex,
    derive_health,
    derive_host,
    derive_purposes,
)
from axiom.infra.skills import SkillContext, SkillResult, ensure_context

# Host, purpose and health are derived in ONE place, the credential index
# (ADR-218), so what resolve shows for a host is exactly what `vault list
# --host` shows. These names stay for callers that imported them.
_PURPOSE_PATTERNS = PURPOSE_PATTERNS
_host_of = derive_host
_purposes = derive_purposes


def _health(meta: dict[str, Any], *, now: Any = None) -> str:
    return derive_health(meta, now=now)


def _default_prober(name: str, meta: dict[str, Any]) -> tuple[bool | None, str]:
    """Ask the issuer whether this credential still works. Never logs the value.

    Returns ``(alive, detail)`` where ``alive`` may be ``None`` — "nothing here
    knows how to test this provider". ``None`` is NOT ``False``: reporting
    unknown as dead would recreate the original error from the other direction,
    declaring a healthy credential broken because no probe existed for it.
    """
    import json
    import urllib.error
    import urllib.request

    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
    from axiom.infra.paths import get_user_state_dir

    provider = str(meta.get("provider") or "")
    issuer = str(meta.get("issuer_url") or "").rstrip("/")
    if provider != "gitlab-pat" or not issuer:
        return None, f"no probe for provider {provider or 'unknown'!r}"

    try:
        secret = ForeignCredentialStore(get_user_state_dir()).get(name)
        value = getattr(secret, "value", secret)
        value = value.decode() if isinstance(value, bytes) else str(value)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not read from the store: {type(exc).__name__}"

    request = urllib.request.Request(
        f"{issuer}/api/v4/user", headers={"PRIVATE-TOKEN": value}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            who = json.load(response).get("username", "")
        return True, f"ok as {who}" if who else "ok"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 — a network failure is not a dead token
        return None, f"unreachable: {type(exc).__name__}"


def _cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Resolve the right credential for a host, or explain why it cannot."""
    ctx = ensure_context(ctx)
    index = CredentialIndex(state_dir=ctx.state_dir, store=params.get("_store"), now=params.get("_now"))

    host = str(params.get("host") or "").strip().lower()
    purpose = str(params.get("purpose") or "").strip().lower()
    if not host:
        return SkillResult(ok=False, errors=["host is required"])

    hosts = index.hosts()

    # Exactly the rows `vault list --host` shows: one reader, one derivation.
    candidates = []
    for row in index.for_host(host):
        candidates.append({
            "name": row.get("name", ""),
            "provider": row.get("provider", ""),
            "purposes": list(row["purposes"]),
            "health": row["health"],
            "expires_at": row.get("expires_at"),
            # None until probed: "not tested" is a third state, distinct from
            # working and from broken.
            "alive": None,
            "probe_detail": "",
            "_meta": row,
        })
    candidates.sort(key=lambda c: c["name"])

    # Probing is opt-in. The default stays offline so listing candidates never
    # costs a network round trip per credential — a check people stop running
    # is a check that does not exist.
    if params.get("probe"):
        prober = params.get("_prober") or _default_prober
        for candidate in candidates:
            alive, detail = prober(candidate["name"], candidate["_meta"])
            candidate["alive"] = alive
            candidate["probe_detail"] = detail

    for candidate in candidates:
        candidate.pop("_meta", None)

    # The git helper is host-keyed and returns whatever the store yields first
    # for that host. Naming it is the point: a caller can see the pick they
    # would otherwise get silently.
    git_pick = next(
        (c["name"] for c in candidates if "git" in c["purposes"]),
        candidates[0]["name"] if candidates else None,
    )
    alive = [c["name"] for c in candidates if c["alive"] is True]
    dead = [c["name"] for c in candidates if c["alive"] is False]
    if not params.get("probe"):
        any_alive: bool | None = None
    elif alive:
        any_alive = True
    elif dead and not [c for c in candidates if c["alive"] is None]:
        any_alive = False
    else:
        # Some candidates could not be tested, so "none work" is not a
        # conclusion the data supports.
        any_alive = None

    value = {
        "host": host,
        "purpose": purpose or None,
        "candidates": candidates,
        "git_helper_would_return": git_pick,
        "resolved": None,
        "alive": alive,
        "dead": dead,
        "any_alive": any_alive,
    }

    if not candidates:
        unhosted = index.without_host()
        return SkillResult(
            ok=False,
            value={**value, "known_hosts": hosts, "without_host": unhosted},
            errors=[
                f"no credential recorded for {host!r}. Known hosts: "
                + (", ".join(hosts) or "(none)")
                + (
                    f". These record no host, so no host can find them: {', '.join(unhosted)}; "
                    f"record one with `{_cli()} vault annotate <name> --host {host}`"
                    if unhosted else ""
                )
            ],
        )

    probe_errors: list[str] = []
    if params.get("probe"):
        if git_pick in dead and alive:
            probe_errors.append(
                f"{git_pick} is DEAD and it is what a git credential helper "
                f"returns for this host — but {', '.join(alive)} still "
                f"work(s). One credential failing is not this host failing."
            )
        elif any_alive is False:
            probe_errors.append(
                f"every credential for {host} failed: {', '.join(dead)}"
            )

    if not purpose:
        # No purpose asked, so nothing to resolve — the listing IS the answer,
        # and picking one here would reintroduce the silent choice.
        return SkillResult(
            ok=any_alive is not False,
            value=value,
            actions_taken=[f"{len(candidates)} credential(s) for {host}"],
            errors=probe_errors,
        )

    matches = [c for c in candidates if purpose in c["purposes"]]
    if len(matches) == 1:
        return SkillResult(
            ok=True, value={**value, "resolved": matches[0]["name"]},
            actions_taken=[f"{host} + {purpose} -> {matches[0]['name']}"],
        )

    if not matches:
        return SkillResult(
            ok=False, value=value,
            errors=[
                f"no credential for {host} declares purpose {purpose!r}. "
                f"Candidates: "
                + ", ".join(
                    f"{c['name']} ({'/'.join(c['purposes']) or 'purpose unlabelled'})"
                    for c in candidates
                )
                + f". Label one with `{_cli()} vault annotate <name> --notes ...`, "
                "or name it directly."
            ],
        )

    return SkillResult(
        ok=False, value=value,
        errors=[
            f"{len(matches)} credentials for {host} declare {purpose!r}: "
            + ", ".join(c["name"] for c in matches)
            + ". Ambiguous on purpose — name the one you mean rather than "
            "letting a helper choose."
        ],
    )


__all__ = ["run"]
