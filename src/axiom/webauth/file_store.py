# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A JSON-file-backed :class:`~axiom.webauth.users.UserStore` + its writer.

The web gate provisions its accounts from a small operator-owned file rather
than a database in this cut. Two properties matter and are both provided here:

* **Fail-closed validation** — :func:`load_user_records` reads a JSON array of
  account objects and rejects the *whole file* on any problem (bad shape,
  plaintext ``password``, unknown field, missing/duplicate email). A partial or
  ambiguous account set is never returned; the caller gets records or an error.

* **Hot-reload** — :class:`JsonFileUserStore` stats the file on each lookup and
  re-parses only when the mtime changes, so an appended account (via
  :func:`upsert_user_record`, e.g. ``axi gate adduser``) is picked up live with
  no process restart. On a parse failure it fails closed to deny-all and records
  the mtime, retrying only when the file is edited again.

Accounts file shape (``password_hash`` is a scrypt string from
:func:`axiom.webauth.password.get_password_hash`; plaintext is refused)::

    [
      {"user_id": "op-20", "email": "op@example.org", "name": "Op Name",
       "password_hash": "scrypt$...", "roles": ["operator"]}
    ]
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from pathlib import Path

from .users import InMemoryUserStore, User, UserStore, _norm_email

# The keys forwarded to ``User(**record)``. Anything else in a record is a
# typo/misuse and is rejected — except "password", which gets its own error.
# Kept deliberately identical to the consumer's role-index loader so one file
# satisfies both readers.
_USER_FIELDS = frozenset({"user_id", "email", "password_hash", "name", "roles"})


class AccountsFileError(ValueError):
    """The accounts file is missing or invalid (fail-closed)."""


def _load_raw(path: Path) -> object:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as e:  # pragma: no cover - env-dependent
            raise AccountsFileError(
                f"{path} is YAML but PyYAML is not installed; use JSON or install pyyaml"
            ) from e
        return yaml.safe_load(text)
    return json.loads(text)


def _validate_records(raw: object, *, where: str) -> list[dict]:
    """Normalize + validate a parsed accounts list; raise on any problem.

    Returns records shaped for ``User(**record)`` (``roles`` as a tuple,
    ``email`` lower-cased, ``user_id`` defaulted to the email).
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise AccountsFileError(f"{where}: top level must be a list of account objects")

    records: list[dict] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        at = f"{where}[{i}]"
        if not isinstance(entry, dict):
            raise AccountsFileError(f"{at}: each account must be an object")
        if "password" in entry:
            raise AccountsFileError(
                f"{at}: 'password' (plaintext) is not allowed — store a "
                "'password_hash' instead (get_password_hash('...'))"
            )
        unknown = set(entry) - _USER_FIELDS
        if unknown:
            raise AccountsFileError(f"{at}: unknown field(s): {', '.join(sorted(unknown))}")
        email = str(entry.get("email", "")).strip().lower()
        if not email:
            raise AccountsFileError(f"{at}: 'email' is required")
        pw_hash = entry.get("password_hash")
        if not (isinstance(pw_hash, str) and pw_hash.strip()):
            raise AccountsFileError(f"{at}: 'password_hash' is required (non-empty)")
        if email in seen:
            raise AccountsFileError(f"{at}: duplicate email {email!r}")
        seen.add(email)
        roles = entry.get("roles", ())
        if isinstance(roles, str) or not isinstance(roles, (list, tuple)):
            raise AccountsFileError(f"{at}: 'roles' must be a list of strings")
        records.append(
            {
                "user_id": str(entry.get("user_id") or email),
                "email": email,
                "password_hash": pw_hash,
                "name": str(entry.get("name", "")),
                "roles": tuple(str(r) for r in roles),
            }
        )
    return records


def load_user_records(path: str | os.PathLike) -> list[dict]:
    """Read + validate the accounts file into records ready for ``User(**r)``.

    Fail-closed: any problem raises :class:`AccountsFileError` rather than
    yielding a partial account set. An empty list (deny-all) is allowed — the
    caller decides whether to warn about it.
    """
    p = Path(path)
    if not p.is_file():
        raise AccountsFileError(f"accounts file not found: {p}")
    try:
        raw = _load_raw(p)
    except AccountsFileError:
        raise
    except Exception as e:  # normalize any parser error into our type
        raise AccountsFileError(f"could not parse {p}: {e}") from e
    return _validate_records(raw, where=str(p))


def _serializable(record: dict) -> dict:
    """JSON-ready copy — ``roles`` tuple → list; drop keys equal to defaults."""
    out = {"email": record["email"], "password_hash": record["password_hash"]}
    if record.get("user_id") and record["user_id"] != record["email"]:
        out = {"user_id": record["user_id"], **out}
    if record.get("name"):
        out["name"] = record["name"]
    out["roles"] = list(record.get("roles", ()))
    return out


def save_user_records(path: str | os.PathLike, records: Iterable[dict]) -> Path:
    """Validate + atomically write the accounts file (mode 0600).

    Validation happens *before* any write, so an invalid record never clobbers
    a good file. The write is temp-then-``os.replace`` so a reader (or the
    running store) never observes a half-written file, and the rename bumps the
    mtime the store watches.
    """
    p = Path(path)
    validated = _validate_records(list(records), where=str(p))
    payload = json.dumps([_serializable(r) for r in validated], indent=2) + "\n"
    tmp = p.with_name(p.name + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()
    return p


def upsert_user_record(
    path: str | os.PathLike,
    *,
    email: str,
    password_hash: str,
    name: str | None = None,
    roles: Sequence[str] | None = None,
    user_id: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Insert or replace one account, preserving the rest of the file.

    On update, ``name``/``roles`` left as ``None`` keep the existing values (so
    a password reset does not have to restate a user's roles). Refuses to
    overwrite an existing email unless ``overwrite=True``.

    Returns ``{"email", "user_id", "created", "roles"}``.
    """
    p = Path(path)
    records = load_user_records(p) if p.is_file() else []
    norm = _norm_email(email)
    idx = next((i for i, r in enumerate(records) if r["email"] == norm), None)
    created = idx is None
    if not created and not overwrite:
        raise AccountsFileError(
            f"account already exists: {norm} (pass overwrite=True to replace)"
        )
    prior = records[idx] if idx is not None else {}
    record = {
        "user_id": str(user_id or prior.get("user_id") or norm),
        "email": norm,
        "password_hash": password_hash,
        "name": name if name is not None else prior.get("name", ""),
        "roles": tuple(roles) if roles is not None else prior.get("roles", ()),
    }
    if created:
        records.append(record)
    else:
        records[idx] = record
    save_user_records(p, records)
    return {
        "email": norm,
        "user_id": record["user_id"],
        "created": created,
        "roles": list(record["roles"]),
    }


