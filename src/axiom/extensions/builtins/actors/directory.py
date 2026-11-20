# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The read across: one row per actor, from where each actor's facts already live.

| kind | from |
|---|---|
| person | the gate's accounts (``$AXIOM_GATE_USERS_FILE``), their keys and pending invitations, and the directory's group tuples |
| agent | the agent roster (``axiom.agents.roster``) |
| node | this node, and the fleet console's nodes where it serves one (``AXIOM_TEAM_FLEET=1``) |

Nothing here writes. A source that is not configured contributes nothing and is
named in ``skipped``, so an empty Team says why rather than implying nobody is
here.

Tenancy: a person belongs to their account's site (an account with no site
belongs to the node's home site, ``AXIOM_SITE``), and is listed only when the
viewer's scope permits that site (:func:`axiom.infra.site_scope.resolve`, the one
decision). Agents and the node belong to the node and are listed for everyone
who may sign in here. Counts are taken after this, so a tenant never learns how
many people another tenant has.

**Team lists active agents only, and every one names an accountable person**
(MVP-23-A2). An agent type is dormant at a site until someone adds it; which are
active, and who answers for each, is the site's activation record
(:mod:`axiom.agents.activation`), with site configuration
(``AXIOM_AGENT_ACCOUNTABLE="keep=@ana:site-a,*=@ops:site-a"``) and the agent's
declaration (``accountable = "@name:context"``) kept wherever they name someone.
An active agent with nobody, or whose person lost access, is listed with an
``issue`` that carries its fix (assign someone).
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

from axiom.governance.roles import canonical_roles

KIND_WORD = {"person": "Person", "agent": "Agent", "node": "Node"}
#: The shape each kind is drawn with, so kind never rests on colour alone.
SHAPE = {"person": "circle", "agent": "diamond", "node": "square"}

NO_ACCOUNTABLE = "no accountable person: name one in the agent's declaration or AXIOM_AGENT_ACCOUNTABLE"


def _base(kind: str) -> dict:
    return {"kind": kind, "kind_word": KIND_WORD[kind], "shape": SHAPE[kind]}


def home_site() -> str | None:
    site = (os.environ.get("AXIOM_SITE") or "").strip()
    return site or None


def person_handle(user_id: str, email: str, site: str | None) -> str:
    """The handle a person's keys and sessions carry, by the gate's own rule."""
    from axiom.extensions.builtins.webgate.api.self_key_route import _principal_of

    handle = _principal_of({"sub": user_id, "email": email, "site": site or ""})
    if handle:
        return handle
    from axiom.webauth.users import _handle_name

    name = _handle_name(user_id)
    return f"@{name}:{site}" if site else f"@{name}"


_LINKS_CACHE: dict[tuple, dict[str, list[dict]]] = {}


def _link_index() -> dict[str, list[dict]]:
    """Every handle an account answers to, mapped to the accounts that answer to it.

    Two handles per account, both derived from the gate's own account record:
    the one a browser session reaches the decision point as (the authz edge's
    ``session_handle`` of the session's verified ``sub``, which is the account's
    id) and the account's Team handle (:func:`person_handle`, what a share by
    email or a key names). Cached by the accounts file's signature.
    """
    from axiom.extensions.builtins.http.authz_hook import SESSION_CONTEXT_ENV, session_handle
    from axiom.webauth.live import accounts_path, is_suspended

    path = accounts_path()
    if path is None or not path.is_file():
        return {}
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size, os.environ.get(SESSION_CONTEXT_ENV, ""), home_site())
    cached = _LINKS_CACHE.get(key)
    if cached is not None:
        return cached
    index: dict[str, list[dict]] = {}
    home = home_site()
    for a in _accounts():
        if a.get("disabled") or is_suspended(a.get("attributes")):
            continue
        account = {"user_id": a["user_id"], "site": a.get("site") or home}
        for handle in {session_handle(a["user_id"]), person_handle(a["user_id"], a["email"], a.get("site"))}:
            index.setdefault(handle, []).append(account)
    _LINKS_CACHE.clear()
    _LINKS_CACHE[key] = index
    return index


