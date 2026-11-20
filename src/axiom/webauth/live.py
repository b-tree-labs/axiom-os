# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The account as it is now, not as it was when the person signed in.

A session is a signed token minted at sign-in, and it carried the account's roles
and site as they were at that moment for its whole twelve hours. So a role an
administrator removed, a site an account was moved to, or a clearance that changed
took effect only when the person signed in again, and a suspended person kept
working until the token expired. The only way to make a change bite was to make
people sign in again, which is the one thing a change must never do (MVP-14: no
forced re-auth).

This module is the other way round. A verified session is re-read against the
accounts file on every request (the file store re-parses only when the file
changes, so this is a ``stat`` per request), and the live record wins:

- roles, site, name and the account's attributes (clearance, qualifications and
  the rest of ABAC) come from the record, so a change takes effect on the next
  request without anyone signing out (MVP-07-A2);
- a suspended account's session answers as no session at all, at once, and the
  same session works again the moment the account is resumed (MVP-14-A3);
- an account the file does not hold keeps its signed claims, so a node whose
  sessions come from somewhere else, or a session minted before an account was
  recorded, is never logged out by this.

Keys follow the same rule: :func:`key_holder_suspended` is consulted by the key
store, so suspending a person refuses their keys at once and resuming restores the
same tokens.

Suspension is recorded inside the account's ``attributes`` (``suspended_at``,
``suspended_by``, ``suspended_reason``), which every release reads without
complaint. A new top-level field would have made the previous release refuse the
whole accounts file, so a rollback would have locked everyone out.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from .users import UserStore, installed_user_store

#: The accounts file the gate reads; the same variable ``axi gate`` writes through.
ACCOUNTS_ENV = "AXIOM_GATE_USERS_FILE"

#: The attribute that marks an account suspended (an ISO instant).
SUSPENDED_AT = "suspended_at"
SUSPENDED_BY = "suspended_by"
SUSPENDED_REASON = "suspended_reason"

#: Bookkeeping attributes that are not facts about the person, so they are not
#: carried into a session's attributes.
_NOT_ABAC = frozenset({"idp", "idp_subject", SUSPENDED_AT, SUSPENDED_BY, SUSPENDED_REASON})

_LOCK = threading.Lock()
_STORES: dict[str, Any] = {}
_INDEX: dict[str, tuple[tuple[int, int, int], dict]] = {}


def is_suspended(attributes: Any) -> bool:
    """Whether an account's attributes mark it suspended."""
    return isinstance(attributes, dict) and bool(attributes.get(SUSPENDED_AT))


def accounts_path() -> Path | None:
    raw = (os.environ.get(ACCOUNTS_ENV) or "").strip()
    return Path(raw) if raw else None


def account_store() -> UserStore | None:
    """The store that holds accounts as they are now, or ``None`` when there is none.

    The accounts file when the node names one (one cached store per path, so its
    parse cache survives between requests), else the process-wide store when
    something filled it. ``None`` means nothing here can say more than the token.
    """
    path = accounts_path()
    if path is not None:
        key = str(path)
        with _LOCK:
            store = _STORES.get(key)
            if store is None:
                from .file_store import JsonFileUserStore

                store = JsonFileUserStore(path)
                _STORES[key] = store
        return store
    # installed_user_store, not get_user_store: asking must never install the empty
    # default, or every session would then be checked against an empty store.
    store = installed_user_store()
    if store is None:
        return None
    try:
        return store if len(store) else None  # type: ignore[arg-type]
    except TypeError:
        return store


def abac_attributes(attributes: Any) -> dict:
    """The account's attributes that are facts about the person."""
    if not isinstance(attributes, dict):
        return {}
    return {k: v for k, v in attributes.items() if k not in _NOT_ABAC}


