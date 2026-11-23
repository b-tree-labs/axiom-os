# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The chain is checked on the node heartbeat (unit X2; spec-attestation "Harness integration").

:func:`check_incremental` verifies each logbook at each served site from the last verified
position (``<state_dir>/attest/verified_heads.json``), through the public library's
``verify.chain`` slice, then checks the newest anchor covering the logbook. On success the
position advances; on failure it does not, so the next run looks at the same records again.
:func:`verify_all` walks every chain from sequence one and records the result the same way.

Each result is also published on the bus as ``health.attestation_chain.ok|failed`` (a platform
event the journal indexes; the payload carries ids and reasons, never a record), and the hygiene
node-health check turns the results into findings. A failure also tells the people holding the
log's ``integrity_notify`` roles at the site, once per break (#1812). Nothing here blocks, locks or stops anything
(ADR-142 rule 7): a finding is a finding.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.site_scope import deployment_sites
from axiom.infra.state import LockedJsonFile

from . import service, signing

log = logging.getLogger("axiom.attest.integrity")

SUBJECT_OK = "health.attestation_chain.ok"
SUBJECT_FAILED = "health.attestation_chain.failed"


def verified_heads_path(state_dir: Path) -> Path:
    return Path(state_dir) / "attest" / "verified_heads.json"


