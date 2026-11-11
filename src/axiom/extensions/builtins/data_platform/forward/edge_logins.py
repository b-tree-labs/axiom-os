# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Named Box logins for edge nodes, held here until each node exists.

the storage login command (``data box-login``) makes one node's Box login: a browser sign-in through the
owner's own OAuth app, written as the one line rclone reads. The forwarder on
that node reads it with ``--box-login <entry> --box-app <app entry>``. Box
renews a login on every use and cancels the old one, so a login has exactly one
live copy, and a second holder (another node, or the host that pulls Box into
bronze with an OAuth refresh token) breaks the first.

This module is the ledger between "made" and "installed":

- ``reserve`` names a future node, the Box folder it drops into, and the vault
  entry holding the Box app the login is made through. It stores no secret.
- ``capture`` moves the file the storage login command (``data box-login``) wrote into the vault under
  ``box-edge-<name>``. A refresh token this host already holds (the OAuth
  reader's token store, or another edge login) is refused.
- ``renew`` replaces a held login after a fresh the storage login command (``data box-login``). A login
  unused for about 60 days is dead; ``list`` says when, with the steps.
- ``attach`` moves the login onto the node's own state dir and deletes this
  host's copy, so only the node renews it. The app entry is copied (it is not
  single-use).

The ledger records names, folders, the app entry name and status. It never
records a token, and no result carries one.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.infra.state_integrity import atomic_write_text, read_state_text

#: Box drops a refresh token about 60 days after its last use. The date is a
#: reminder, not a lease Box honours.
REFRESH_LIFETIME = timedelta(days=60)

#: Where the OAuth reader keeps its rotating refresh token by default
#: (the storage provider's OAuth config reads it from here).
DEFAULT_READER_TOKEN_STORE = "~/.axi/credentials/box/oauth_refresh.json"

CREDENTIAL_PREFIX = "box-edge-"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FOLDER_RE = re.compile(r"^[0-9]{3,}$")
_ENTRY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LEDGER = "edge-box-logins.json"


class EdgeLoginError(ValueError):
    """A request was refused. The message is safe to show; it never contains a token."""


def credential_name(name: str) -> str:
    """The vault entry the node's forwarder reads as ``--box-login``."""
    return f"{CREDENTIAL_PREFIX}{_name(name)}"


def reserve(state_dir: Path, name: str, folder_id: str, app: str) -> dict[str, Any]:
    """Name a future node, its Box folder and the app its login is made through."""
    name = _name(name)
    folder = _folder(folder_id)
    app = _entry(app, "app")
    data = _load(state_dir)
    row = data["logins"].get(name)
    if row is None:
        row = {
            "name": name,
            "folder_id": folder,
            "app": app,
            "credential": credential_name(name),
            "status": "reserved",
            "node": "",
            "created_at": _now(),
            "refresh_expires_at": "",
            "attached_at": "",
        }
    elif row["status"] != "reserved" and (row["folder_id"] != folder or row.get("app") != app):
        raise EdgeLoginError(
            f"edge Box login {name} already holds a login for folder {row['folder_id']} "
            f"through app {row.get('app')!r}; not changing either out from under it"
        )
    else:
        row["folder_id"] = folder
        row["app"] = app
    data["logins"][name] = row
    _save(state_dir, data)
    return _public(row)