def live_session_claims(claims: dict | None) -> dict | None:
    """``claims`` brought up to date with the account, or ``None`` if it is suspended.

    Never raises: a store that cannot be read leaves the claims as signed, which is
    what every caller saw before this existed.
    """
    if not claims:
        return claims
    try:
        store = account_store()
        if store is None:
            return claims
        sub = str(claims.get("sub") or "")
        user = store.get_by_id(sub) if sub else None
        if user is None and claims.get("email"):
            found = store.get_by_email(str(claims["email"]))
            # Only the same subject: an address reused by another account must not
            # hand this session that account's roles.
            if found is not None and found.user_id == sub:
                user = found
    except Exception:  # noqa: BLE001 - an unreadable store is not a reason to sign anyone out
        return claims
    if user is None:
        return claims
    if user.disabled or is_suspended(user.attributes):
        return None
    out = dict(claims)
    out["roles"] = list(user.roles)
    out["email"] = user.email
    if user.name:
        out["name"] = user.name
    if user.site:
        out["site"] = user.site
    else:
        out.pop("site", None)
    attrs = abac_attributes(user.attributes)
    if attrs:
        out["attributes"] = attrs
    else:
        out.pop("attributes", None)
    return out


def _handles(record: dict) -> set[str]:
    """Every handle a key minted for this account can carry."""
    from .users import _handle_name

    site = record.get("site")
    names = {_handle_name(str(record.get("user_id") or ""))}
    local = str(record.get("email") or "").split("@", 1)[0].strip()
    if local:
        names.add(local)
    out = set()
    for n in names:
        out.add(f"@{n}")
        if site:
            out.add(f"@{n}:{site}")
    return out


def suspended_index(path: Path | None = None) -> dict:
    """``{"handles": set, "names": {name: sites}}`` of suspended accounts.

    Cached by the file's signature, so a key check is a ``stat`` until the file
    changes. A missing or unreadable file suspends nobody: the accounts file going
    away must not refuse every key on the node.
    """
    path = path if path is not None else accounts_path()
    empty: dict = {"handles": set(), "names": {}}
    if path is None:
        return empty
    try:
        st = path.stat()
    except OSError:
        return empty
    sig = (st.st_mtime_ns, st.st_ino, st.st_size)
    key = str(path)
    with _LOCK:
        cached = _INDEX.get(key)
        if cached is not None and cached[0] == sig:
            return cached[1]
    try:
        from .file_store import load_user_records

        records = load_user_records(path)
    except Exception:  # noqa: BLE001 - see the docstring
        return empty
    handles: set[str] = set()
    names: dict[str, set[str | None]] = {}
    for r in records:
        if not is_suspended(r.get("attributes")):
            continue
        for h in _handles(r):
            handles.add(h)
            name, _, site = h[1:].partition(":")
            names.setdefault(name, set()).add(site or None)
    index = {"handles": handles, "names": names}
    with _LOCK:
        _INDEX[key] = (sig, index)
    return index


def key_holder_suspended(record: dict) -> bool:
    """Whether an issued key belongs to a suspended account.

    A key is matched to its holder by its principal handle, the same rule the gate
    uses to name a person's self-minted key: ``@<name>:<site>``. A bare ``@name``
    on the account matches the key at any site; an account held to a site matches
    only that site's keys.
    """
    principal = str(record.get("principal") or "")
    if not principal.startswith("@"):
        return False
    index = suspended_index()
    if principal in index["handles"]:
        return True
    name, _, site = principal[1:].partition(":")
    sites = index["names"].get(name)
    if not sites:
        return False
    return None in sites or (site or record.get("site")) in sites


def _reset_for_tests() -> None:
    with _LOCK:
        _STORES.clear()
        _INDEX.clear()


__all__ = [
    "ACCOUNTS_ENV",
    "SUSPENDED_AT",
    "SUSPENDED_BY",
    "SUSPENDED_REASON",
    "abac_attributes",
    "account_store",
    "is_suspended",
    "key_holder_suspended",
    "live_session_claims",
    "suspended_index",
]