def verified_heads(state_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """``{site: {logbook: {seq, digest, verified_at}}}`` as saved; empty when nothing was."""
    with LockedJsonFile(verified_heads_path(state_dir)) as f:
        data = f.read()
    return data if isinstance(data, dict) else {}


def forget_verified(state_dir: Path) -> None:
    """Test isolation: the next check starts from sequence one again."""
    with LockedJsonFile(verified_heads_path(state_dir), exclusive=True) as f:
        f.write({})


def _save_head(state_dir: Path, site: str, logbook: str, seq: int, digest: str | None) -> None:
    with LockedJsonFile(verified_heads_path(state_dir), exclusive=True) as f:
        data = f.read()
        data = data if isinstance(data, dict) else {}
        data.setdefault(site, {})[logbook] = {
            "seq": int(seq),
            "digest": digest,
            "verified_at": datetime.now(UTC).isoformat(),
        }
        f.write(data)


def chains() -> list[tuple[str, str, int]]:
    """``(site, logbook, head_seq)`` for every chain at a site this node serves."""
    from sqlalchemy import text

    from . import store

    with store.session_scope() as s:
        rows = s.execute(
            text(
                "SELECT site_id, logbook, seq FROM attest_chain_heads WHERE seq > 0 "
                "ORDER BY site_id, logbook"
            )  # fmt: skip
        ).all()
    served = deployment_sites()
    return [(str(r[0]), str(r[1]), int(r[2])) for r in rows if served is None or r[0] in served]


def _latest_anchor(site: str, logbook: str, keys: dict[str, bytes]) -> dict[str, Any] | None:
    for a in reversed(service.anchors(site)):
        if any(h["logbook"] == logbook for h in a["heads"]):
            ok, reason = service.verify_site_anchor(a["anchor_id"], keys)
            return {"anchor_id": a["anchor_id"], "ok": ok, "reason": reason}
    return None


def _publish(fact: dict[str, Any]) -> None:
    try:
        from axiom.infra.bus import get_default_eventbus

        payload = {"schema_version": 1, **{k: v for k, v in fact.items() if k != "site"},
                   "site_id": fact["site"]}  # fmt: skip
        get_default_eventbus().publish(
            SUBJECT_OK if fact["ok"] else SUBJECT_FAILED, payload, source="attest.integrity"
        )
    except Exception:  # noqa: BLE001 - the fact is in the state file; the event is best effort
        pass


Sender = Callable[[str, dict[str, Any]], None]


def _default_sender(recipient: str, fact: dict[str, Any]) -> None:
    from axiom.extensions.builtins.notifications.send import (
        NotificationPayload,
        Priority,
        SendContext,
        send,
    )
    from axiom.governance.classification import Classification

    where = (
        f"at #{fact['first_bad_seq']} ({fact['reason']})"
        if fact.get("first_bad_seq") is not None
        else f"its anchor ({fact.get('anchor_reason')})"
    )
    send(
        SendContext.default(),
        actor=f"@attest:{fact['site']}",
        recipient=recipient,
        payload=NotificationPayload(
            summary=f"Log {fact['logbook']} failed verification {where}",
            body=f"The signed chain of {fact['logbook']} at {fact['site']} no longer verifies "
            f"{where}. Nothing is locked; run the full verification to see the break.",
            metadata={k: str(v) for k, v in fact.items()},
        ),
        classification=Classification.INTERNAL,
        priority=Priority.URGENT,
        dedup_key=_break_key(fact),
    )


_sender: Sender = _default_sender


def set_sender(sender: Sender) -> None:
    """Seam: where integrity notices go (tests capture them)."""
    global _sender
    _sender = sender


def reset_sender() -> None:
    global _sender
    _sender = _default_sender


def _break_key(fact: dict[str, Any]) -> str:
    """One cause, one key: the log, the first bad record and why, or the anchor and why."""
    return (
        f"integrity:{fact['site']}:{fact['logbook']}:{fact.get('first_bad_seq')}:"
        f"{fact.get('reason')}:{fact.get('anchor_id')}:{fact.get('anchor_reason')}"
    )


def _notified_path(state_dir: Path) -> Path:
    return Path(state_dir) / "attest" / "integrity_notified.json"


def _notify_break(state_dir: Path, fact: dict[str, Any]) -> None:
    """Tell the holders of the log's ``integrity_notify`` roles at the site, once per break
    (#1812). Best effort: the fact is recorded and published whether or not anyone is told."""
    from . import registry, roles
    from .logbooks import LogbookError

    try:
        wanted = set(registry.get(fact["logbook"]).integrity_notify)
    except LogbookError:
        return
    if not wanted:
        return
    key = _break_key(fact)
    with LockedJsonFile(_notified_path(state_dir), exclusive=True) as f:
        seen = f.read()
        seen = seen if isinstance(seen, dict) else {}
        if key in seen:
            return
        seen[key] = datetime.now(UTC).isoformat()
        f.write(seen)
    for h in roles.assignments(fact["site"], state_dir=state_dir):
        if set(h["roles"]) & wanted:
            try:
                _sender(h["principal"], dict(fact))
            except Exception:  # noqa: BLE001 - delivery is best effort
                log.warning("attest: integrity notice to %s not sent", h["principal"])


def _check_one(
    state_dir: Path,
    site: str,
    logbook: str,
    head_seq: int,
    *,
    from_seq: int,
    keys: dict[str, bytes],
) -> dict[str, Any]:
    from axiom.attest import verify as verify_lib

    fact: dict[str, Any] = {
        "site": site,
        "logbook": logbook,
        "from_seq": from_seq,
        "head_seq": head_seq,
        "checked": 0,
        "ok": True,
        "first_bad_seq": None,
        "reason": None,
        "anchor_id": None,
        "anchor_ok": None,
        "anchor_reason": None,
    }
    if from_seq <= head_seq:
        report = verify_lib.chain(site, logbook, from_seq=from_seq, public_keys=keys)
        fact["checked"] = report.checked
        if not report.ok:
            fact.update(ok=False, first_bad_seq=report.first_bad_seq, reason=report.reason)
    anchor = _latest_anchor(site, logbook, keys)
    if anchor is not None:
        fact.update(
            anchor_id=anchor["anchor_id"], anchor_ok=anchor["ok"], anchor_reason=anchor["reason"]
        )
        if fact["ok"] and not anchor["ok"]:
            fact["ok"] = False
    if fact["ok"]:
        head = service.records(site, logbook, from_seq=head_seq)
        _save_head(state_dir, site, logbook, head_seq, head[0]["digest"] if head else None)
    _publish(fact)
    if not fact["ok"]:
        _notify_break(state_dir, fact)
    return fact


def check_incremental(
    state_dir: Path, *, keys: dict[str, bytes] | None = None
) -> list[dict[str, Any]]:
    """Verify what is new since the last verified position, per served site and logbook, and
    check the newest anchor. Advances the position only on success. Raises when the store
    cannot be read, so the caller can say "not checked" rather than "verified"."""
    state_dir = Path(state_dir)
    keys = signing.public_keys() if keys is None else keys
    saved = verified_heads(state_dir)
    out: list[dict[str, Any]] = []
    for site, logbook, head_seq in chains():
        last = int((saved.get(site, {}).get(logbook) or {}).get("seq") or 0)
        out.append(_check_one(state_dir, site, logbook, head_seq, from_seq=last + 1, keys=keys))
    return out


def verify_all(state_dir: Path, *, keys: dict[str, bytes] | None = None) -> dict[str, Any]:
    """Every chain at every served site from sequence one; the first break, if any."""
    state_dir = Path(state_dir)
    keys = signing.public_keys() if keys is None else keys
    facts = [
        _check_one(state_dir, site, logbook, head_seq, from_seq=1, keys=keys)
        for site, logbook, head_seq in chains()
    ]
    first_break = next((f for f in facts if not f["ok"]), None)
    return {"ok": first_break is None, "logbooks": facts, "first_break": first_break}


__all__ = [
    "SUBJECT_FAILED",
    "SUBJECT_OK",
    "chains",
    "check_incremental",
    "forget_verified",
    "reset_sender",
    "set_sender",
    "verified_heads",
    "verified_heads_path",
    "verify_all",
]
