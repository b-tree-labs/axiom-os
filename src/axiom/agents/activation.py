# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which agents are active at a site, and who answers for each: one record.

Every agent type the platform ships is **dormant** at a site until someone adds
it. A dormant agent is not on Team and takes no model turns there (it cannot be
asked or addressed), but nothing deterministic stops: its manifest, its tools,
the roster that tells an agent from a person, its background duties under the
operator's consent (``agents_consent.json``) and GUARD's decisions all carry on
exactly as before. Dormancy is about Team and the agent's model work only.

**The record** sits beside the gate's accounts file (``$AXIOM_GATE_USERS_FILE``),
because it belongs to the same site the accounts do: ``agent-activation.json``::

    {"version": 1,
     "agents": {"axi": {"active": true, "accountable": "@ana:site-a",
                        "account": "ana@example.org", "how": "site-start",
                        "at": "...", "by": "@axiom:site-start"}}}

``accountable`` is the person who answers for the agent; ``account`` is the gate
account that handle stood for when it was recorded, so a person who loses access
(suspended, disabled or removed) is noticed rather than silently kept.

**Who answers for an agent**, first match wins:

1. site configuration for that agent (``AXIOM_AGENT_ACCOUNTABLE="keep=@ana:site-a"``);
2. this record (the person who added it, or the one an administrator assigned);
3. the agent's own declaration (``accountable = "@name:context"`` in its manifest);
4. the site's configured default (``AXIOM_AGENT_ACCOUNTABLE="*=@ops:site-a"``).

Nothing here overwrites 1 or 3: the record is only written for an agent whose
accountable person is not configured.

**Site start** (:func:`ensure_site_record`) is idempotent and runs when the
first account is created and each time a node starts serving. With no record yet
it writes one: the assistant (``axi``, it answers chat) and every agent the site
already names in its configuration (``AXIOM_AGENT_ACCOUNTABLE``, so someone set
it up to work here) start active, the rest dormant. Any active agent with
nobody accountable gets the oldest live site administrator. Each write is one
entry on the action chain (``axiom.infra.audit_trail``).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RECORD_NAME = "agent-activation.json"
VERSION = 1

#: Active on a new site: the assistant answers chat, so it is already working.
STARTS_ACTIVE: tuple[str, ...] = ("axi",)

#: The principal that site start writes as on the action chain.
SITE_START = "@axiom:site-start"


class ActivationError(ValueError):
    """A change the record refuses, in a sentence a person can act on."""


# ---------------------------------------------------------------------------
# where it lives
# ---------------------------------------------------------------------------


def record_path(accounts_file: Path | None = None) -> Path | None:
    """``agent-activation.json`` beside the accounts file, or None with no gate."""
    from axiom.webauth.live import accounts_path

    accounts = Path(accounts_file) if accounts_file else accounts_path()
    return accounts.with_name(RECORD_NAME) if accounts else None


def load(path: Path | None = None) -> dict | None:
    """The record, or None when this site has none yet (or no gate at all)."""
    p = path or record_path()
    if p is None or not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("agents"), dict):
        return None
    return data


def _save(path: Path, data: dict) -> None:
    from axiom.infra.state_integrity import atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------


def _accounts(accounts_file: Path | None = None) -> list[dict]:
    from axiom.webauth import load_user_records
    from axiom.webauth.live import accounts_path

    path = Path(accounts_file) if accounts_file else accounts_path()
    if path is None or not path.is_file():
        return []
    try:
        return load_user_records(path)
    except Exception:  # noqa: BLE001 - an unreadable accounts file has nobody in it
        return []


def _live(account: dict) -> bool:
    from axiom.webauth.live import is_suspended

    return not account.get("disabled") and not is_suspended(account.get("attributes"))


def _is_admin(account: dict) -> bool:
    from axiom.extensions.builtins.actors.visibility import ADMIN, viewer_class

    return viewer_class(account.get("roles") or ()) == ADMIN


