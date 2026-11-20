# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Universal action audit — one tamper-evident record per mutating skill
invocation (S1 mechanism).

Every CLI verb, MCP tool, and agent persona reaches skills through
``SkillRegistry.invoke`` (ADR-056), so that seam is where the audit lives:
after each invocation of a mutating skill, one record is appended to the
hash chain at ``<state_dir>/audit/actions.jsonl``.

Record shape::

    {seq, ts, principal, skill, params_digest, site, outcome,
     errors_count, prev_hash, hash}

- ``params_digest`` is the SHA-256 of the canonical-JSON params with any
  value whose key looks secret-shaped (``token|secret|password|key``,
  case-insensitive, at any nesting depth) redacted BEFORE digesting — the
  audit proves *which* invocation happened without ever storing a credential.
- ``hash`` chains via HMAC-SHA256 exactly the way the platform's existing
  EC audit chain does (:func:`axiom.infra.audit_log._compute_hmac`:
  ``HMAC(key, canonical(record_without_hash) + prev_hash)``, ``GENESIS``
  sentinel). The key is ``$AXIOM_AUDIT_HMAC_KEY`` — the same key the EC
  chain and the action-provenance ledger use — falling back to the
  documented ``"unkeyed"`` sentinel so the chain is always tamper-evident
  against casual edits even before a key is provisioned.

The file is shared territory with the action-provenance ledger's JSONL
fallback (``axiom.policy.action_ledger``), which also lands in
``<state_dir>/audit/actions.jsonl`` when SQL is unavailable. The two record
shapes are disjoint (this chain carries ``hash``/``prev_hash``/``skill``;
the ledger carries ``hmac``), and :func:`read_chain` / :func:`verify_chain`
walk only skill-audit records — mirroring how the ledger's own
``verify_chain`` skips foreign lines.

Failure isolation is absolute: an audit-write error must NEVER fail the
skill invocation. Errors are swallowed, counted
(:func:`emit_failure_count`), and logged as warnings.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axiom.infra.audit_log import _GENESIS, _compute_hmac
from axiom.infra.state import locked_append_jsonl

if TYPE_CHECKING:
    from axiom.infra.skills import SkillContext

_log = logging.getLogger(__name__)

#: Chain-start sentinel — the same one the EC audit chain uses.
GENESIS = _GENESIS

#: Env var holding the chain key (shared with the EC audit log and the
#: action-provenance ledger). Absent → the ``"unkeyed"`` sentinel.
HMAC_KEY_ENV = "AXIOM_AUDIT_HMAC_KEY"
UNKEYED = "unkeyed"

#: What an unattributed invocation is recorded as (a local CLI session).
DEFAULT_PRINCIPAL = "@cli:local"

_SECRET_KEY_RE = re.compile(r"token|secret|password|key", re.IGNORECASE)
_REDACTED = "[REDACTED]"

# The fields that make up a record, in the order they are written.
RECORD_FIELDS = (
    "seq", "ts", "principal", "skill", "params_digest", "site",
    "outcome", "errors_count", "prev_hash", "hash",
)
#: Present only on a call that is a step of a sequence (ADR-185).
CHAIN_FIELDS = ("chain_id", "step_id", "parent_step")

#: Present only on a write that said what it changed (:func:`note_change`):
#: ``{kind, subject, old, new}``. The change log (``settings.changes``) reads it,
#: so "who changed what from what to what, and when" is one row of this one
#: chain, never a second table (MVP-08-A2, MVP-13).
CHANGE_FIELD = "change"

#: The change the running skill noted, picked up by :func:`emit` for its record.
_pending_change: ContextVar[dict[str, Any] | None] = ContextVar("axiom_audit_change", default=None)

#: Present only on a read of a signed record (logbook unit X2): ``{what, logbook, site, ids,
#: count, surface, person}``. A record read through any door carries it, so "who read logbook X"
#: is one row of this one chain and never a second store. Ids only; never content.
ACCESS_FIELD = "access"

#: The access the running read skill noted, picked up by :func:`emit` for its record.
_pending_access: ContextVar[dict[str, Any] | None] = ContextVar("axiom_audit_access", default=None)

_lock = threading.Lock()
#: path → (last_seq, last_hash) so each append does not re-read the file.
_tails: dict[str, tuple[int, str]] = {}
_failures = 0


def emit_failure_count() -> int:
    """How many audit writes have failed (and been swallowed) this process."""
    return _failures


def audit_path(state_dir: Path) -> Path:
    """Where the chain lives under a state dir."""
    return Path(state_dir) / "audit" / "actions.jsonl"


# ---------------------------------------------------------------- digest