def capture(
    state_dir: Path,
    name: str,
    login_line: str,
    *,
    store: ForeignCredentialStore | None = None,
    reader_token_stores: list[Path] | None = None,
) -> dict[str, Any]:
    """Store the first login for a reserved name, from the file the storage login command (``data box-login``) wrote."""
    name = _name(name)
    data = _load(state_dir)
    row = data["logins"].get(name)
    if row is None:
        raise EdgeLoginError(f"edge Box login {name} is not reserved; reserve its folder and app first")
    if row["status"] == "attached":
        raise EdgeLoginError(_moved_message(row))

    line, refresh = parse_login(login_line)
    vault = store or ForeignCredentialStore(state_dir)
    _refuse_held(refresh, name, data, vault, reader_token_stores)

    cred = credential_name(name)
    if vault.exists(cred):
        with vault.get(cred) as secret:
            current = secret.as_str()
        if _refresh_of(current) != refresh:
            raise EdgeLoginError(
                f"edge Box login {name} already holds a login. A different one replaces it "
                "only through renew, after a new `axi data box-login`."
            )
        row["status"] = "held"
        data["logins"][name] = row
        _save(state_dir, data)
        return _public(row)

    expires = _expires()
    _store_login(vault, cred, line, expires, f"edge Box login {name}, held until its node is installed")
    row["status"] = "held"
    row["refresh_expires_at"] = expires
    data["logins"][name] = row
    _save(state_dir, data)
    return _public(row)


def renew(
    state_dir: Path,
    name: str,
    login_line: str,
    *,
    store: ForeignCredentialStore | None = None,
    reader_token_stores: list[Path] | None = None,
) -> dict[str, Any]:
    """Replace a held login with a new one.

    A login already moved to a node is not replaced here: that would leave two
    hosts holding one login. The error is the procedure to run on the node. On
    the node itself (no ledger row, but the vault entry exists) the entry is
    replaced in place.
    """
    name = _name(name)
    data = _load(state_dir)
    row = data["logins"].get(name)
    line, refresh = parse_login(login_line)
    vault = store or ForeignCredentialStore(state_dir)
    cred = credential_name(name)

    if row is not None and row["status"] == "attached":
        raise EdgeLoginError(_moved_message(row))
    if row is not None and row["status"] != "held":
        raise EdgeLoginError(
            f"edge Box login {name} has no login yet. Capture the first one; "
            "renew replaces one that is already held."
        )
    if row is None and not vault.exists(cred):
        raise EdgeLoginError(f"this host holds no edge Box login {cred!r}. Renew on the host that holds it.")
    _refuse_held(refresh, name, data, vault, reader_token_stores)

    expires = _expires()
    _store_login(vault, cred, line, expires, f"edge Box login {name}")
    if row is None:
        return {
            "name": name,
            "credential": cred,
            "status": "replaced",
            "refresh_expires_at": expires,
            "needs_renewal": False,
            "renew": [],
        }
    row["status"] = "held"
    row["refresh_expires_at"] = expires
    data["logins"][name] = row
    _save(state_dir, data)
    return _public(row)


def attach(
    state_dir: Path,
    name: str,
    node: str,
    dest_state_dir: Path,
    *,
    store: ForeignCredentialStore | None = None,
    dest_store: ForeignCredentialStore | None = None,
) -> dict[str, Any]:
    """Move the login onto the node's state dir. This host keeps no copy."""
    name = _name(name)
    node = _node(node)
    src = Path(state_dir)
    dest = Path(dest_state_dir)
    if src.resolve() == dest.resolve():
        raise EdgeLoginError(
            "the node's state dir must be a different directory from this host's, "
            "or both will renew the same login"
        )

    data = _load(src)
    row = data["logins"].get(name)
    if row is None:
        raise EdgeLoginError(f"edge Box login {name} is not reserved")
    if row["status"] == "reserved":
        raise EdgeLoginError(f"edge Box login {name} has no login yet; capture one before moving it")
    if row["status"] == "attached" and row.get("node") not in ("", node):
        raise EdgeLoginError(f"edge Box login {name} is already on {row['node']}")

    cred = row["credential"]
    app = row.get("app") or ""
    vault = store or ForeignCredentialStore(src)
    target = dest_store or ForeignCredentialStore(dest)

    if row["status"] == "attached" or (not vault.exists(cred) and target.exists(cred)):
        if not target.exists(cred):
            raise EdgeLoginError(f"edge Box login {name} was moved to {node}, but that state dir has no {cred}")
        row.update(status="attached", node=node, attached_at=row.get("attached_at") or _now())
        data["logins"][name] = row
        _save(src, data)
        return {**_public(row), "already": True}
    if not vault.exists(cred):
        raise EdgeLoginError(f"edge Box login {name} has no login stored")

    # The app first: a node holding a login with no app to renew it through is broken.
    if app and not target.exists(app):
        if not vault.exists(app):
            raise EdgeLoginError(f"the Box app entry {app!r} is not in this host's vault; store it before moving")
        with vault.get(app) as secret:
            target.set(app, secret.as_str().encode("utf-8"), notes="Box app for edge forwarders")
    with vault.get(cred) as secret:
        raw = secret.as_str()
    _store_login(target, cred, raw, str(row.get("refresh_expires_at") or ""), f"edge Box login {name} on {node}")
    vault.remove(cred)
    row.update(status="attached", node=node, attached_at=_now())
    data["logins"][name] = row
    _save(src, data)
    return _public(row)