def handle_of(account: dict) -> str:
    """The handle Team shows for ``account`` (the gate's own rule)."""
    from axiom.extensions.builtins.actors.directory import person_handle

    return person_handle(account["user_id"], account["email"], account.get("site"))


def oldest_admin(accounts: list[dict] | None = None) -> dict | None:
    """The first live site administrator in the accounts file (it is append-ordered)."""
    for a in accounts if accounts is not None else _accounts():
        if _live(a) and _is_admin(a):
            return a
    return None


def find_account(who: str, accounts: list[dict] | None = None) -> dict | None:
    """An account by email, user id or Team handle; None when nobody matches."""
    want = (who or "").strip()
    if not want:
        return None
    for a in accounts if accounts is not None else _accounts():
        if want.lower() in {a["email"], str(a["user_id"]).lower()} or want == handle_of(a):
            return a
    return None


def account_state(entry: dict, accounts: list[dict] | None = None) -> str:
    """``live``, ``suspended`` or ``removed`` for the account a record entry names."""
    who = str(entry.get("account") or "")
    if not who:
        return "live"
    acct = next((a for a in (accounts if accounts is not None else _accounts())
                 if who in {a["email"], a["user_id"]}), None)
    if acct is None:
        return "removed"
    return "live" if _live(acct) else "suspended"


# ---------------------------------------------------------------------------
# who answers for an agent
# ---------------------------------------------------------------------------


def accountable_map() -> dict[str, str]:
    """``AXIOM_AGENT_ACCOUNTABLE``, as {agent: handle}; ``*`` is the site default."""
    raw = os.environ.get("AXIOM_AGENT_ACCOUNTABLE") or ""
    out: dict[str, str] = {}
    for part in raw.split(","):
        agent, _, person = part.partition("=")
        if agent.strip() and person.strip().startswith("@"):
            out[agent.strip().lstrip("@")] = person.strip()
    return out


def configured_accountable(agent: Any, named: dict[str, str] | None = None) -> str:
    """The person site configuration or the agent's declaration names, else ``""``."""
    named = accountable_map() if named is None else named
    return named.get(agent.handle) or str(getattr(agent, "accountable", "") or "")


def accountable_for(agent: Any, record: dict | None, named: dict[str, str] | None = None
                    ) -> tuple[str, str, dict | None]:
    """``(handle, source, entry)``: who answers for ``agent``, from where, and its entry.

    ``source`` is ``site``, ``record``, ``declaration``, ``site-default`` or ``""``.
    """
    named = accountable_map() if named is None else named
    entry = ((record or {}).get("agents") or {}).get(agent.handle)
    if named.get(agent.handle):
        return named[agent.handle], "site", entry
    if entry and entry.get("accountable"):
        return str(entry["accountable"]), "record", entry
    if getattr(agent, "accountable", ""):
        return str(agent.accountable), "declaration", entry
    if named.get("*"):
        return named["*"], "site-default", entry
    return "", "", entry


def is_active(handle: str, record: dict | None = None) -> bool | None:
    """Whether ``handle`` is active here; None when the site has no record yet.

    None leaves the caller to keep its behaviour from before this record: a
    node with no gate, or a site that has not started under this release.
    """
    rec = record if record is not None else load()
    if rec is None:
        return None
    entry = rec["agents"].get(handle)
    return bool(entry and entry.get("active"))


# ---------------------------------------------------------------------------
# the audit entry
# ---------------------------------------------------------------------------


def _audit(skill: str, agent: str, old: Any, new: Any, *, by: str) -> None:
    """One action-chain entry for a write made outside a skill invocation."""
    from axiom.infra import audit_trail
    from axiom.infra.paths import get_user_state_dir

    try:
        audit_trail.record_invocation(
            state_dir=get_user_state_dir(), skill=skill, params={"agent": agent},
            principal=by, site=os.environ.get("AXIOM_SITE") or None, outcome="ok",
            errors_count=0,
            change={"kind": "agent", "subject": f"@{agent}", "old": old, "new": new},
        )
    except Exception:  # noqa: BLE001 - the chain's own isolation rule
        pass