def linked_handles(principal: str, site: str) -> tuple[str, ...]:
    """The other handles the gate's account record links to ``principal`` at ``site`` (ADR-263 D9).

    A link is the account record itself, never a likeness: ``principal`` must be
    a handle exactly one account answers to, that account must belong to
    ``site``, and each linked handle must also name that one account and no
    other. Anything else (no account, two accounts, another site, a suspended
    or disabled account) links nothing, so access can only narrow to the
    principal as presented.
    """
    try:
        index = _link_index()
    except Exception:  # noqa: BLE001 - an unreadable accounts file links nobody
        return ()
    owners = index.get((principal or "").strip()) or []
    if len(owners) != 1 or owners[0]["site"] != site:
        return ()
    uid = owners[0]["user_id"]
    return tuple(sorted(
        h for h, accts in index.items()
        if h != principal and len(accts) == 1 and accts[0]["user_id"] == uid
    ))


def handle_holders(handle: str) -> int:
    """How many live accounts answer to ``handle`` (0, 1, or more: a collision)."""
    try:
        return len(_link_index().get(handle) or [])
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# people
# ---------------------------------------------------------------------------


def _accounts() -> list[dict]:
    from axiom.webauth import load_user_records
    from axiom.webauth.live import accounts_path

    path = accounts_path()
    if path is None or not path.is_file():
        raise LookupError("no accounts file on this node ($AXIOM_GATE_USERS_FILE)")
    return load_user_records(path)


def _keys_by_holder() -> dict[str, dict]:
    from axiom.webauth import load_api_key_records, load_last_used

    raw = (os.environ.get("AXIOM_GATE_API_KEYS_FILE") or "").strip()
    if not raw or not Path(raw).is_file():
        return {}
    path = Path(raw)
    try:
        records = load_api_key_records(path)
        used = load_last_used(path)
    except Exception:  # noqa: BLE001 - keys are a detail of a row, not a reason to fail it
        return {}
    out: dict[str, dict] = {}
    for r in records:
        if r.get("revoked_at"):
            continue
        slot = out.setdefault(r["principal"], {"active": 0, "last_used_at": None})
        slot["active"] += 1
        last = used.get(r["key_id"])
        if last and (slot["last_used_at"] is None or last > slot["last_used_at"]):
            slot["last_used_at"] = last
    return out


def _groups_by_subject() -> dict[str, list[str]]:
    from axiom.extensions.builtins.directory.reconcile import (
        GROUP_PREFIX,
        MEMBER_RELATION,
        USER_PREFIX,
    )

    raw = (os.environ.get("AXIOM_DIRECTORY_TUPLE_STORE") or "").strip()
    if not raw.startswith("json:"):
        # Only the file store is read here: a page load must not become a call to
        # a remote authorization service.
        return {}
    from axiom.extensions.builtins.directory.stores import resolve_tuple_store

    store = resolve_tuple_store()
    out: dict[str, list[str]] = {}
    for user, relation, obj in store.all_tuples():
        if relation == MEMBER_RELATION and user.startswith(USER_PREFIX) and obj.startswith(GROUP_PREFIX):
            out.setdefault(user[len(USER_PREFIX):], []).append(obj[len(GROUP_PREFIX):])
    return {k: sorted(v) for k, v in out.items()}


def _people(scope, skipped: dict[str, str]) -> list[dict]:
    from axiom.webauth.live import is_suspended

    try:
        accounts = _accounts()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        skipped["people"] = str(exc)
        accounts = []
    keys = _keys_by_holder()
    try:
        groups = _groups_by_subject()
    except Exception as exc:  # noqa: BLE001
        skipped["directory"] = str(exc)
        groups = {}
    home = home_site()
    rows: list[dict] = []
    seen: set[str] = set()
    for a in accounts:
        site = a.get("site") or home
        if not _visible(site, scope):
            continue
        handle = person_handle(a["user_id"], a["email"], a.get("site"))
        seen.update({a["user_id"], a["email"]})
        rows.append({
            **_base("person"),
            "handle": handle,
            "name": a.get("name") or a["email"].split("@", 1)[0],
            "roles": list(canonical_roles(a.get("roles") or ())),
            "site": site,
            "contact": ["email"] if a.get("email") else [],
            "status": "suspended" if is_suspended(a.get("attributes")) else "active",
            "groups": groups.get(a["user_id"]) or groups.get(a["email"]) or [],
            "email": a["email"],
            "user_id": a["user_id"],
            "keys": keys.get(handle) or keys.get(handle.split(":", 1)[0])
            or {"active": 0, "last_used_at": None},
            "source": "gate",
        })
    # Directory members with no account here yet: they are people at the site,
    # and an administrator invites them from this row.
    for subject, names in groups.items():
        if subject in seen or not _visible(home, scope):
            continue
        rows.append({
            **_base("person"),
            "handle": person_handle(subject, subject if "@" in subject else "", home),
            "name": subject.split("@", 1)[0],
            "roles": [],
            "site": home,
            "contact": ["email"] if "@" in subject else [],
            "status": "no account",
            "groups": names,
            "source": "directory",
        })
    return rows


