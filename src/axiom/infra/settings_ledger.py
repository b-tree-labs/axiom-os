# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The settings ledger: the ``node`` layer of settings as an append-only, hash-chained file.

ADR-284, prd-settings ST-11, ST-16 and ST-18. This is the file-backed form, the one a node
without a database uses (B1). The Postgres backend, signatures on authority keys, the
reconcile step and the offsite anchor are later chunks and do not change an entry's format.

An entry says one thing happened to one key: it was set, or it was unset. The effective value
of a key is its latest entry. Three rules keep that honest:

* **Compare and swap.** A writer passes the revision of the key it read. A stale revision is
  refused, and the check and the append happen under one lock, so two writers holding one
  revision cannot both win.
* **One writer per section.** A writer is made for one declared section and cannot write
  another. Who the writer is comes from the platform that built it, never from a value.
* **An authority key needs evidence.** A key a section marks as authority is changed only with
  evidence that a person signed that exact change. The ledger does not know how signing works:
  the signed-record extension registers a verifier (``set_authority_verifier``), as the decision
  seam in ``axiom.infra.orchestrator`` is filled. The verifier is asked again on every read, so a
  row inserted by hand, or evidence that has gone, is ignored and reported, never served.
* **Damage never confers anything.** If the chain does not verify, every key reads as unset
  and nothing more is appended. Settings that decide authority therefore fail closed.

The chain is a plain SHA-256 chain: it shows a changed or reordered entry, not a rewritten
chain or a cut-off tail. Those need the anchor (ADR-284 item 10), which comes later.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

GENESIS = "GENESIS"


class SettingsLedgerError(Exception):
    """Base of every refusal the ledger makes."""


class StaleRevision(SettingsLedgerError):
    """The key changed since the writer read it."""


class SectionNotOwned(SettingsLedgerError):
    """A writer tried to write a section that is not its own."""


class UnknownSection(SettingsLedgerError):
    """A section was written before it was declared."""


class InvalidValue(SettingsLedgerError):
    """A value does not satisfy its section's schema."""


class LedgerDamaged(SettingsLedgerError):
    """The chain does not verify, so nothing is appended to it."""


class AuthorityRequired(SettingsLedgerError):
    """An authority key was written without evidence."""


class AuthorityRefused(SettingsLedgerError):
    """The verifier did not accept the evidence for this change, or there is no verifier."""


class AuthorityVerifier(Protocol):
    def verify(
        self,
        *,
        section: str,
        key: str,
        op: str,
        value: Any,
        revision: int,
        evidence: str,
        scope: str = "",
    ) -> str | None:
        """``None`` when ``evidence`` is a signed record of exactly this change, else why not.
        ``revision`` is the revision the key takes with this entry, and ``scope`` is what the
        ledger's entries apply to (a site); use :func:`change_digest` to compare the change with
        what the record says."""


_verifier: AuthorityVerifier | None = None


def set_authority_verifier(verifier: AuthorityVerifier | None) -> None:
    global _verifier
    _verifier = verifier


def reset_authority_verifier() -> None:
    set_authority_verifier(None)


def change_digest(
    *, section: str, key: str, op: str, value: Any, revision: int, scope: str = ""
) -> str:
    """The digest a signed record carries to bind itself to one change: this section, key,
    operation and value at this revision, in this scope, and nothing else. Evidence for one change
    therefore cannot authorise another value, the same value again at a later revision, or the
    same change in another scope (another site's settings)."""
    body: dict[str, Any] = {
        "section": section, "key": key, "op": op, "value": value, "revision": revision,
    }  # fmt: skip
    if scope:
        body["scope"] = scope  # absent for an unscoped ledger, so its digests are unchanged
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Entry:
    seq: int
    ts: str
    section: str
    key: str
    op: str  # "set" or "unset"
    value: Any
    actor: str
    key_revision: int
    prev_hash: str
    hash: str
    evidence: str | None = None


_ENTRY_FIELDS = frozenset(f.name for f in fields(Entry))


@dataclass(frozen=True)
class Unverified:
    """An authority entry the ledger ignores, and why."""

    seq: int
    section: str
    key: str
    reason: str


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    records: int
    broken_at: int | None = None
    reason: str | None = None


