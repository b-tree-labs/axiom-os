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

**Every agent names an accountable person** (MVP-23-A2): from its roster
declaration (``accountable = "@name:context"``), else site configuration
(``AXIOM_AGENT_ACCOUNTABLE="keep=@ana:site-a,*=@ops:site-a"``, where ``*`` is the
default). An agent with neither is still listed, with a finding.
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
    raw = os.environ.get("AXIOM_AGENT_ACCOUNTABLE") or ""
    out: dict[str, str] = {}
    for part in raw.split(","):
        agent, _, person = part.partition("=")
        if agent.strip() and person.strip().startswith("@"):
            out[agent.strip().lstrip("@")] = person.strip()
    return out


def _agents(skipped: dict[str, str], records: Any = None) -> list[dict]:
    if records is None:
        try:
            from axiom.agents.roster import load_roster

            records = load_roster()
        except Exception as exc:  # noqa: BLE001
            skipped["agents"] = str(exc)
            return []
    named = accountable_map()
    rows = []
    for r in records:
        who = named.get(r.handle) or getattr(r, "accountable", "") or named.get("*") or ""
        row = {
            **_base("agent"),
            "handle": f"@{r.handle}",
            "name": r.display,
            "roles": [r.role] if r.role else [],
            "site": None,
            "about": r.domain or r.description,
            "accountable": who or None,
            "contact": ["ask"],
            "status": "running",
            "source": "roster",
        }
        if not who:
            row["finding"] = NO_ACCOUNTABLE
        rows.append(row)
    return rows


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


__all__ = ["KIND_WORD", "NO_ACCOUNTABLE", "SHAPE", "accountable_map", "build_rows", "find",
           "person_handle"]