def _invited(scope, skipped: dict[str, str]) -> list[dict]:
    from axiom.extensions.builtins.webgate.skills.invite import resolve_invitations_path
    from axiom.webauth.invitations import list_invitations

    path = resolve_invitations_path({})
    if path is None or not path.is_file():
        return []
    try:
        records = list_invitations(path)
    except Exception as exc:  # noqa: BLE001
        skipped["invitations"] = str(exc)
        return []
    rows = []
    for r in records:
        if r.get("status") != "outstanding":
            continue
        site = r.get("site") or home_site()
        if not _visible(site, scope):
            continue
        rows.append({
            **_base("person"),
            "handle": str(r.get("principal") or ""),
            "name": r.get("name") or str(r.get("principal") or "").lstrip("@").split(":")[0],
            "roles": list(canonical_roles(r.get("roles") or ())),
            "site": site,
            "contact": [],
            "status": "invited",
            "invitation": {"id": r["invitation_id"], "expires_at": r.get("expires_at"),
                           "invited_by": r.get("invited_by") or ""},
            "source": "invitation",
        })
    return rows


# ---------------------------------------------------------------------------
# agents and nodes
# ---------------------------------------------------------------------------


def accountable_map() -> dict[str, str]:
    from axiom.agents.activation import accountable_map as _map

    return _map()


def _display_names(accounts: list[dict]) -> dict[str, str]:
    """Every account's Team handle, mapped to the name Team shows for it."""
    out: dict[str, str] = {}
    for a in accounts:
        name = a.get("name") or a["email"].split("@", 1)[0]
        out[person_handle(a["user_id"], a["email"], a.get("site"))] = name
    return out


def _agents(skipped: dict[str, str], records: Any = None) -> list[dict]:
    """The agents on Team: active ones only (``axiom.agents.activation``).

    A dormant agent type is not listed (it is offered under New agent). With no
    activation record yet (a node with no gate, or a site not yet started under
    this release) the agents the site configured by name are listed, as before.
    """
    from axiom.agents import activation

    if records is None:
        try:
            from axiom.agents.roster import load_roster

            records = load_roster()
        except Exception as exc:  # noqa: BLE001
            skipped["agents"] = str(exc)
            return []
    named = accountable_map()
    record = activation.load()
    try:
        accounts = _accounts()
    except Exception:  # noqa: BLE001 - no accounts names nobody
        accounts = []
    names = _display_names(accounts)
    rows = []
    for r in records:
        active = activation.is_active(r.handle, record) if record is not None else None
        if active is None:
            # Before this site's record exists, as before: what it configured.
            active = bool(activation.configured_accountable(r, named))
        if not active:
            continue
        who, source, entry = activation.accountable_for(r, record, named)
        row = {
            **_base("agent"),
            "handle": f"@{r.handle}",
            "name": r.display,
            "roles": [r.role] if r.role else [],
            "site": None,
            "about": r.domain or r.description,
            "accountable": who or None,
            "accountable_name": names.get(who) if who else None,
            "accountable_source": source or None,
            "contact": ["ask"],
            "status": "running",
            "source": "roster",
        }
        if not who:
            row["finding"] = NO_ACCOUNTABLE
            row["issue"] = _issue("no-accountable", r.display, "")
        elif source == "record" and entry is not None:
            state = activation.account_state(entry, accounts)
            if state != "live":
                row["issue"] = _issue(f"accountable-{state}", r.display, names.get(who) or who)
                row["finding"] = row["issue"]["summary"]
        rows.append(row)
    return rows


def _issue(code: str, agent: str, who: str) -> dict:
    """An abnormal state with its fix: the summary says what, the action says what to do."""
    summary = {
        "no-accountable": f"Nobody answers for {agent}.",
        "accountable-suspended": f"{who} answers for {agent} but is suspended, so cannot act for it.",
        "accountable-removed": f"{who} answered for {agent} but no longer has an account here.",
    }[code]
    return {"code": code, "summary": summary,
            "action": {"id": "assign", "label": "Assign someone"}}