def _canonical(body: Mapping[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def _hash(body: Mapping[str, Any], prev_hash: str) -> str:
    return hashlib.sha256((_canonical(body) + prev_hash).encode("utf-8")).hexdigest()


class SettingsLedger:
    """One node's settings ledger, a hash-chained JSONL file."""

    def __init__(
        self,
        path: str | Path,
        *,
        scope: str = "",
        verification_ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``verification_ttl`` is how long, in seconds, an authority entry the verifier has
        accepted is not asked about again. Checking a signed record is not free, and an authority
        key is read on the sign-in path. A new entry is checked when it is first read, and a
        record that is edited or goes missing is noticed within the window. ``0`` asks on every
        read. ``scope`` is what this ledger's entries apply to, a site for the node's gate pairs:
        it is part of what a signature binds, so a record signed for one scope is not evidence
        in another."""
        self.path = Path(path)
        self.scope = scope
        self._ttl = verification_ttl
        self._clock = clock
        self._verified: dict[tuple[str, str], float] = {}
        self._schemas: dict[str, dict[str, Any]] = {}
        self._authority: dict[str, frozenset[str]] = {}
        self._thread_lock = threading.Lock()

    # -- declaring and writing ------------------------------------------------------------

    def declare_section(
        self, name: str, *, schema: Mapping[str, Any], authority: tuple[str, ...] = ()
    ) -> None:
        """Declare a section, the JSON Schema of each key it may hold, and which of those keys
        are authority keys (changed only with evidence of a signature)."""
        unknown = set(authority) - set(schema)
        if unknown:
            raise ValueError(f"authority keys {sorted(unknown)} are not keys of section {name!r}")
        self._schemas[name] = dict(schema)
        self._authority[name] = frozenset(authority)

    def writer(self, section: str, *, actor: str) -> SectionWriter:
        if section not in self._schemas:
            raise UnknownSection(f"section {section!r} is not declared")
        return SectionWriter(self, section, actor)

    def _append(
        self,
        section: str,
        key: str,
        op: str,
        value: Any,
        actor: str,
        expected_revision: int,
        evidence: str | None = None,
    ) -> Entry:
        with self._locked():
            entries, reason = self._load()
            if reason is not None:
                raise LedgerDamaged(reason)
            current = _revisions(entries).get((section, key), 0)
            if current != expected_revision:
                raise StaleRevision(
                    f"{section}.{key} is at revision {current}, not {expected_revision}"
                )
            if key in self._authority.get(section, frozenset()):
                self._require_evidence(section, key, op, value, current + 1, evidence)
            prev = entries[-1].hash if entries else GENESIS
            body = {
                "seq": len(entries) + 1,
                "ts": datetime.now(UTC).isoformat(),
                "section": section,
                "key": key,
                "op": op,
                "value": value,
                "actor": actor,
                "key_revision": current + 1,
                "prev_hash": prev,
                "evidence": evidence,
            }
            record = {**body, "hash": _hash(body, prev)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(_canonical(record) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return Entry(**record)

    def _require_evidence(
        self, section: str, key: str, op: str, value: Any, revision: int, evidence: str | None
    ) -> None:
        if evidence is None:
            raise AuthorityRequired(f"{section}.{key} is an authority key and needs evidence")
        reason = self._ask_verifier(section, key, op, value, revision, evidence)
        if reason is not None:
            raise AuthorityRefused(reason)

    def _ask_verifier(
        self, section: str, key: str, op: str, value: Any, revision: int, evidence: str
    ) -> str | None:
        """Why the evidence is not accepted, or ``None``. Any failure to ask is a refusal."""
        if _verifier is None:
            return "no verifier is installed, so nobody can vouch for an authority change"
        try:
            extra = {"scope": self.scope} if self.scope else {}
            return _verifier.verify(
                section=section, key=key, op=op, value=value, revision=revision,
                evidence=evidence, **extra,
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - failing to check is failing the check
            return f"the verifier could not check the evidence ({type(exc).__name__})"

    def _check_value(self, section: str, key: str, value: Any) -> None:
        schema = self._schemas[section]
        if key not in schema:
            raise InvalidValue(f"section {section!r} declares no key {key!r}")
        import jsonschema

        try:
            jsonschema.validate(value, schema[key])
        except jsonschema.ValidationError as exc:
            raise InvalidValue(f"{section}.{key}: {exc.message}") from None

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """One lock around read, check and append, across threads and processes."""
        with self._thread_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_suffix(self.path.suffix + ".lock")
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                if sys.platform != "win32":
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)  # closing releases the flock

    # -- reading ----------------------------------------------------------------------------

    def _load(self) -> tuple[list[Entry], str | None]:
        """The entries, and the reason the chain does not verify (``None`` when it does)."""
        if not self.path.exists():
            return [], None
        entries: list[Entry] = []
        prev = GENESIS
        revisions: dict[tuple[str, str], int] = {}
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
                body = {k: v for k, v in record.items() if k != "hash"}
                # A later release may add a field. This one ignores it, and the hash below still
                # covers it, so two releases can read one ledger during a switch (ADR-284 B3).
                entry = Entry(**{k: v for k, v in record.items() if k in _ENTRY_FIELDS})
            except (ValueError, TypeError):
                return entries, f"line {number} is not a ledger entry"
            if entry.seq != number or entry.prev_hash != prev:
                return entries, f"entry {number} does not follow the one before it"
            if entry.hash != _hash(body, prev):
                return entries, f"entry {number} does not match its hash"
            expected = revisions.get((entry.section, entry.key), 0) + 1
            if entry.key_revision != expected:
                return entries, f"entry {number} is revision {entry.key_revision}, expected {expected}"
            revisions[(entry.section, entry.key)] = entry.key_revision
            entries.append(entry)
            prev = entry.hash
        return entries, None

    def entries(self) -> list[Entry]:
        return self._load()[0]

    def verify(self) -> VerifyReport:
        entries, reason = self._load()
        if reason is None:
            return VerifyReport(ok=True, records=len(entries))
        return VerifyReport(ok=False, records=len(entries), broken_at=len(entries) + 1, reason=reason)

    def damage(self) -> str | None:
        """Why the ledger cannot be trusted, or ``None`` when it can."""
        return self._load()[1]

    def _fold(self) -> tuple[dict[tuple[str, str], Any], list[Unverified]]:
        """The latest value of each key that is set, and the authority entries ignored.

        Both are empty when the chain is damaged. An authority entry counts only if the verifier
        accepts its evidence now: no evidence, no verifier, or a rejection all leave it out."""
        entries, reason = self._load()
        if reason is not None:
            return {}, []
        state: dict[tuple[str, str], Any] = {}
        ignored: list[Unverified] = []
        now = self._clock()
        for entry in entries:
            if entry.key in self._authority.get(entry.section, frozenset()):
                if entry.evidence is None:
                    why = "an authority entry has no evidence"
                else:
                    seen = self._verified.get((entry.hash, entry.evidence))
                    if seen is not None and now - seen < self._ttl:
                        why = None
                    else:
                        why = self._ask_verifier(
                            entry.section, entry.key, entry.op, entry.value,
                            entry.key_revision, entry.evidence,
                        )  # fmt: skip
                        if why is None and self._ttl > 0:
                            self._verified[(entry.hash, entry.evidence)] = now
                if why is not None:
                    ignored.append(Unverified(entry.seq, entry.section, entry.key, why))
                    continue
            if entry.op == "set":
                state[(entry.section, entry.key)] = entry.value
            else:
                state.pop((entry.section, entry.key), None)
        return state, ignored

    def _state(self) -> dict[tuple[str, str], Any]:
        return self._fold()[0]

    def unverified(self) -> list[Unverified]:
        """The authority entries this ledger ignores, with the reason for each."""
        return self._fold()[1]

    def value(self, section: str, key: str, default: Any = None) -> Any:
        return self._state().get((section, key), default)

    def revision(self, section: str, key: str) -> int:
        return _revisions(self._load()[0]).get((section, key), 0)

    def as_site_settings_source(self) -> Callable[[str], Mapping[str, Any]]:
        """The function ``attest.site_settings.register`` takes: every site sees this node's
        settings as ``{section: {key: value}}``, and a damaged ledger yields nothing."""

        def source(_site_id: str) -> Mapping[str, Any]:
            tree: dict[str, dict[str, Any]] = {}
            for (section, key), value in self._state().items():
                tree.setdefault(section, {})[key] = value
            return tree

        return source


class SectionWriter:
    """Writes one section of one ledger on behalf of one actor."""

    def __init__(self, ledger: SettingsLedger, section: str, actor: str) -> None:
        self._ledger = ledger
        self._section = section
        self._actor = actor

    def set(
        self, key: str, value: Any, *, expected_revision: int, evidence: str | None = None
    ) -> Entry:
        return self.write(
            self._section, key, value, expected_revision=expected_revision, evidence=evidence
        )

    def unset(self, key: str, *, expected_revision: int, evidence: str | None = None) -> Entry:
        return self._ledger._append(
            self._section, key, "unset", None, self._actor, expected_revision, evidence
        )

    def write(
        self,
        section: str,
        key: str,
        value: Any,
        *,
        expected_revision: int,
        evidence: str | None = None,
    ) -> Entry:
        if section != self._section:
            raise SectionNotOwned(
                f"a writer for {self._section!r} cannot write section {section!r}"
            )
        self._ledger._check_value(section, key, value)
        return self._ledger._append(
            section, key, "set", value, self._actor, expected_revision, evidence
        )


def _revisions(entries: list[Entry]) -> dict[tuple[str, str], int]:
    revisions: dict[tuple[str, str], int] = {}
    for entry in entries:
        revisions[(entry.section, entry.key)] = entry.key_revision
    return revisions


__all__ = [
    "AuthorityRefused",
    "AuthorityRequired",
    "AuthorityVerifier",
    "Entry",
    "InvalidValue",
    "LedgerDamaged",
    "SectionNotOwned",
    "SectionWriter",
    "SettingsLedger",
    "SettingsLedgerError",
    "StaleRevision",
    "GENESIS",
    "UnknownSection",
    "Unverified",
    "VerifyReport",
    "change_digest",
    "reset_authority_verifier",
    "set_authority_verifier",
]
