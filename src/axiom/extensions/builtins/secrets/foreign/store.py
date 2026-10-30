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
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from ..providers.protocol import Secret, SecretRef, SecretStore

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Version 2 (ADR-218) records each credential's ``host``. The file keeps its
# path so an older release on the same machine reads the same index; see
# axiom.extensions.builtins.vault.index for the migration and the one reader.
_INDEX_VERSION = 2

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
    # What can actually be done about this credential, and the follow-ups each
    # answer makes sensible. Asking every credential "when does this expire"
    # produced six unclearable findings whose advice was wrong for most of
    # them; see axiom.extensions.builtins.vault.disposition.
    "disposition",
    # Derived in `set()` from the value; never accepted from a caller. Listed
    # so it round-trips through the index like any other recorded field.
    "fingerprint",
    # For credentials with no expiry to record: when to look at this again, so
    # something falls due even though nothing expires.
    "review_by",
    # Who to contact when the credential is somebody else's to rotate. Without
    # it, "externally owned" is an unactionable finding.
    "owner",
    # KEEP rotates this credential unattended only when the operator sets this.
    # Absent from the index, `set` rejected it, so the gate could never open.
    "auto_rotate",
    # Proof of life. We cannot predict death for a credential we do not
    # control; this is how we notice it within a heartbeat instead of months.
    "last_verified_at",
    # The public half of an OAuth/OIDC client credential. Not secret (it rides
    # in every sign-in redirect), but meaningless apart from the secret it is
    # paired with, so it is recorded on the same entry. With ``issuer_url`` it
    # is what lets a local node discover a sign-in client it can use.
    "client_id",
    # The host this credential is for (ADR-218). Derived from ``git_host`` or
    # ``issuer_url`` when a call sets either without naming a host, so
    # ``vault list --host`` and ``vault resolve --host`` read one recorded answer.
    "host",
)


def _rederive_host(record: dict, changed: dict) -> None:
    """Keep a recorded ``host`` in step with the fields it is derived from."""
    if "host" in changed or not ({"git_host", "issuer_url"} & set(changed)):
        return
    from axiom.extensions.builtins.vault.index import derive_host

    record.pop("host", None)
    host = derive_host(record)
    if host:
        record["host"] = host


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
    if not path.exists() and not path.is_symlink():
        return []
    try:
        creds = ForeignCredentialStore(Path(state_dir))._load_index()["credentials"]
    except Exception as exc:  # noqa: BLE001 - a read-only listing stays alive, but says why
        logging.getLogger(__name__).error(
            "credential index unreadable, so no names are listed: %s", exc
        )
        return []
    return sorted(creds.keys())


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
    from ..vault_settings import foreign_backend

    # The environment, else the vault settings file: one answer for a shell and for a
    # process a desktop app started without one.
    scheme = foreign_backend()
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



def _fingerprint_of(value: bytes) -> str:
    """The digest `secrets.discover` matches a finding against."""
    from ..discovery.model import fingerprint

    return fingerprint(value.decode("utf-8", "replace"))


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

    def _index_fix(self) -> str:
        p = self.index_path
        return (
            f"restore a good copy of {p}, or move it aside with `mv '{p}' '{p}.bad'` and "
            "re-add each credential (the secret values themselves are in the keychain/"
            "value store and are not affected)"
        )

    def _load_index(self) -> dict:
        """The metadata index. Damaged is NOT empty.

        An empty answer for a damaged index made the next ``set()`` rewrite it
        from nothing: every other credential's metadata (expiry, fingerprint,
        disposition) was silently destroyed, and `secrets.discover` then
        reported managed credentials as loose. So damage raises, the file is
        left exactly as found, and nothing is written over it.
        """
        from axiom.infra.state_integrity import (
            StateDamaged,
            read_state_text,
            restore_if_snapshot,
        )

        path = self.index_path
        fix = self._index_fix()
        restore_if_snapshot(path, what="credential index")
        text = read_state_text(path, fix=fix)
        if text is None:
            return {"version": _INDEX_VERSION, "credentials": {}}
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise StateDamaged(path, f"is not valid JSON ({exc})", fix) from None
        if not isinstance(data, dict) or not isinstance(data.get("credentials", {}), dict):
            raise StateDamaged(path, "has the wrong shape (expected an object of credentials)", fix)
        data.setdefault("credentials", {})
        return data

    def _save_index(self, data: dict) -> None:
        # Every write is a version 2 write (ADR-218): a version 1 file is copied
        # aside once, then each record gains the host it implies. Additive only.
        from axiom.extensions.builtins.vault.index import keep_v1_copy, upgrade_document
        from axiom.infra.state_integrity import atomic_write_text

        keep_v1_copy(self.index_path)
        data, _ = upgrade_document(data)

        atomic_write_text(
            self.index_path, json.dumps(data, indent=2, sort_keys=True), mode=0o600
        )

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

        if "fingerprint" in meta:
            # DERIVED, never supplied. A caller-set fingerprint would let the
            # index claim a credential is managed without the value ever
            # having been in custody, which is the one assertion this field
            # exists to make.
            raise ValueError("fingerprint is derived from the value, not supplied")

        self._values.put(self._ref(name), bytes(value))

        data = self._load_index()
        existing = data["credentials"].get(name) or {}
        record = {
            "name": name,
            "created_at": existing.get("created_at") or _now_iso(),
            "updated_at": _now_iso(),
            # A one-way, truncated digest of the value — the ONLY thing that
            # lets `secrets.discover` answer "is this credential already in
            # custody?" about material it finds on disk or in a process.
            #
            # Without it `discover` compared every finding against an empty
            # set, so `managed` was always None, `unmanaged` was always zero,
            # and the hourly sweep reported "clean" over a host it had not
            # actually checked. The index is the sweep's entire basis for
            # escalating, and nothing wrote it.
            #
            # The value itself never leaves custody. The digest does not
            # reverse, and `fingerprint()` truncates so a discovery report is
            # safe to paste into a ticket. It does let a holder of the index
            # CONFIRM a guess at a credential's value, which is why it is
            # derived here rather than accepted from a caller.
            "fingerprint": _fingerprint_of(bytes(value)),
        }
        for field in _META_FIELDS:
            if field == "fingerprint":
                # Derived above from the value just written. Carrying the
                # previous one forward would leave the index asserting
                # custody of the OLD value after a rotation — the index
                # would say a rotated credential is still the one found
                # loose on disk, which is the exact opposite of true.
                continue
            val = meta.get(field, existing.get(field))
            if val is not None:
                record[field] = val
        _rederive_host(record, {k: v for k, v in meta.items() if v is not None})
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
        _rederive_host(record, fields)
        record["updated_at"] = _now_iso()
        data["credentials"][name] = record
        self._save_index(data)
        return dict(record)


__all__ = [
    "ForeignCredentialStore",
    "declared_secret_names",
    "open_default_value_store",
]