def _redact(value: Any) -> Any:
    """Copy ``value`` with every secret-shaped key's value replaced."""
    if isinstance(value, dict):
        return {
            k: _REDACTED if _SECRET_KEY_RE.search(str(k)) else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def redacted(value: Any) -> Any:
    """A copy of ``value`` with every secret-shaped key's value hidden, by the rule the chain
    uses, so a surface that shows parameters hides exactly what the audit record hides."""
    return _redact(value)


def without_secrets(value: Any) -> Any:
    """A copy of ``value`` with every secret-shaped key left out, by the chain's rule: a view a
    person reads never names a secret parameter, not even as a redacted row (SBD-A21)."""
    if isinstance(value, dict):
        return {
            k: without_secrets(v) for k, v in value.items() if not _SECRET_KEY_RE.search(str(k))
        }
    if isinstance(value, (list, tuple)):
        return [without_secrets(v) for v in value]
    return value


def has_secret(value: Any) -> bool:
    """Whether ``value`` carries a secret-shaped key anywhere: "Uses a stored credential"."""
    if isinstance(value, dict):
        return any(_SECRET_KEY_RE.search(str(k)) or has_secret(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_secret(v) for v in value)
    return False


def params_digest(params: dict[str, Any] | None) -> str:
    """SHA-256 over the canonical JSON of the *redacted* params."""
    canonical = json.dumps(
        _redact(dict(params or {})), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------- change notes


#: A setting whose leaf name suggests a credential (the settings skills' rule).
_SECRETISH_SETTING = re.compile(r"url|token|secret|password|key|dsn|credential", re.IGNORECASE)


def _shown_value(kind: str, subject: str, value: Any) -> Any:
    """A value as the change log may show it.

    A setting whose name looks like a credential records only whether it is set;
    every other value is copied with secret-shaped dict keys redacted.
    """
    if kind == "setting" and _SECRETISH_SETTING.search(str(subject or "").rsplit(".", 1)[-1]):
        return "(set)" if value not in (None, "", [], {}) else ""
    return _redact(value)


def note_change(*, kind: str, subject: str, old: Any, new: Any) -> None:
    """Say what the running write changed, for its one audit record.

    A skill calls this once, after it has read the old value and written the
    new one. The record :func:`emit` writes for this invocation then carries
    ``change = {kind, subject, old, new}`` under the same HMAC as every other
    field. A secret-shaped subject records only whether it is set. Outside a
    skill invocation (no registry around it) the note is simply dropped.
    """
    _pending_change.set({
        "kind": str(kind),
        "subject": str(subject),
        "old": _shown_value(kind, subject, old),
        "new": _shown_value(kind, subject, new),
    })


def begin_invocation() -> Any:
    """Clear any stale note before a skill runs; returns the change note's reset token."""
    _pending_access.set(None)
    return _pending_change.set(None)


def take_change(token: Any = None) -> dict[str, Any] | None:
    """The note the skill left (if any), clearing it."""
    change = _pending_change.get()
    if token is not None:
        try:
            _pending_change.reset(token)
        except ValueError:  # a different context: just clear it
            _pending_change.set(None)
    else:
        _pending_change.set(None)
    return change


def note_access(access: dict[str, Any]) -> None:
    """Say what the running read skill read, for its one audit record (logbook unit X2).

    A read skill calls this once; the record :func:`emit` writes for the invocation then carries
    ``access`` under the same HMAC as every other field. Outside a skill invocation the note is
    dropped at the next :func:`begin_invocation`.
    """
    _pending_access.set(dict(access))


def take_access() -> dict[str, Any] | None:
    """The access note the skill left (if any), clearing it."""
    access = _pending_access.get()
    _pending_access.set(None)
    return access


# ---------------------------------------------------------------- append


def _chain_key(key: str | None = None) -> str:
    return key or os.environ.get(HMAC_KEY_ENV) or UNKEYED


def _now_iso_ms() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _is_chain_record(rec: Any) -> bool:
    return isinstance(rec, dict) and "hash" in rec and "prev_hash" in rec and "skill" in rec


def _tail(path: Path) -> tuple[int, str]:
    """Last (seq, hash) of the chain at ``path`` — cached per path."""
    cached = _tails.get(str(path))
    if cached is not None:
        return cached
    seq, prev = 0, GENESIS
    if path.exists():
        for rec in read_chain(path):
            seq, prev = int(rec.get("seq", seq)), str(rec.get("hash", prev))
    _tails[str(path)] = (seq, prev)
    return seq, prev


def record_invocation(
    *,
    state_dir: Path,
    skill: str,
    params: dict[str, Any] | None,
    principal: str,
    site: str | None,
    outcome: str,
    errors_count: int,
    key: str | None = None,
    chain: dict[str, Any] | None = None,
    change: dict[str, Any] | None = None,
    access: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one record to the chain; returns the record written.

    Raises on I/O failure — callers wanting isolation go through
    :func:`emit`, which swallows and counts.
    """
    path = audit_path(state_dir)
    with _lock:
        seq, prev = _tail(path)
        record: dict[str, Any] = {
            "seq": seq + 1,
            "ts": _now_iso_ms(),
            "principal": principal,
            "skill": skill,
            "params_digest": params_digest(params),
            "site": site,
            "outcome": outcome,
            "errors_count": int(errors_count),
            "prev_hash": prev,
        }
        # A call that is a step of a sequence (ADR-185) says so. The keys exist
        # only for a chained call, so an unchained record is byte-identical to
        # what it always was; the HMAC covers them like every other field.
        if chain:
            for k in ("chain_id", "step_id", "parent_step"):
                if chain.get(k):
                    record[k] = str(chain[k])
        # Likewise a write that noted its change: the key exists only then.
        if change:
            record[CHANGE_FIELD] = dict(change)
        # And a read that said what it read (unit X2): the key exists only then.
        if access:
            record[ACCESS_FIELD] = dict(access)
        record["hash"] = _compute_hmac(_chain_key(key), record, prev)
        locked_append_jsonl(path, record)
        _tails[str(path)] = (record["seq"], record["hash"])
        return record


def emit(
    ctx: SkillContext,
    *,
    skill: str,
    params: dict[str, Any] | None,
    outcome: str,
    errors_count: int,
    chain: dict[str, Any] | None = None,
    change: dict[str, Any] | None = None,
    access: dict[str, Any] | None = None,
) -> None:
    """Journal one skill invocation; NEVER raises (failure isolation).

    The principal is ``ctx.actor`` when a dispatcher set one
    (``@<agent>:<context>``, ``@user:<context>``), else the local-CLI
    default. The tenancy context comes from ``$AXIOM_SITE`` — the same
    source the gate skills stamp onto accounts and keys.
    """
    global _failures
    try:
        record_invocation(
            state_dir=ctx.state_dir,
            skill=skill,
            params=params,
            principal=getattr(ctx, "actor", None) or DEFAULT_PRINCIPAL,
            site=os.environ.get("AXIOM_SITE") or None,
            outcome=outcome,
            errors_count=errors_count,
            chain=chain if chain is not None else getattr(ctx, "chain", None),
            change=change,
            access=access,
        )
    except Exception as exc:  # noqa: BLE001 — isolation is the contract
        _failures += 1
        try:
            ctx.logger.warning("action audit write failed for %s: %s", skill, exc)
        except Exception:  # noqa: BLE001 — even the warning must not escape
            _log.warning("action audit write failed for %s: %s", skill, exc)


# ---------------------------------------------------------------- read


def read_chain(path: Path) -> list[dict[str, Any]]:
    """The skill-audit records at ``path``, in file order.

    Foreign lines (the action ledger's fallback records, garbage) are
    skipped — the chain is the subsequence carrying ``hash``/``prev_hash``.
    """
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _is_chain_record(rec):
            out.append(rec)
    return out


def verify_chain(path: Path, key: str | None = None) -> bool:
    """Walk the chain at ``path`` recomputing every link.

    ``True`` iff every record's ``prev_hash`` matches its predecessor's
    ``hash`` and every ``hash`` recomputes under ``key`` (default: the
    environment's chain key, else ``"unkeyed"``). Any flipped byte,
    reordered line, or deleted record breaks a link.
    """
    chain_key = _chain_key(key)
    prev = GENESIS
    for rec in read_chain(path):
        if rec.get("prev_hash") != prev:
            return False
        body = {k: v for k, v in rec.items() if k != "hash"}
        if rec.get("hash") != _compute_hmac(chain_key, body, prev):
            return False
        prev = rec["hash"]
    return True


def verify_report(path: Path, key: str | None = None) -> dict[str, Any]:
    """:func:`verify_chain` with the evidence: how many records, and where it broke.

    ``{"ok", "records", "broken_at", "reason"}``; ``broken_at`` is the ``seq`` of
    the first record whose link or hash does not recompute (``None`` when intact).
    """
    chain_key = _chain_key(key)
    prev = GENESIS
    records = read_chain(path)
    for rec in records:
        if rec.get("prev_hash") != prev:
            return {"ok": False, "records": len(records), "broken_at": rec.get("seq"),
                    "reason": "its prev_hash is not its predecessor's hash (a record was removed or reordered)"}
        body = {k: v for k, v in rec.items() if k != "hash"}
        if rec.get("hash") != _compute_hmac(chain_key, body, prev):
            return {"ok": False, "records": len(records), "broken_at": rec.get("seq"),
                    "reason": "its hash does not recompute (the record was edited)"}
        prev = rec["hash"]
    return {"ok": True, "records": len(records), "broken_at": None, "reason": ""}


def _reset_for_tests() -> None:
    """Forget cached chain tails (test isolation across tmp dirs)."""
    with _lock:
        _tails.clear()


__all__ = [
    "ACCESS_FIELD",
    "CHANGE_FIELD",
    "DEFAULT_PRINCIPAL",
    "GENESIS",
    "HMAC_KEY_ENV",
    "RECORD_FIELDS",
    "UNKEYED",
    "audit_path",
    "begin_invocation",
    "emit",
    "emit_failure_count",
    "note_access",
    "note_change",
    "params_digest",
    "read_chain",
    "record_invocation",
    "take_access",
    "take_change",
    "verify_chain",
    "verify_report",
]
