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


def params_digest(params: dict[str, Any] | None) -> str:
    """SHA-256 over the canonical JSON of the *redacted* params."""
    canonical = json.dumps(
        _redact(dict(params or {})), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


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


def _reset_for_tests() -> None:
    """Forget cached chain tails (test isolation across tmp dirs)."""
    with _lock:
        _tails.clear()


__all__ = [
    "DEFAULT_PRINCIPAL",
    "GENESIS",
    "HMAC_KEY_ENV",
    "RECORD_FIELDS",
    "UNKEYED",
    "audit_path",
    "emit",
    "emit_failure_count",
    "params_digest",
    "read_chain",
    "record_invocation",
    "verify_chain",
]