class JsonFileUserStore:
    """A :class:`UserStore` backed by an accounts JSON file, reloaded on change.

    Stats the file on each lookup and re-parses only when the mtime changes
    (cheap per-request stat; parse on change only). Fails closed to deny-all on
    a missing/broken file, recording the mtime so a broken file is not re-parsed
    every lookup — it retries when the admin edits it again. Mirrors the
    consumer's role index so both readers converge on the same edited file.
    """

    def __init__(self, path: str | os.PathLike | None, *, loader=None) -> None:
        self._path = Path(path) if path else None
        self._loader = loader or load_user_records
        self._mtime: float | None = None
        self._inner: InMemoryUserStore = InMemoryUserStore()

    def _refresh(self) -> None:
        if not self._path:
            self._inner = InMemoryUserStore()
            return
        try:
            mt = self._path.stat().st_mtime
        except OSError:
            self._inner = InMemoryUserStore()
            self._mtime = None
            return
        if mt == self._mtime:
            return
        # Record the mtime even on parse failure so a broken file isn't
        # re-parsed every lookup — retry only when it's edited (mtime) again.
        self._mtime = mt
        try:
            records = self._loader(self._path)
        except Exception:
            self._inner = InMemoryUserStore()  # fail-closed: deny-all until fixed
            return
        self._inner = InMemoryUserStore(User(**r) for r in records)

    def reload(self) -> None:
        """Force a re-read on the next lookup (bypass the mtime cache)."""
        self._mtime = None

    def get_by_email(self, email: str) -> User | None:
        self._refresh()
        return self._inner.get_by_email(email)

    def get_by_id(self, user_id: str) -> User | None:
        self._refresh()
        return self._inner.get_by_id(user_id)

    def __len__(self) -> int:
        self._refresh()
        return len(self._inner)


# A JsonFileUserStore satisfies the UserStore protocol.
_: UserStore = JsonFileUserStore(None)


__all__ = [
    "AccountsFileError",
    "JsonFileUserStore",
    "load_user_records",
    "save_user_records",
    "upsert_user_record",
]
