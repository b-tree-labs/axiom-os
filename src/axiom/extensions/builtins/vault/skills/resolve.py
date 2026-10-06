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
from urllib.parse import urlsplit

from axiom.infra.skills import SkillContext, SkillResult, ensure_context

#: Purpose hints, matched against a credential's recorded metadata and notes.
#: Deliberately a small, explicit table rather than free-text search: a fuzzy
#: match that picks the wrong token is the failure this verb exists to prevent.
_PURPOSE_PATTERNS: dict[str, tuple[str, ...]] = {
    "git": ("read_repository", "write_repository", "git-only", "clone", "mirror"),
    "api": ("api scope", " api ", "api,", "automation", "self_rotate"),
    "registry": ("packaging", "upload", "pypi", "artifacts"),
    "runner": ("create_runner", "runner"),
}


def _host_of(meta: dict[str, Any]) -> str:
    """The host a credential belongs to, from whichever field carries it."""
    host = str(meta.get("git_host") or "").strip()
    if host:
        return host
    issuer = str(meta.get("issuer_url") or "").strip()
    return urlsplit(issuer).hostname or ""


def _purposes(meta: dict[str, Any]) -> list[str]:
    """Purposes a credential's own metadata declares or plainly implies.

    ``git_host`` being set IS a declaration — it is the field the git credential
    helper keys on. Everything else is read from the notes, and a credential
    whose notes say nothing gets an empty list rather than a guess.
    """
    found: list[str] = []
    if str(meta.get("git_host") or "").strip():
        found.append("git")
    haystack = f" {str(meta.get('notes') or '').lower()} "
    for purpose, needles in _PURPOSE_PATTERNS.items():
        if purpose in found:
            continue
        if any(needle in haystack for needle in needles):
            found.append(purpose)
    return found


def _health(meta: dict[str, Any], *, now: Any = None) -> str:
    """``ok`` / ``expiring`` / ``expired`` / ``no_expiry`` for one credential."""
    from datetime import UTC, datetime

    raw = str(meta.get("expires_at") or "").strip()
    if not raw:
        # NOT "ok". Nothing can ever flag it, which is how one of these expired
        # unnoticed in the first place.
        return "no_expiry"
    when = datetime.now(UTC) if now is None else now
    try:
        expiry = datetime.fromisoformat(raw)
    except ValueError:
        return "no_expiry"
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if expiry <= when:
        return "expired"
    return "expiring" if (expiry - when).days <= 14 else "ok"


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


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Resolve the right credential for a host, or explain why it cannot."""
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    ctx = ensure_context(ctx)
    store = params.get("_store") or ForeignCredentialStore(ctx.state_dir)

    host = str(params.get("host") or "").strip().lower()
    purpose = str(params.get("purpose") or "").strip().lower()
    if not host:
        return SkillResult(ok=False, errors=["host is required"])

    everything = store.list()
    hosts = sorted({h for h in (_host_of(m) for m in everything) if h})

    candidates = []
    for meta in everything:
        if _host_of(meta).lower() != host:
            continue
        candidates.append({
            "name": meta.get("name", ""),
            "provider": meta.get("provider", ""),
            "purposes": _purposes(meta),
            "health": _health(meta, now=params.get("_now")),
            "expires_at": meta.get("expires_at"),
            # None until probed: "not tested" is a third state, distinct from
            # working and from broken.
            "alive": None,
            "probe_detail": "",
            "_meta": meta,
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
        return SkillResult(
            ok=False,
            value={**value, "known_hosts": hosts},
            errors=[
                f"no credential recorded for {host!r}. Known hosts: "
                + (", ".join(hosts) or "(none)")
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
                + ". Label one with `axi secrets rotate <name> --notes ...`, "
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