def _note(agent: str, old: Any, new: Any) -> None:
    """Inside a skill: the change note the chokepoint's one audit entry carries."""
    from axiom.infra import audit_trail

    audit_trail.note_change(kind="agent", subject=f"@{agent}", old=old, new=new)


def _view(entry: dict | None) -> dict | None:
    if not entry:
        return None
    return {"active": bool(entry.get("active")), "accountable": entry.get("accountable") or ""}


# ---------------------------------------------------------------------------
# site start: bootstrap and upgrade, one idempotent step
# ---------------------------------------------------------------------------


def _roster(roster: Any) -> list[Any]:
    if roster is not None:
        return list(roster)
    from axiom.agents.roster import load_roster

    return load_roster()


def ensure_site_record(*, roster: Any = None, accounts_file: Path | None = None) -> list[str]:
    """Write the site's record if it has none, and give active agents someone to answer.

    Safe to run any number of times: a second run changes nothing. Returns one
    sentence per change. Does nothing on a node with no gate accounts file, or
    while the site has no live administrator to answer for anything.
    ``accounts_file`` names a site's accounts file other than this process's.
    """
    path = record_path(accounts_file)
    if path is None:
        return []
    accounts = _accounts(accounts_file)
    admin = oldest_admin(accounts)
    if admin is None:
        return []
    agents = _roster(roster)
    named = accountable_map()
    record = load(path)
    changes: list[str] = []
    fresh = record is None
    if fresh:
        record = {"version": VERSION, "agents": {}}
        for a in agents:
            if a.handle in STARTS_ACTIVE or named.get(a.handle):
                record["agents"][a.handle] = {"active": True, "how": "site-start",
                                              "at": _now(), "by": SITE_START}
    admin_handle = handle_of(admin)
    for a in agents:
        entry = record["agents"].get(a.handle)
        if not entry or not entry.get("active"):
            continue
        who, _source, _ = accountable_for(a, record, named)
        if who:
            continue
        before = None if fresh else _view(entry)
        entry.update({"accountable": admin_handle, "account": admin["email"],
                      "accountable_how": "oldest-admin", "accountable_at": _now()})
        _audit("agents.site_start", a.handle, before, _view(entry), by=SITE_START)
        changes.append(f"{a.display} is active; {admin_handle} answers for it (the oldest site administrator)")
    if fresh:
        for a in agents:
            entry = record["agents"].get(a.handle)
            if entry and not entry.get("accountable"):
                _audit("agents.site_start", a.handle, None, _view(entry), by=SITE_START)
                changes.append(f"{a.display} is active; its configured person answers for it")
        _save(path, record)
        changes.append(f"recorded which agents are active at this site in {path.name}")
    elif changes:
        _save(path, record)
    return changes


# ---------------------------------------------------------------------------
# the verbs: add (activate), deactivate, assign
# ---------------------------------------------------------------------------


def _agent(handle: str, roster: Any) -> Any:
    key = (handle or "").strip().lstrip("@").lower()
    for a in _roster(roster):
        if a.handle == key:
            return a
    raise ActivationError(f"no agent type @{key} here; agents.catalog lists them")


def _record_for_write(roster: Any) -> tuple[Path, dict]:
    path = record_path()
    if path is None:
        raise ActivationError("this node has no gate accounts file ($AXIOM_GATE_USERS_FILE), "
                              "so it has no site to add agents to")
    record = load(path)
    if record is None:
        ensure_site_record(roster=roster)
        record = load(path) or {"version": VERSION, "agents": {}}
    return path, record