def list_logins(state_dir: Path) -> list[dict[str, Any]]:
    """Every edge Box login on this host's ledger. No secrets."""
    data = _load(state_dir)
    return [_public(data["logins"][n]) for n in sorted(data["logins"])]


def renew_steps(row: dict[str, Any]) -> list[str]:
    """What a person, then an agent, does when ``needs_renewal`` is true.

    The browser sign-in is a person. Everything after the file exists is the agent.
    """
    name = row["name"]
    file = f"box-edge-{name}.json"
    app = row.get("app") or "<app entry>"
    sign_in = f"axi data box-login --app {app} --out {file}"
    if row.get("status") == "attached":
        host = row.get("node") or "the node"
        return [
            f"Run these on {host}, not on this host.",
            f"A person runs: {sign_in} (sign in as the Box account made for {name})",
            f"On {host}: axi data edge-login renew {name} --login-file {file}",
            f"On {host}: delete {file}",
            f"The forwarder on {host} reads {row['credential']} on its next pass. Do not copy the login back.",
        ]
    return [
        f"A person runs: {sign_in} (sign in as the Box account made for {name})",
        f"axi data edge-login renew {name} --login-file {file}",
        f"Delete {file}",
        "axi data edge-login list: refresh_expires_at moves out and needs_renewal is false.",
        "If renew says this login is already held on this host, sign in again for a new one.",
    ]


def parse_login(text: str) -> tuple[str, str]:
    """The one-line login the storage login command (``data box-login``) writes, and its refresh token.

    The line is stored as written, because it is what rclone reads.
    """
    line = (text or "").strip()
    try:
        blob = json.loads(line)
    except json.JSONDecodeError:
        blob = None
    if not isinstance(blob, dict) or "\n" in line:
        raise EdgeLoginError("that is not a Box login: expected the one line `axi data box-login` writes")
    refresh = str(blob.get("refresh_token") or "").strip()
    if not refresh or not str(blob.get("access_token") or "").strip():
        raise EdgeLoginError(
            "that Box login has no access_token and refresh_token; make a fresh one with `axi data box-login`"
        )
    return line, refresh


# -- internals ---------------------------------------------------------------


def _store_login(vault: ForeignCredentialStore, cred: str, line: str, expires: str, notes: str) -> None:
    # No rotation provider is recorded: the forwarder renews this login as it
    # uses it, and a second renewer would cancel the forwarder's copy.
    vault.set(cred, line.encode("utf-8"), expires_at=expires or None, notes=notes)


def _refuse_held(
    refresh: str,
    name: str,
    data: dict[str, Any],
    vault: ForeignCredentialStore,
    reader_token_stores: list[Path] | None,
) -> None:
    stores = reader_token_stores
    if stores is None:
        stores = [Path(os.path.expanduser(DEFAULT_READER_TOKEN_STORE))]
    for path in stores:
        if _reader_refresh(path) == refresh:
            raise EdgeLoginError(
                "this is the refresh token this host's Box reader already renews. A second "
                "holder cancels the reader's pull. Sign in again with `axi data box-login` "
                "for a login of its own."
            )
    for other, row in data["logins"].items():
        if other == name:
            continue
        cred = row.get("credential") or ""
        if not cred or not vault.exists(cred):
            continue
        with vault.get(cred) as secret:
            held = _refresh_of(secret.as_str())
        if held == refresh:
            raise EdgeLoginError(
                f"this login is already held as edge Box login {other}. Each node needs its own; "
                "sign in again with `axi data box-login`."
            )


