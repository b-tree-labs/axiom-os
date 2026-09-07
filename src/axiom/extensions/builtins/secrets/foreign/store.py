# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ForeignCredentialStore — named third-party credentials (issue #667).

Two planes, deliberately separated:

- **Values** live in a ``SecretStore`` opened through the provider
  factory — ``keychain`` on darwin; ``file`` (0600, dev/CI) via
  ``AXIOM_FOREIGN_SECRETS_BACKEND``; any injected store under test.
  Values never touch the metadata index, logs, or results.
- **Metadata** (name, rotation-provider kind, issuer URL, expiry,
  git wiring, timestamps) lives in a 0600 JSON index at
  ``<state_dir>/secrets/foreign-credentials.json`` so listing and
  expiry auditing never need to open the keychain at all.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from ..providers.protocol import Secret, SecretRef, SecretStore

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_INDEX_VERSION = 1

# Metadata fields settable through ``set``/``update_metadata``. Anything
# else is rejected so a typo can't silently invent a field.
_META_FIELDS = (
    "provider",
    "issuer_url",
    "expires_at",
    "last_rotated_at",
    "git_host",
    "git_username",
    "notes",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _index_path(state_dir: Path) -> Path:
    return Path(state_dir) / "secrets" / "foreign-credentials.json"


def declared_secret_names(state_dir: Path) -> list[str]:
    """Names in the metadata index — no value store is opened.

    The cheap read used by ``axi ext lint`` (manifest secret-dependency
    check) and any surface that only needs to know *what exists*.
    """
    path = _index_path(Path(state_dir))
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return sorted((data.get("credentials") or {}).keys())


def open_default_value_store(state_dir: Path) -> SecretStore:
    """The custody backend for foreign-credential VALUES.

    ``AXIOM_FOREIGN_SECRETS_BACKEND`` selects a registered
    SecretStoreProvider kind explicitly. Unset, the darwin keychain is
    used when available; in ``AXIOM_MODE=dev`` a missing keychain
    degrades to the 0600 ``file`` backend; outside dev it fails closed.
    """
    from .. import SecretStoreUnavailable, _default_config_for_scheme, _mode
    from ..providers.keychain import KeychainSecretStoreProvider
    from ..providers.localfile import FileSecretStoreProvider
    from ..providers.registry import SecretStoreRegistry

    scheme = (
        os.environ.get("AXIOM_FOREIGN_SECRETS_BACKEND") or ""
    ).strip().lower()
    if scheme:
        provider = SecretStoreRegistry.get(scheme)(
            _default_config_for_scheme(scheme)
        )
        return provider.open()

    keychain = KeychainSecretStoreProvider(
        _default_config_for_scheme("keychain")
    )
    if keychain.available():
        return keychain.open()
    if _mode() == "dev":
        return FileSecretStoreProvider(
            {
                "name": "foreign-file-fallback",
                "path": str(
                    Path(state_dir) / "secrets" / "foreign-values.json"
                ),
            }
        ).open()
    raise SecretStoreUnavailable(
        "no OS keychain available for foreign credentials and "
        f"AXIOM_MODE={_mode()!r} refuses the plaintext-equivalent `file` "
        "fallback; set AXIOM_FOREIGN_SECRETS_BACKEND to a real backend."
    )


class ForeignCredentialStore:
    """Named foreign credentials: keychain values + JSON metadata index."""

    def __init__(
        self,
        state_dir: Path,
        *,
        value_store: SecretStore | None = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._value_store = value_store

    @property
    def _values(self) -> SecretStore:
        """Custody backend, opened lazily.

        Metadata-only paths (``list``/``metadata``/``audit``) never open
        the keychain at all — important for read-only surfaces (MCP
        list/audit) on hosts without one.
        """
        if self._value_store is None:
            self._value_store = open_default_value_store(self._state_dir)
        return self._value_store

    # -- index plumbing ----------------------------------------------------

    @property
    def index_path(self) -> Path:
        return _index_path(self._state_dir)

    def _load_index(self) -> dict:
        if not self.index_path.exists():
            return {"version": _INDEX_VERSION, "credentials": {}}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": _INDEX_VERSION, "credentials": {}}
        data.setdefault("credentials", {})
        return data

    def _save_index(self, data: dict) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            self.index_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600,
        )
        try:
            os.write(fd, json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(self.index_path, 0o600)

    @staticmethod
    def _validate_name(name: str) -> str:
        if not name or not _NAME_RE.match(name):
            raise ValueError(
                f"bad credential name {name!r}: use letters/digits/._- "
                "(must start alphanumeric)"
            )
        return name

    @staticmethod
    def _ref(name: str) -> SecretRef:
        return SecretRef(scheme="foreign", path=name)

    # -- lifecycle ---------------------------------------------------------

    def set(self, name: str, value: bytes, **meta: str | None) -> dict:
        """Store/overwrite a credential value + metadata. Returns metadata."""
        self._validate_name(name)
        unknown = set(meta) - set(_META_FIELDS)
        if unknown:
            raise ValueError(f"unknown metadata fields: {sorted(unknown)}")
        if not isinstance(value, (bytes, bytearray)) or not value:
            raise ValueError("credential value must be non-empty bytes")

        self._values.put(self._ref(name), bytes(value))

        data = self._load_index()
        existing = data["credentials"].get(name) or {}
        record = {
            "name": name,
            "created_at": existing.get("created_at") or _now_iso(),
            "updated_at": _now_iso(),
        }
        for field in _META_FIELDS:
            val = meta.get(field, existing.get(field))
            if val is not None:
                record[field] = val
        data["credentials"][name] = record
        self._save_index(data)
        return dict(record)

    def get(self, name: str) -> Secret:
        """The credential value (context-managed ``Secret``)."""
        self._validate_name(name)
        if not self.exists(name):
            raise KeyError(f"no foreign credential named {name!r}")
        return self._values.get(self._ref(name))

    def remove(self, name: str) -> None:
        self._validate_name(name)
        data = self._load_index()
        if name not in data["credentials"]:
            raise KeyError(f"no foreign credential named {name!r}")
        try:
            self._values.delete(self._ref(name))
        except KeyError:
            pass  # metadata existed without a value — still drop the index row
        del data["credentials"][name]
        self._save_index(data)

    def exists(self, name: str) -> bool:
        return name in self._load_index()["credentials"]

    # -- metadata ----------------------------------------------------------

    def list(self) -> list[dict]:
        """Metadata rows, sorted by name. NEVER contains values."""
        creds = self._load_index()["credentials"]
        return [dict(creds[name]) for name in sorted(creds)]

    def metadata(self, name: str) -> dict:
        self._validate_name(name)
        creds = self._load_index()["credentials"]
        if name not in creds:
            raise KeyError(f"no foreign credential named {name!r}")
        return dict(creds[name])

    def update_metadata(self, name: str, **fields: str | None) -> dict:
        self._validate_name(name)
        unknown = set(fields) - set(_META_FIELDS)
        if unknown:
            raise ValueError(f"unknown metadata fields: {sorted(unknown)}")
        data = self._load_index()
        if name not in data["credentials"]:
            raise KeyError(f"no foreign credential named {name!r}")
        record = data["credentials"][name]
        for key, val in fields.items():
            if val is None:
                record.pop(key, None)
            else:
                record[key] = val
        record["updated_at"] = _now_iso()
        data["credentials"][name] = record
        self._save_index(data)
        return dict(record)


__all__ = [
    "ForeignCredentialStore",
    "declared_secret_names",
    "open_default_value_store",
]