def activate(handle: str, *, by: str, roster: Any = None) -> dict:
    """Add agent ``handle`` to Team; the person adding it answers for it.

    ``by`` is the adder's email, user id or handle and must be a live account.
    An accountable person named in configuration or the agent's declaration is
    kept, never replaced.
    """
    agent = _agent(handle, roster)
    accounts = _accounts()
    adder = find_account(by, accounts)
    if adder is None or not _live(adder):
        raise ActivationError(f"{by or 'nobody'} has no live account here, so cannot answer for an agent")
    path, record = _record_for_write(roster)
    entry = record["agents"].get(agent.handle) or {}
    if entry.get("active"):
        raise ActivationError(f"{agent.display} is already on Team")
    before = _view(entry)
    entry = {**entry, "active": True, "how": "added", "at": _now(), "by": handle_of(adder)}
    entry.pop("deactivated_at", None)
    entry.pop("deactivated_by", None)
    if not configured_accountable(agent):
        entry.update({"accountable": handle_of(adder), "account": adder["email"],
                      "accountable_how": "added", "accountable_at": _now()})
    record["agents"][agent.handle] = entry
    _save(path, record)
    _note(agent.handle, before, _view(entry))
    who, _, _ = accountable_for(agent, record)
    return {"agent": f"@{agent.handle}", "name": agent.display, "active": True, "accountable": who}


def deactivate(handle: str, *, by: str, roster: Any = None) -> dict:
    """Return agent ``handle`` to dormant. Its history and its record entry stay."""
    agent = _agent(handle, roster)
    path, record = _record_for_write(roster)
    entry = record["agents"].get(agent.handle)
    if not entry or not entry.get("active"):
        raise ActivationError(f"{agent.display} is not on Team; there is nothing to deactivate")
    before = _view(entry)
    entry.update({"active": False, "deactivated_at": _now(), "deactivated_by": by})
    _save(path, record)
    _note(agent.handle, before, _view(entry))
    return {"agent": f"@{agent.handle}", "name": agent.display, "active": False}


def assign(handle: str, person: str, *, by: str, roster: Any = None) -> dict:
    """Make ``person`` (email, user id or handle of a live account) answer for ``handle``."""
    agent = _agent(handle, roster)
    named = accountable_map()
    if named.get(agent.handle):
        raise ActivationError(
            f"{agent.display}'s accountable person is set in site configuration "
            f"(AXIOM_AGENT_ACCOUNTABLE); change it there")
    accounts = _accounts()
    acct = find_account(person, accounts)
    if acct is None or not _live(acct):
        raise ActivationError(f"{person or 'nobody'} has no live account here; name someone who can sign in")
    path, record = _record_for_write(roster)
    entry = record["agents"].get(agent.handle)
    if not entry or not entry.get("active"):
        raise ActivationError(f"{agent.display} is not on Team; add it first")
    before = _view(entry)
    entry.update({"accountable": handle_of(acct), "account": acct["email"],
                  "accountable_how": "assigned", "accountable_at": _now(), "accountable_by": by})
    _save(path, record)
    _note(agent.handle, before, _view(entry))
    return {"agent": f"@{agent.handle}", "name": agent.display, "accountable": handle_of(acct)}


def catalog(roster: Any = None) -> list[dict]:
    """Every agent type here, with whether it is on Team and what adding it does."""
    record = load()
    out = []
    for a in _roster(roster):
        active = is_active(a.handle, record)
        if active is None:
            active = a.handle in STARTS_ACTIVE or bool(accountable_map().get(a.handle))
        out.append({
            "handle": f"@{a.handle}",
            "name": a.display,
            "role": a.role,
            "does": a.domain or a.description,
            "asks": list(a.asks),
            "origin": "built-in" if a.extension else "installed",
            "active": active,
        })
    return out


def dormant_refusal(handle: str) -> str | None:
    """Why ``handle`` may not take a model turn here, or None when it may."""
    key = (handle or "").strip().lstrip("@").lower()
    active = is_active(key)
    if active is None or active:
        return None
    return (f"@{key} is not on Team at this site, so it takes no requests; "
            f"an administrator adds it from Team (New agent)")


__all__ = [
    "RECORD_NAME",
    "SITE_START",
    "STARTS_ACTIVE",
    "ActivationError",
    "account_state",
    "accountable_for",
    "accountable_map",
    "activate",
    "assign",
    "catalog",
    "configured_accountable",
    "deactivate",
    "dormant_refusal",
    "ensure_site_record",
    "find_account",
    "handle_of",
    "is_active",
    "load",
    "oldest_admin",
    "record_path",
]