def _nodes(scope, skipped: dict[str, str]) -> list[dict]:
    from axiom.infra import site_scope

    name = (os.environ.get("AXIOM_FLEET_NODE_ID") or os.environ.get("AXIOM_NODE_NAME")
            or socket.gethostname() or "this-node").strip()
    served = site_scope.deployment_sites()
    serves = sorted(s for s in (served or ()) if scope.permits(s))
    if not serves and home_site() and _visible(home_site(), scope):
        serves = [home_site()]
    rows = [{
        **_base("node"),
        "handle": f"@{name}:{home_site()}" if home_site() else f"@{name}",
        "name": name,
        "roles": ["node"],
        "site": home_site(),
        "serves": serves,
        "contact": [],
        "status": "serving",
        "source": "this node",
    }]
    if (os.environ.get("AXIOM_TEAM_FLEET") or "").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            from axiom.extensions.builtins.fleet.skills import status as fleet_status

            for n in fleet_status.run({}).value.get("nodes", []):
                site = n.get("site")
                if not _visible(site, scope) or n.get("node_id") == name:
                    continue
                rows.append({**_base("node"), "handle": f"@{n.get('node_id')}:{site}",
                             "name": n.get("node_id"), "roles": ["node"], "site": site,
                             "serves": [site] if site else [], "contact": [],
                             "status": n.get("rollup") or n.get("status") or "unknown",
                             "source": "fleet"})
        except Exception as exc:  # noqa: BLE001
            skipped["fleet"] = str(exc)
    return rows


def _visible(site: str | None, scope) -> bool:
    if not site:
        return bool(scope.unbounded)
    return scope.permits(site)


def build_rows(*, agents: Any = None) -> tuple[list[dict], dict[str, str]]:
    """Every actor the current caller's scope permits, unprojected, and what was skipped."""
    from axiom.infra import site_scope

    scope = site_scope.resolve()
    skipped: dict[str, str] = {}
    rows = _people(scope, skipped) + _invited(scope, skipped)
    rows += _agents(skipped, agents) + _nodes(scope, skipped)
    order = {"person": 0, "agent": 1, "node": 2}
    rows.sort(key=lambda r: (order[r["kind"]], str(r.get("name") or "").lower()))
    return rows, skipped


def find(handle: str, *, agents: Any = None) -> dict | None:
    want = (handle or "").strip()
    if want and not want.startswith("@"):
        want = "@" + want
    rows, _ = build_rows(agents=agents)
    return next((r for r in rows if r["handle"] == want), None)


#: How long the name map is reused: a Timeline page or a streamed row reads names on every call,
#: and the read across (accounts, roster, nodes) is the costly part. A renamed person shows within it.
NAMES_TTL_SECONDS = 30.0
_NAMES_CACHE: dict[str, Any] = {"at": None, "by": {}}


def _names(agents: Any = None) -> dict[str, str]:
    import time

    now = time.monotonic()
    at = _NAMES_CACHE["at"]
    if agents is None and at is not None and now - at < NAMES_TTL_SECONDS:
        return _NAMES_CACHE["by"]
    rows, _ = build_rows(agents=agents)
    by = {r["handle"]: str(r.get("name") or "") for r in rows if r.get("name")}
    if agents is None:
        _NAMES_CACHE.update(at=now, by=by)
    return by


def names_by_handle(handles: Any, *, agents: Any = None) -> dict[str, str]:
    """The names Team shows for these handles, for any surface that would otherwise show a handle.

    One read across for the lot, within the caller's scope like :func:`build_rows`. A handle with a
    context the roster does not carry (``@helper:site-a`` for the agent ``@helper``) takes the bare
    handle's name. A handle nobody here answers to is left out: the caller shows the handle."""
    wanted = {str(h) for h in handles or () if h}
    if not wanted:
        return {}
    by = _names(agents)
    out: dict[str, str] = {}
    for h in wanted:
        name = by.get(h) or by.get(h.split(":", 1)[0])
        if name:
            out[h] = name
    return out


__all__ = ["KIND_WORD", "NO_ACCOUNTABLE", "SHAPE", "accountable_map", "build_rows", "find",
           "names_by_handle", "person_handle"]