def _reader_refresh(path: Path) -> str | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _refresh_of(text) or None


def _refresh_of(text: str) -> str:
    try:
        blob = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(blob, dict):
        return ""
    found = blob.get("refresh_token")
    return found.strip() if isinstance(found, str) else ""


def _moved_message(row: dict[str, Any]) -> str:
    host = row.get("node") or "a node"
    steps = " ".join(renew_steps({**row, "status": "attached"}))
    return (
        f"edge Box login {row['name']} was moved to {host}. Renewing it here would leave "
        f"two hosts holding one login. {steps}"
    )


def _needs_renewal(row: dict[str, Any]) -> bool:
    if row.get("status") not in ("held", "attached"):
        return False
    raw = str(row.get("refresh_expires_at") or "")
    try:
        day = datetime.fromisoformat(raw).date()
    except ValueError:
        return False
    return day <= datetime.now(UTC).date()


def _public(row: dict[str, Any]) -> dict[str, Any]:
    public = {
        "name": row["name"],
        "folder_id": row["folder_id"],
        "app": row.get("app") or "",
        "credential": row["credential"],
        "status": row["status"],
        "node": row.get("node") or "",
        "created_at": row.get("created_at") or "",
        "refresh_expires_at": row.get("refresh_expires_at") or "",
        "attached_at": row.get("attached_at") or "",
    }
    public["needs_renewal"] = _needs_renewal(public)
    public["renew"] = renew_steps(public) if public["needs_renewal"] else []
    return public


def _name(name: str) -> str:
    value = (name or "").strip()
    if not _NAME_RE.match(value):
        raise EdgeLoginError(
            f"bad edge Box login name {name!r}: lowercase letters, digits and hyphens, "
            "starting with a letter or digit"
        )
    return value


def _node(node: str) -> str:
    value = (node or "").strip()
    if not _NODE_RE.match(value):
        raise EdgeLoginError(f"bad node name {node!r}")
    return value


def _folder(folder_id: str) -> str:
    value = (folder_id or "").strip()
    if not _FOLDER_RE.match(value):
        raise EdgeLoginError(f"bad Box folder id {folder_id!r}: expected the numeric id")
    return value


def _entry(entry: str, what: str) -> str:
    value = (entry or "").strip()
    if not _ENTRY_RE.match(value):
        raise EdgeLoginError(f"give the vault entry holding the Box {what} (got {entry!r})")
    return value


def _load(state_dir: Path) -> dict[str, Any]:
    text = read_state_text(Path(state_dir) / _LEDGER)
    if text is None:
        return {"version": 1, "logins": {}}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EdgeLoginError(f"the edge Box login ledger is not valid JSON ({exc})") from None
    if not isinstance(data, dict) or not isinstance(data.get("logins"), dict):
        raise EdgeLoginError("the edge Box login ledger has the wrong shape")
    return data


def _save(state_dir: Path, data: dict[str, Any]) -> None:
    atomic_write_text(Path(state_dir) / _LEDGER, json.dumps(data, indent=2, sort_keys=True), mode=0o600)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _expires() -> str:
    return (datetime.now(UTC) + REFRESH_LIFETIME).date().isoformat()


__all__ = [
    "CREDENTIAL_PREFIX",
    "DEFAULT_READER_TOKEN_STORE",
    "EdgeLoginError",
    "attach",
    "capture",
    "credential_name",
    "list_logins",
    "parse_login",
    "renew",
    "renew_steps",
    "reserve",
]
