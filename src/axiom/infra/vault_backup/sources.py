# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What a vault backup holds, and how it is read and written back.

Inventory of credential custody the harness uses (the full survey, with
rationale, is in docs/specs/spec-vault-backup.md):

IN SCOPE -- values the harness itself holds
  * foreign credential store, local custody backends: the macOS keychain (or
    Windows Credential Manager) and the 0600 ``file`` backend.  Values are read
    and written ONLY through ``ForeignCredentialStore``, so the metadata index,
    fingerprints and audit stay consistent.
  * key files the harness owns: the node identity private key
    (``<state>/identity/private.pem``, which also signs attestation records and
    federation traffic), the audit-chain HMAC delivery artifact
    (``~/.config/axiom/audit-hmac-key.env``), and anything named in
    ``vault.backup.extra_files``.

OUT OF SCOPE -- custody lives elsewhere
  * remote secret managers (OpenBao, AWS/Azure/GCP, Kubernetes), env-var
    providers: the harness holds a reference, not the value, and those systems
    have their own durability.  Recorded as metadata only, with a recovery hint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.infra import backup_core as bc
from axiom.infra import state_snapshots as ss

#: Key files backed up by default (state-relative, or ``~``-relative).
DEFAULT_FILES: tuple[str, ...] = ("identity/private.pem", "~/.config/axiom/audit-hmac-key.env")

_LOCAL_STORE_CLASSES = frozenset({"_FileSecretStore", "_KeychainSecretStore", "_CredentialManagerStore"})

#: Index fields that cannot be passed back through ``ForeignCredentialStore.set``.
_NOT_RESTORABLE = ("fingerprint", "name", "created_at", "updated_at")

#: Churn that must not count as "the vault changed".
_DIGEST_IGNORE = ("updated_at", "last_verified_at")


def fingerprint_of(value: bytes) -> str:
    from axiom.extensions.builtins.secrets.discovery.model import fingerprint

    return fingerprint(value.decode("utf-8", "replace"))


@dataclass
class Entry:
    name: str
    kind: str  # "foreign" | "file"
    value: bytes = field(repr=False, default=b"")
    fingerprint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def manifest(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "scope": "value",
                "fingerprint": self.fingerprint, "size": len(self.value), "metadata": self.metadata}


@dataclass
class Collected:
    entries: list[Entry] = field(default_factory=list)
    metadata_only: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def manifest(self) -> list[dict[str, Any]]:
        rows = [e.manifest() for e in self.entries] + list(self.metadata_only)
        return sorted(rows, key=lambda r: r["name"])

    def set_digest(self) -> str:
        rows = []
        for r in self.manifest():
            meta = {k: v for k, v in (r.get("metadata") or {}).items() if k not in _DIGEST_IGNORE}
            rows.append([r["kind"], r["name"], r.get("scope"), r.get("fingerprint"), meta])
        return bc.sha256_bytes(bc.canon(rows))


def _brand_cli() -> str:
    return ss._brand_cli()


def _recovery_hint(name: str, meta: dict[str, Any]) -> str:
    bits = []
    if meta.get("issuer_url"):
        bits.append(f"re-issue at {meta['issuer_url']}")
    if meta.get("owner"):
        bits.append(f"owner: {meta['owner']}")
    if meta.get("disposition"):
        bits.append(f"disposition: {meta['disposition']}")
    bits.append(f"then `{_brand_cli()} vault set {name}`")
    return "; ".join(bits)


def custody_is_local(value_store: Any) -> bool:
    return type(value_store).__name__ in _LOCAL_STORE_CLASSES


def resolve_file_spec(spec: str, state_dir: Path) -> Path:
    if spec.startswith("~"):
        return Path(os.path.expanduser(spec))
    p = Path(spec)
    return p if p.is_absolute() else Path(state_dir) / spec


def file_specs(policy_extra: tuple[str, ...]) -> list[str]:
    seen: list[str] = []
    for s in (*DEFAULT_FILES, *policy_extra):
        if s not in seen:
            seen.append(s)
    return seen


def foreign_store(state_dir: Path, store: Any = None):
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    return store if store is not None else ForeignCredentialStore(state_dir)


def collect(state_dir: Path, *, extra_files: tuple[str, ...] = (), store: Any = None) -> Collected:
    """Read every in-scope value.  Values live only in the returned ``Entry`` objects."""
    out = Collected()
    fstore = foreign_store(state_dir, store)
    rows = fstore.list()
    local = True
    if rows:
        try:
            local = custody_is_local(fstore._values)
        except Exception as exc:  # noqa: BLE001
            local = False
            out.skipped.append({"name": "(foreign store)", "reason": f"custody backend unavailable: {type(exc).__name__}"})
    for row in rows:
        name = row["name"]
        meta = {k: v for k, v in row.items() if k not in ("name",)}
        if not local:
            out.metadata_only.append({"name": name, "kind": "foreign", "scope": "metadata-only",
                                      "fingerprint": row.get("fingerprint", ""), "size": 0,
                                      "metadata": meta, "recovery_hint": _recovery_hint(name, meta)})
            continue
        try:
            with fstore.get(name) as secret:
                value = bytes(secret.value)
        except Exception as exc:  # noqa: BLE001 -- never include exception text: a backend may echo
            out.skipped.append({"name": name, "reason": f"value unreadable ({type(exc).__name__})"})
            continue
        out.entries.append(Entry(name=name, kind="foreign", value=value,
                                 fingerprint=fingerprint_of(value), metadata=meta))
    for spec in file_specs(extra_files):
        path = resolve_file_spec(spec, state_dir)
        if not path.is_file():
            continue
        try:
            value = path.read_bytes()
        except OSError as exc:
            out.skipped.append({"name": f"file:{spec}", "reason": f"unreadable ({exc.strerror})"})
            continue
        if not value:
            continue
        out.entries.append(Entry(name=f"file:{spec}", kind="file", value=value,
                                 fingerprint=fingerprint_of(value), metadata={"mode": "0600"}))
    out.entries.sort(key=lambda e: e.name)
    return out


def current_fingerprints(state_dir: Path, *, extra_files: tuple[str, ...] = (), store: Any = None) -> dict[str, str]:
    """name -> fingerprint of what is in the vault NOW (values read, never returned)."""
    return {e.name: e.fingerprint for e in collect(state_dir, extra_files=extra_files, store=store).entries}


# -- writing back ----------------------------------------------------------


def write_entry(state_dir: Path, name: str, kind: str, value: bytes, metadata: dict[str, Any], *,
                store: Any = None) -> None:
    """Write one entry through the owning API.  Foreign credentials go through
    ``ForeignCredentialStore.set`` (index, fingerprint and custody stay consistent)."""
    if kind == "foreign":
        meta = {k: v for k, v in metadata.items()
                if k not in _NOT_RESTORABLE and isinstance(v, str)}
        foreign_store(state_dir, store).set(name, value, **meta)
        return
    if kind == "file":
        spec = name[len("file:"):]
        dest = resolve_file_spec(spec, state_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        bc.atomic_publish(dest, value)
        return
    raise ValueError(f"unknown entry kind {kind!r}")
